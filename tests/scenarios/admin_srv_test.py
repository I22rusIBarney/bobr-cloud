"""Требования ОС, цена смены IP без наценки, сторож маржи, действия админа с сервером клиента."""
import asyncio, os, sys, json, time, hmac, hashlib
from urllib.parse import urlencode
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from app import db as D, services as svc
from app.web.server import build_app
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select


def init(uid, name="Иван"):
    f = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": name, "username": f"user{uid}"}), "query_id": "q"}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
    sec = hmac.new(b"WebAppData", b"123:TEST", hashlib.sha256).digest()
    return urlencode({**f, "hash": hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest()})


PRESETS = [{"idx": 0, "cpu": 1, "ram": 1, "disk": 10, "price_month_rub": {"nl": 188}}, {"idx": 1, "cpu": 2, "ram": 4, "disk": 60, "price_month_rub": {"nl": 675}}]


class FakeAstra:
    def __init__(s): s.n = 100; s.actions = []; s.renew_price = 188
    async def catalog(s): return {"locations": [{"code": "nl", "name": "🇳🇱 Нидерланды", "available": True}], "presets": PRESETS, "os": ["ubuntu2404", "win2022"]}
    async def balance(s): return {"balance_rub": 5000}
    async def create(s, **kw):
        s.n += 1; p = PRESETS[kw.get("idx", 0)]
        return {"id": s.n, "spec": {"cpu": p["cpu"], "ram": p["ram"], "disk": p["disk"], "os": kw["os"]}, "charged_rub": 188, "expires_in_days": 30}
    async def servers(s): return []
    async def server(s, sid): return {"id": sid, "status": "active", "renew_month_rub": s.renew_price, **({"ip": s.ips[sid]} if sid in getattr(s, "ips", {}) else {})}
    async def action(s, sid, a, body=None):
        s.actions.append((sid, a, body))
        if a == "change-ip":
            s.ips = {**getattr(s, "ips", {}), sid: f"203.0.113.{sid % 250}"}
        return {"ok": True, "url": "https://vnc.astravps.co/tok"} if a == "vnc" else {"ok": True}
    async def delete(s, sid): s.actions.append((sid, "delete", None)); return {"ok": True}
    async def close(s): pass


class Stub:
    configured = False; enabled = False
    async def close(s): pass


msgs = []
async def notify(uid, t): msgs.append((uid, t))
async def alert(t): msgs.append(("admin", t))


def ok(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond: ok.fails += 1
ok.fails = 0


async def main():
    await D.init_db()
    astra = FakeAstra()
    ctx = svc.Ctx(astra=astra, yk=Stub(), nalog=Stub(), notify=notify, alert_admin=alert)
    app = build_app(ctx)
    DOCS4 = {"docs": ["terms", "aup", "privacy", "pd"]}
    from app.settings import save_settings, load_settings
    async with D.session() as s:
        await save_settings(s, {"confirm_codes": False})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        A, B, HA = {"X-Init-Data": init(61, "Аня")}, {"X-Init-Data": init(62, "Боря")}, {"X-Init-Data": init(1, "Админ")}
        for h in (A, B, HA):
            await c.get("/api/me", headers=h); await c.post("/api/me/consent", headers=h, json=DOCS4)
        async with D.session() as s:
            await D.change_balance(s, 61, 500000, "admin_adjust")

        # ── требования ОС
        cat = (await c.get("/api/me/catalog", headers=A)).json()
        ok(cat.get("os_requirements", {}).get("win2022") == {"cpu": 2, "ram": 2, "disk": 40}, "кабинет получает требования Windows по умолчанию")
        r = await c.post("/api/me/servers", headers=A, json={"idx": 0, "location": "nl", "os": "win2022"})
        ok(r.status_code == 400 and "Windows" in r.json()["detail"] and "2 ГБ" in r.json()["detail"], "Windows на 1/1/10 купить нельзя — понятная причина")
        r = await c.post("/api/me/servers", headers=A, json={"idx": 1, "location": "nl", "os": "win2022"})
        ok(r.status_code == 200, "Windows на 2/4/60 — можно")
        r = await c.post("/api/me/servers", headers=A, json={"idx": 0, "location": "nl", "os": "ubuntu2404"}); small = r.json()["id"]
        big = (await c.get("/api/me", headers=A)).json()["servers"]
        big = next(x["id"] for x in big if x["id"] != small)
        async with D.session() as s:
            for sid in (small, big):
                x = await s.get(D.Server, sid); x.status = "active"; x.ip = "1.1.1.1"
            await s.commit()
        r = await c.post(f"/api/me/servers/{small}/action", headers=A, json={"action": "reinstall", "os": "win2022"})
        ok(r.status_code == 400 and "увеличьте" in r.json()["detail"], "переустановка на Windows слабого сервера — запрет")
        r = await c.post(f"/api/me/servers/{big}/action", headers=A, json={"action": "reinstall", "os": "win2022"})
        ok(r.status_code == 200, "переустановка на Windows мощного сервера — можно")
        r = await c.post("/api/admin/settings", headers=HA, json={"values": {"os_requirements": {"win2022": "4-8-80", "bad key!": "1-1-1", "x2": "junk"}}})
        async with D.session() as s:
            cfg = await load_settings(s)
        ok(cfg["os_requirements"] == {"win2022": "4-8-80"}, "админ меняет требования, мусор отбрасывается")
        ac = (await c.get("/api/admin/catalog", headers=HA)).json()
        ok(any(o["code"] == "win2022" for o in ac["os"]) and ac["os_requirements"]["win2022"] == "4-8-80", "админский каталог отдаёт список ОС и требования")

        # ── смена IP: без наценки — по себестоимости
        me = (await c.get("/api/me", headers=A)).json(); ok(me["cfg"]["change_ip_price_rub"] == 150, "обычный клиент: смена IP 150 ₽")
        await c.post("/api/admin/users/61/markup", headers=HA, json={"mode": "none"})
        me = (await c.get("/api/me", headers=A)).json(); ok(me["cfg"]["change_ip_price_rub"] == 100, "без наценки: смена IP 100 ₽")
        bal = me["balance_rub"]
        r = await c.post(f"/api/me/servers/{big}/action", headers=A, json={"action": "change-ip"})
        me = (await c.get("/api/me", headers=A)).json(); ok(r.status_code == 200 and round(bal - me["balance_rub"], 2) == 100, "списано 100 ₽")
        await c.post("/api/admin/users/61/markup", headers=HA, json={"mode": "default"})

        # ── сторож маржи
        from app import admin_servers as asv
        async with D.session() as s:
            x = await s.get(D.Server, small); price = x.price_kop; x.cost_kop = price + 5000; await s.commit()
        msgs.clear()
        n = await asv.margin_check(ctx); ok(n == 1 and any("Продление в минус" in t for _, t in msgs), "алерт о продлении в минус")
        msgs.clear(); n = await asv.margin_check(ctx); ok(n == 0 and not msgs, "повторно о той же цене не пишем")
        m = (await c.get("/api/admin/margin", headers=HA)).json()["servers"]
        ok(len(m) == 1 and m[0]["id"] == small and m[0]["loss_rub"] == 50 and m[0]["new_price_rub"] > m[0]["cost_rub"], "список убыточных с новой ценой")
        ok((await c.get("/api/admin/margin", headers=A)).status_code in (401, 403), "клиенту список недоступен")
        msgs.clear()
        r = (await c.post("/api/admin/margin/reprice-all", headers=HA)).json(); ok(r["changed"] == 1, "пересчитали все")
        ok(any(u == 61 and "со следующего продления" in t for u, t in msgs), "клиент получил уведомление о новой цене")
        ok((await c.get("/api/admin/margin", headers=HA)).json()["servers"] == [], "после пересчёта убыточных нет")
        async with D.session() as s:
            ok((await s.get(D.Server, small)).price_kop > price, "цена клиента выросла")
        # себестоимость из ответа Astra
        astra.renew_price = 999
        async with D.session() as s:
            aid = (await s.get(D.Server, big)).astra_id
        await svc.refresh_server(ctx, aid)
        async with D.session() as s:
            ok((await s.get(D.Server, big)).cost_kop == svc.cost_kop(999), "себестоимость продления обновляется из ответа Astra")

        # ── действия админа
        bal0 = (await c.get("/api/me", headers=A)).json()["balance_rub"]
        async with D.session() as s:
            exp0 = (await s.get(D.Server, small)).expires_at
        r = await c.post(f"/api/admin/servers/{small}/renew_free", headers=HA, json={})
        bal1 = (await c.get("/api/me", headers=A)).json()["balance_rub"]
        async with D.session() as s:
            exp1 = (await s.get(D.Server, small)).expires_at
        ok(r.status_code == 200 and bal1 == bal0 and exp1 > exp0, "бесплатное продление: срок +, баланс не тронут")
        ok(any(a == "renew" for _, a, _ in astra.actions), "Astra продлила")
        log = (await c.get(f"/api/me/servers/{small}/log", headers=A)).json()
        items = log.get("items", log) if isinstance(log, dict) else log
        ok(any(i["action"] == "renew_free" and i["who"] == "Поддержка" for i in items), "в журнале клиента — «Поддержка», без имени админа")
        r = await c.post(f"/api/admin/servers/{small}/reboot", headers=HA, json={}); ok(r.status_code == 200 and astra.actions[-1][1] == "reboot", "перезагрузка админом")
        r = await c.post(f"/api/admin/servers/{small}/vnc", headers=HA, json={}); ok(r.status_code == 200 and r.json().get("link"), "VNC админом — ссылка")
        bal_c = (await c.get("/api/me", headers=A)).json()["balance_rub"]
        msgs.clear()
        r = await c.post(f"/api/admin/servers/{small}/change_ip", headers=HA, json={"charge": False})
        async with D.session() as s:
            ip_now = (await s.get(D.Server, small)).ip
        ok(r.status_code == 200 and r.json()["ip"] == ip_now and ip_now and ip_now.startswith("203.0.113.")
           and (await c.get("/api/me", headers=A)).json()["balance_rub"] == bal_c, f"смена IP админом бесплатно: {r.text[:200]}")
        ok(any("Новый IP" in t for _, t in msgs), "клиенту пришёл новый IP")
        r = await c.post(f"/api/admin/servers/{small}/change_ip", headers=HA, json={"charge": True})
        ok(r.status_code == 200 and (await c.get("/api/me", headers=A)).json()["balance_rub"] < bal_c, "смена IP админом с баланса клиента")
        r = await c.post(f"/api/admin/servers/{small}/format", headers=HA, json={}); ok(r.status_code == 400, "неизвестное действие — 400")
        r = await c.post(f"/api/admin/servers/{small}/reboot", headers=B, json={}); ok(r.status_code in (401, 403), "клиент не может звать админские действия")
        msgs.clear()
        r = await c.post(f"/api/admin/servers/{small}/move", headers=HA, json={"to": "@user62"})
        async with D.session() as s:
            ok(r.status_code == 200 and (await s.get(D.Server, small)).tg_id == 62, "перенос сервера другому клиенту")
        ok(any(u == 62 for u, _ in msgs) and any(u == 61 for u, _ in msgs), "оба клиента уведомлены")
        r = await c.post(f"/api/admin/servers/{small}/move", headers=HA, json={"to": "62"}); ok(r.status_code == 400, "повторный перенос тому же — ошибка")
        bal_b = (await c.get("/api/me", headers=B)).json()["balance_rub"]
        r = await c.post(f"/api/admin/servers/{small}/delete", headers=HA, json={"refund": False})
        bal_b2 = (await c.get("/api/me", headers=B)).json()["balance_rub"]
        async with D.session() as s:
            ok(r.status_code == 200 and r.json()["refund_rub"] == 0 and bal_b2 == bal_b and (await s.get(D.Server, small)).status == "deleted", "удаление без возврата")
            lg = (await s.execute(select(D.ServerLog).where(D.ServerLog.server_id == small, D.ServerLog.action == "delete"))).scalar_one()
            ok(lg.actor == 1 and lg.detail == "без возврата", "в журнале: удалил админ, без возврата")
        bal_a = (await c.get("/api/me", headers=A)).json()["balance_rub"]
        r = await c.post(f"/api/admin/servers/{big}/delete", headers=HA, json={"refund": True})
        bal_a2 = (await c.get("/api/me", headers=A)).json()["balance_rub"]
        ok(r.status_code == 200 and r.json()["refund_rub"] > 0 and round(bal_a2 - bal_a, 2) == r.json()["refund_rub"], "удаление с возвратом — деньги на балансе клиента")
        card = (await c.get("/api/admin/users/61", headers=HA)).json()
        ok(all("refund_rub" in x for x in card["servers"]), "карточка клиента: сумма возврата по серверам")
    print("FAILS:", ok.fails)

asyncio.run(main())
