"""Версии бота и обновление самого себя.

Номер версии — из истории изменений app/changelog.json (новые выпуски сверху). После обновления бот пишет
администраторам, что нового с прошлой виденной версии.

Обновляет сервер, а не контейнер: таймер systemd раз в час запускает `bcloud _auto`. Тот смотрит, есть ли новая
версия в GitHub и собран ли для неё образ (тесты прошли), и спрашивает бота, можно ли сейчас:
`docker compose exec bot python -m app.selfupdate check [--now]` — код 0, если автообновление включено и сейчас
выбранный час (с запасом в 3 часа) и никакой сервер не создаётся. «Обновить сейчас» — файл data/update-now,
его видит systemd (bcloud-update-now.path).
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

KEY = "self_update"
WINDOW = 3
NOW_FILE = "update-now"
CHANGELOG = Path(__file__).with_name("changelog.json")
KIND_MARK = {"new": "✨", "better": "⚡️", "fix": "🛠"}
_REMOTE: dict = {}


# ───────── история версий ─────────
def changelog() -> list[dict]:
    try:
        return json.loads(CHANGELOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def vkey(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def current() -> str:
    log = changelog()
    return log[0]["version"] if log else ""


def newer(log: list[dict], than: str) -> list[dict]:
    return [r for r in log if vkey(r["version"]) > vkey(than)] if than else log[:1]


def whats_new_text(seen: str) -> str | None:
    """Сообщение «что нового» после обновления (None — версия та же). Первый запуск с версиями — только текущая."""
    log = changelog()
    if not log or seen == log[0]["version"]:
        return None
    fresh = newer(log, seen)[:5]
    if not fresh:
        return None
    lines = [f"🔄 <b>Бот обновился до версии {html.escape(log[0]['version'])}</b>" + (f" (была {html.escape(seen)})" if seen else "")]
    for r in fresh:
        lines.append(f"\n<b>{html.escape(r['version'])} — {html.escape(r['title'])}</b>")
        lines += [f"{KIND_MARK.get(i.get('kind'), '•')} {html.escape(i['text'])}" for i in r.get("items", [])[:8]]
    return "\n".join(lines)


def data_dir() -> Path:
    from .config import env
    url = env().database_url
    if url.startswith("sqlite") and ":///" in url:
        return Path(url.split(":///", 1)[1]).resolve().parent
    return Path("data").resolve()


def version(ddir: Path | None = None) -> dict:
    """Номер — из истории, коммит и дата — из data/version.txt (пишет bcloud при запуске)."""
    out = {"number": current()}
    try:
        sha, date, subject = ((ddir or data_dir()) / "version.txt").read_text(encoding="utf-8").rstrip("\n").split("\t", 2)
        out.update(sha=sha[:7], date=date, subject=subject)
    except (OSError, ValueError):
        pass
    return out


# ───────── настройки автообновления ─────────
def parse_cfg(raw: str | None) -> dict:
    try:
        c = json.loads(raw or "{}")
    except ValueError:
        c = {}
    return {"on": bool(c.get("on")), "hour": int(c.get("hour", 4)) % 24, "tz": int(c.get("tz", 180)), "last": c.get("last", ""),
            "once": bool(c.get("once"))}


def clean(raw: dict, cur: dict) -> dict:
    out = dict(cur)
    if "on" in raw:
        out["on"] = bool(raw["on"])
    if "hour" in raw:
        h = int(raw["hour"])
        if not 0 <= h <= 23:
            raise ValueError("Час: от 0 до 23")
        out["hour"] = h
    if "tz" in raw:
        tz = int(raw["tz"])
        if not -14 * 60 <= tz <= 14 * 60:
            raise ValueError("Неверный часовой пояс")
        out["tz"] = tz
    return out


def due(c: dict, busy: bool, now: float | None = None) -> tuple[bool, str]:
    if not c["on"] and not c.get("once"):
        return False, "автообновление выключено"
    t = dt.datetime.fromtimestamp(now if now is not None else time.time(), dt.timezone.utc) + dt.timedelta(minutes=c["tz"])
    if (t.hour - c["hour"]) % 24 >= WINDOW:
        return False, f"не время: обновляюсь в {c['hour']:02d}:00"
    if c.get("last") == t.strftime("%Y-%m-%d"):
        return False, "сегодня уже обновлялся"
    if busy:
        return False, "создаются серверы — попробую через час"
    return True, t.strftime("%Y-%m-%d")


async def get_setting(key: str) -> str | None:
    from .db import Setting, session
    async with session() as s:
        row = await s.get(Setting, key)
        return row.value if row else None


async def set_setting(key: str, value: str) -> None:
    from .db import Setting, session
    async with session() as s:
        row = await s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=value))
        else:
            row.value = value
        await s.commit()


async def cfg() -> dict:
    return parse_cfg(await get_setting(KEY))


# ───────── новая версия на GitHub ─────────
def repo_slug(ddir: Path | None = None) -> str:
    url = "https://github.com/I22rusIBarney/bobr-cloud.git"
    try:
        url = ((ddir or data_dir()) / "origin.txt").read_text().strip() or url
    except OSError:
        pass
    return re.sub(r"(\.git)?/*$", "", re.sub(r"^.*github\.com[:/]", "", url))


def current_sha(ddir: Path | None = None) -> str:
    try:
        sha = ((ddir or data_dir()) / "version.txt").read_text().split("\t", 1)[0].strip()
    except OSError:
        return ""
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else ""


async def check_remote(force: bool = False, client=None, ddir: Path | None = None) -> dict:
    """state: latest — новее нет; ready — можно ставить; testing — тесты идут; failed — не прошли; unknown — GitHub молчит."""
    import httpx
    slug, cur = repo_slug(ddir), current_sha(ddir)
    hit = _REMOTE.get(slug)
    if hit and not force and time.time() - hit["at"] < 300 and hit["cur"] == cur:
        return hit["res"]
    res = {"state": "unknown", "current": current(), "current_sha": cur[:7], "version": "", "sha": "", "changes": [], "checked_at": int(time.time())}
    own = client is None
    c = client or httpx.AsyncClient(timeout=15, headers={"Accept": "application/vnd.github+json", "User-Agent": "bobr-cloud"})
    try:
        r = await c.get(f"https://api.github.com/repos/{slug}/commits/main")
        r.raise_for_status()
        sha = r.json()["sha"]
        res["sha"] = sha[:7]
        if cur and sha == cur:
            res["state"] = "latest"
        else:
            try:
                log = (await c.get(f"https://raw.githubusercontent.com/{slug}/{sha}/app/changelog.json")).json()
            except Exception:  # noqa: BLE001
                log = []
            if isinstance(log, list) and log:
                res["version"] = log[0].get("version", "")
                res["changes"] = newer(log, current())[:5]
            runs = (await c.get(f"https://api.github.com/repos/{slug}/actions/runs", params={"head_sha": sha, "per_page": 5})).json()
            runs = [x for x in runs.get("workflow_runs") or [] if x.get("event") == "push"]
            if not runs:
                res["state"] = "testing"
            elif any(x.get("conclusion") == "failure" for x in runs):
                res["state"] = "failed"
            elif all(x.get("status") == "completed" and x.get("conclusion") == "success" for x in runs):
                res["state"] = "ready"
            else:
                res["state"] = "testing"
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).info("update check: %s", e)
        res["error"] = "GitHub не ответил — попробуйте позже"
    finally:
        if own:
            await c.aclose()
    if res["state"] != "unknown":
        _REMOTE[slug] = {"at": time.time(), "cur": cur, "res": res}
    return res


# ───────── проверка из bcloud _auto (вне бота, без asyncio) ─────────
def _db_path() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:////srv/data/bot.db")
    return url.split(":///", 1)[1] if ":///" in url else "data/bot.db"


def main(argv: list[str]) -> int:
    if argv[1:2] != ["check"]:
        print("usage: python -m app.selfupdate check [--now]")
        return 2
    con = sqlite3.connect(_db_path())
    try:
        get = lambda k: (con.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone() or [None])[0]  # noqa: E731

        def put(k, v):
            con.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
            con.commit()
        try:
            busy = bool(con.execute("SELECT COUNT(*) FROM servers WHERE status='provisioning'").fetchone()[0])
        except sqlite3.Error:
            busy = False
        c = parse_cfg(get(KEY))
        if "--now" in argv or get("self_update_pending"):
            if busy:
                put("self_update_pending", "1")
                print("создаются серверы — обновлюсь, когда закончится")
                return 1
            put("self_update_pending", "")
            print("ok")
            return 0
        ok, why = due(c, busy)
        if not ok:
            print(why)
            return 1
        put(KEY, json.dumps({**c, "last": why, "once": False}))
        print("ok")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
