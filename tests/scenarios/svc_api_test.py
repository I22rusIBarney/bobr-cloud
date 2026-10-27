"""Служебный API /api/svc: токен, поиск клиента, каталог, покупка от имени клиента с его баланса, статус с IP и паролем."""
import asyncio, os, sys
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db", SERVICE_TOKEN="svc-secret-token")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from app import db as D, services as svc
from app.web.server import build_app
from httpx import AsyncClient, ASGITransport

PRESETS = [{"idx": 0, "cpu": 1, "ram": 1, "disk": 10, "price_month_rub": {"nl": 188}},
           {"idx": 1, "cpu": 2, "ram": 4, "disk": 60, "price_month_rub": {"nl": 675}}]


class FakeAstra:
    def __init__(s): s.n = 100; s.ready = False; s.renamed = []
    async def catalog(s): return {"locations": [{"code": "nl", "name": "Нидерланды", "available": True}], "presets": PRESETS, "os": ["ubuntu2404", "debian12"]}
    async def balance(s): return {"balance_rub": 5000}
    async def create(s, **kw):
        s.n += 1; s.pw = kw.get("password"); p = PRESETS[kw.get("idx", 0)]
        return {"id": s.n, "spec": {"cpu": p["cpu"], "ram": p["ram"], "disk": p["disk"], "os": kw["os"]}, "charged_rub": 188, "expires_in_days": 30}
    async def servers(s): return []
    async def server(s, sid):
        if not s.ready:
            return {"id": sid, "status": "provisioning"}
        return {"id": sid, "status": "active", "ip": "198.51.100.5", "root_password": s.pw}
    async def action(s, sid, a, body=None): s.renamed.append((sid, a, body)); return {"ok": True}
    async def close(s): pass


class Stub:
    configured = False; enabled = False
    async def close(s): pass


async def notify(uid, t): pass
async def alert(t): pass


def ok(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond: ok.fails += 1
ok.fails = 0


async def main():
    await D.init_db()
    astra = FakeAstra()
    ctx = svc.Ctx(astra=astra, yk=Stub(), nalog=Stub(), notify=notify, alert_admin=alert)
    app = build_app(ctx)
    T = {"Authorization": "Bearer svc-secret-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        ok((await c.get("/api/svc/ping")).status_code == 401, "без токена — 401")
        ok((await c.get("/api/svc/ping", headers={"Authorization": "Bearer wrong"})).status_code == 401, "чужой токен — 401")
        ok((await c.get("/api/svc/ping", headers=T)).json() == {"ok": True}, "с токеном — ok")
        ok((await c.get("/api/svc/catalog?tg_id=77", headers=T)).status_code == 404, "неизвестный клиент — 404")

        ok((await c.get("/api/svc/balance?tg_id=1", headers=T)).status_code == 404, "аккаунта админа ещё нет — понятный 404")
        async with D.session() as s:
            s.add(D.User(tg_id=1, username="admin"))
            await s.commit()
            await D.change_balance(s, 1, 100000, "admin_adjust")
        ok((await c.get("/api/svc/balance?tg_id=1", headers=T)).json()["balance_kop"] == 100000, "баланс админа")

        cat = (await c.get("/api/svc/catalog?tg_id=1", headers=T)).json()
        nl = cat["locations"][0]
        ok(nl["code"] == "nl" and len(nl["offers"]) == 2 and nl["offers"][0]["price_kop"] > 0, "каталог: локация и тарифы с ценой")
        ok({o["code"] for o in cat["os"]} == {"ubuntu2404", "debian12"} and cat["periods"][0]["code"] == "1m", "каталог: ОС и сроки")

        # клиент: поиск по @username и ID, покупка от его имени с его баланса
        async with D.session() as s:
            s.add(D.User(tg_id=77, username="Client_77", first_name="Клиент"))
            s.add(D.User(tg_id=78, username="banned", banned=True))
            await s.commit()
            await D.change_balance(s, 77, 50000, "admin_adjust")
        r = (await c.get("/api/svc/users/resolve?q=@client_77", headers=T)).json()
        ok(r["tg_id"] == 77 and r["balance_kop"] == 50000 and r["admin"] is False, "клиент находится по @username без учёта регистра")
        ok((await c.get("/api/svc/users/resolve?q=77", headers=T)).json()["username"] == "Client_77", "клиент находится по ID")
        ok((await c.get("/api/svc/users/resolve?q=@nobody", headers=T)).status_code == 404, "неизвестный @username — 404")
        r = await c.post("/api/svc/servers", headers=T, json={"tg_id": 77, "idx": 0, "location": "nl", "os": "debian12"})
        ok(r.status_code == 200, "покупка от имени клиента")
        csid = r.json()["id"]
        ok((await c.get("/api/svc/balance?tg_id=77", headers=T)).json()["balance_kop"] < 50000, "списано с баланса клиента")
        async with D.session() as s:
            ok((await s.get(D.Server, csid)).tg_id == 77, "сервер в аккаунте клиента")
        r = await c.post("/api/svc/servers", headers=T, json={"tg_id": 78, "idx": 0, "location": "nl", "os": "debian12"})
        ok(r.status_code == 403, "заблокированному клиенту покупать нельзя")
        r = await c.post("/api/svc/servers", headers=T, json={"tg_id": 1, "idx": 0, "location": "nl", "os": "debian12", "password": "Str0ngPassw0rd"})
        ok(r.status_code == 200 and r.json()["status"] == "provisioning", "покупка с баланса админа")
        sid = r.json()["id"]
        bal = (await c.get("/api/svc/balance?tg_id=1", headers=T)).json()["balance_kop"]
        ok(bal < 100000, "деньги списаны с баланса админа")

        v = (await c.get(f"/api/svc/servers/{sid}?tg_id=1", headers=T)).json()
        ok(v["status"] == "provisioning" and v["ip"] is None, "пока создаётся — без IP")
        r = await c.post(f"/api/svc/servers/{sid}/rename", headers=T, json={"tg_id": 1, "name": "de-1"})
        ok(r.status_code == 400, "переименовать до готовности нельзя — понятная ошибка")
        astra.ready = True
        v = (await c.get(f"/api/svc/servers/{sid}?tg_id=1", headers=T)).json()
        ok(v["status"] == "active" and v["ip"] == "198.51.100.5" and v["root_password"] == "Str0ngPassw0rd", "готов: IP и пароль")
        r = await c.post(f"/api/svc/servers/{sid}/rename", headers=T, json={"tg_id": 1, "name": "de-1"})
        ok(r.status_code == 200 and r.json()["name"] == "de-1", "переименование после готовности")
        ok((await c.get(f"/api/svc/servers/{sid}?tg_id=77", headers=T)).status_code == 404, "сервер не виден от имени другого клиента")

        r = await c.post("/api/svc/servers", headers=T, json={"tg_id": 1, "idx": 1, "location": "nl", "os": "debian12"})
        ok(r.status_code == 400 and "баланс" in r.json()["detail"].lower(), "не хватает баланса — понятная ошибка")

    os.environ["SERVICE_TOKEN"] = ""
    from app.config import env
    env.cache_clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        ok((await c.get("/api/svc/ping", headers=T)).status_code == 404, "без SERVICE_TOKEN API выключен")

    print(f"FAILS: {ok.fails}")


asyncio.run(main())
