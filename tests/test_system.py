"""Журнал действий, версии, зашифрованные копии и восстановление, автообновление, место ошибки."""
import asyncio
import gzip
import json
import os
import sqlite3

from tests.test_flows import FakeAstra, FakeNalog, FakeYK, alert, init, notify


def env_setup(tmp_path, monkeypatch):
    for k, v in dict(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test",
                     WEBHOOK_SECRET="sec", DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/t.db").items():
        monkeypatch.setenv(k, v)
    from app.config import env
    env.cache_clear()


# ───────── история версий ─────────
def test_changelog_format():
    from app import selfupdate as su
    log = su.changelog()
    assert log, "история версий пуста"
    keys = [su.vkey(r["version"]) for r in log]
    assert keys == sorted(keys, reverse=True) and len(set(keys)) == len(keys), "версии — новые сверху, без повторов"
    for r in log:
        assert r["title"] and r["date"] and r["items"]
        for i in r["items"]:
            assert i["kind"] in ("new", "better", "fix") and i["text"]


def test_whats_new_and_due():
    from app import selfupdate as su
    cur = su.current()
    assert su.whats_new_text(cur) is None
    t = su.whats_new_text("")
    assert t and cur in t
    c = su.parse_cfg(json.dumps({"on": True, "hour": 4, "tz": 0}))
    import datetime as dt
    at4 = dt.datetime(2026, 11, 2, 4, 30, tzinfo=dt.timezone.utc).timestamp()
    assert su.due(c, False, at4)[0] is True
    assert su.due(c, True, at4)[0] is False
    assert su.due({**c, "on": False}, False, at4)[0] is False
    assert su.due(c, False, at4 + 6 * 3600)[0] is False


def test_selfupdate_check_cli(tmp_path, monkeypatch):
    from app import selfupdate as su
    db = tmp_path / "x.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE servers (id INTEGER, status TEXT)")
    con.execute("INSERT INTO servers VALUES (1, 'provisioning')")
    con.commit(); con.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    assert su.main(["x", "check", "--now"]) == 1          # создаётся сервер — подождать
    con = sqlite3.connect(db); con.execute("DELETE FROM servers"); con.commit(); con.close()
    assert su.main(["x", "check"]) == 0                   # отложенное «сейчас» срабатывает при следующем запуске таймера
    assert su.main(["x", "check"]) == 1                   # автообновление выключено


# ───────── журнал ─────────
def test_audit_match_and_describe():
    from app import audit
    assert audit.match("POST", "users/5/balance")[:2] == ("user.balance", "Изменил баланс клиента")
    assert audit.match("POST", "integrations/yookassa")[1] == "Подключил ЮKassa"
    assert audit.match("POST", "integrations/yookassa/test") is None
    assert audit.match("GET", "users/5") is None
    d = audit.describe({"values": {"markup_pct": 40, "yk_secret": "zzz"}, "delta_rub": 100, "secret_key": "s"})
    assert "markup_pct: 40" in d and "zzz" not in d and "сумма ₽: 100" in d and "s" not in d.split("сумма")[1].replace("сумма", "")


# ───────── копии ─────────
def make_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (tg_id INTEGER)"); con.execute("CREATE TABLE servers (id INTEGER)")
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO settings VALUES ('secret:yk_secret', 'S3CR3T')")
    con.commit(); con.close()


def test_encrypted_backup_roundtrip(tmp_path):
    from app import backup as bk
    import pytest
    src = tmp_path / "bot.db"; make_db(src)
    out = tmp_path / "out"; out.mkdir()
    key = bk.new_key()
    f = bk._make(str(src), str(out), key)
    assert f.endswith(".bcb")
    blob = open(f, "rb").read()
    assert b"S3CR3T" not in blob and blob.startswith(bk.MAGIC)
    db = bk.open_copy(blob, key.lower().replace("-", ""))      # ключ можно вводить без дефисов и строчными
    assert b"S3CR3T" in db
    with pytest.raises(bk.BackupError):
        bk.open_copy(blob, bk.new_key())
    with pytest.raises(bk.BackupError):
        bk.open_copy(blob, "")
    plain = bk._make(str(src), str(out), "")                   # без ключа — как раньше, без секретов
    assert plain.endswith(".db.gz") and b"S3CR3T" not in gzip.decompress(open(plain, "rb").read())
    for i in range(10):
        bk.keep_local(f, str(tmp_path / "data")) if i == 0 else None
    assert bk.local_copies(str(tmp_path / "data"))[0]["encrypted"]


def test_apply_pending(tmp_path):
    from app import backup as bk
    db = tmp_path / "bot.db"; db.write_bytes(b"old")
    assert bk.apply_pending(str(db)) is None
    (tmp_path / "restore.db").write_bytes(b"new")
    note = bk.apply_pending(str(db))
    assert note and db.read_bytes() == b"new" and any(p.name.startswith("bot.db.before-restore-") for p in tmp_path.iterdir())
    assert bk.restored_note(str(tmp_path)) == note and bk.restored_note(str(tmp_path)) is None


# ───────── место ошибки ─────────
def test_error_where():
    from app.errors import describe
    try:
        {}["x"]
    except KeyError as e:
        d = describe(e)
    assert d.startswith("KeyError: 'x'") and "test_system.py:" in d


# ───────── через API ─────────
def test_admin_api_audit_backup_update(tmp_path, monkeypatch):
    env_setup(tmp_path, monkeypatch)
    exits = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))

    async def main():
        from httpx import ASGITransport, AsyncClient
        from app import db as D, services as svc
        from app.web.server import build_app
        await D.init_db()
        ctx = svc.Ctx(astra=FakeAstra(), yk=FakeYK(), nalog=FakeNalog(), notify=notify, alert_admin=alert)
        app = build_app(ctx)
        HA = {"X-Init-Data": init(1, "Админ")}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
            r = await c.post("/api/admin/settings", headers=HA, json={"values": {"markup_pct": 35}})
            assert r.status_code == 200
            r = await c.post("/api/admin/promos", headers=HA, json={"code": "AUD1", "kind": "bonus", "value": 50})
            assert r.status_code == 200
            await c.post("/api/admin/promos", headers=HA, json={"code": "BAD", "kind": "discount", "value": 95})  # 400 — не пишется
            items = (await c.get("/api/admin/audit", headers=HA)).json()["items"]
            assert [x["label"] for x in items] == ["Создал промокод", "Изменил настройки"]
            assert items[0]["who"] == "Админ" and "AUD1" in items[0]["detail"] and "markup_pct: 35" in items[1]["detail"]
            assert (await c.get("/api/admin/audit?q=промокод", headers=HA)).json()["items"][0]["label"] == "Создал промокод"
            assert (await c.get("/api/admin/audit", headers={"X-Init-Data": init(2)})).status_code == 403

            # ключ копий и восстановление
            r = await c.post("/api/admin/backup/key", headers=HA, json={})
            key = r.json()["key"]
            assert (await c.get("/api/admin/backup", headers=HA)).json()["has_key"] is True
            from app import backup as bk
            out = tmp_path / "o"; out.mkdir()
            f = bk._make(bk.db_path(), str(out), key)
            r = await c.put("/api/admin/backup/restore?key=WRONG-KEY-0000", headers=HA, content=open(f, "rb").read())
            assert r.status_code == 400 and "Ключ" in r.json()["detail"]
            r = await c.put("/api/admin/backup/restore?key=" + key, headers=HA, content=open(f, "rb").read())
            assert r.status_code == 200 and (tmp_path / "restore.db").exists()
            await asyncio.sleep(1.7)
            assert exits == [0]                                   # бот перезапускается, при запуске подменит базу

            # версия и автообновление
            st = (await c.get("/api/admin/state", headers=HA)).json()
            from app import selfupdate as su
            assert st["version"]["number"] == su.current()
            r = await c.post("/api/admin/update", headers=HA, json={"on": True, "hour": 5, "tz": 420})
            assert r.json()["on"] is True and r.json()["hour"] == 5
            assert (await c.get("/api/admin/update", headers=HA)).json()["hour"] == 5
            labels = [x["label"] for x in (await c.get("/api/admin/audit", headers=HA)).json()["items"]]
            assert "Изменил автообновление" in labels and "Восстановил базу из копии" in labels and "Сменил ключ резервных копий" in labels
            assert all(key not in x["detail"] for x in (await c.get("/api/admin/audit", headers=HA)).json()["items"])

    asyncio.run(main())


def test_worker_alerts_with_location():
    from app import worker

    class Ctx:
        sent = []

        async def alert_once(self, key, text, every_s=0):
            self.sent.append(text)

    async def boom(ctx):
        raise ValueError("сломалось")

    async def run():
        ctx = Ctx()
        t = asyncio.create_task(worker._loop("demo", 3600, boom, ctx, 0))
        await asyncio.sleep(0.05)
        t.cancel()
        return ctx.sent
    sent = asyncio.run(run())
    assert sent and "«demo»" in sent[0] and "ValueError: сломалось" in sent[0] and "test_system.py:" in sent[0]
