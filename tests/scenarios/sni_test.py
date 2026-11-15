"""Ограничения за чужой SNI и проверка доступной системы перед покупкой."""
import asyncio, os, sys
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from app import db as D, services as svc


def ok(c, l):
    print(("PASS " if c else "FAIL ") + l); ok.f += 0 if c else 1
ok.f = 0

PRESETS = [{"idx": 0, "cpu": 2, "ram": 4, "disk": 60, "price_month_rub": {"nl": 188}}]


class FakeAstra:
    def __init__(s):
        s.acts, s.avail = [], ["ubuntu2404"]
        s.incidents = [
            {"incident_id": "inc-1", "service_id": 900, "action_required": "report_correction", "grace_until": "2030-01-01T00:00:00Z", "binding_current": True},
            {"incident_id": "inc-old", "service_id": 900, "action_required": "contact_support", "binding_current": False},
            {"incident_id": "inc-closed", "service_id": 900, "is_closed": True},
        ]
    async def sni_incidents(s, service_id, status="open"): return [x for x in s.incidents if x["service_id"] == service_id]
    async def sni_act(s, iid, act, body=None): s.acts.append((iid, act, body)); return {"status": "queued"}
    async def os_available(s): return s.avail
    async def catalog(s): return {"locations": [{"code": "nl", "name": "NL", "available": True}], "presets": PRESETS, "os": ["ubuntu2404", "windows2022"]}
    async def create(s, **kw): return {"id": 950, "spec": {"cpu": 2, "ram": 4, "disk": 60, "os": kw["os"]}, "charged_rub": 188, "expires_in_days": 30}
    async def server(s, sid): return {"id": sid, "status": "active"}
    async def close(s): pass


msgs = []
async def notify(uid, t): msgs.append((uid, t))
async def alert(t): msgs.append(("admin", t))


async def main():
    await D.init_db()
    astra = FakeAstra()
    ctx = svc.Ctx(astra=astra, yk=None, nalog=None, notify=notify, alert_admin=alert)
    async with D.session() as s:
        s.add(D.User(tg_id=2, first_name="Иван", balance_kop=100000))
        s.add(D.User(tg_id=3, first_name="Чужой", balance_kop=100000))
        s.add(D.Server(astra_id=900, tg_id=2, name="vpn", location="nl", cpu=1, ram=1, disk=10, os="ubuntu2404", price_kop=27000, status="active", ip="1.1.1.1"))
        await s.commit()

    lst = await svc.sni_list(ctx, 2, 1)
    ok([x["incident_id"] for x in lst] == ["inc-1"], "видно только открытое ограничение по текущей привязке")
    try:
        await svc.sni_list(ctx, 3, 1); ok(False, "чужой сервер")
    except svc.UserError:
        ok(True, "чужой сервер — отказ")
    for bad in (["https://site.com/x"], [], ["a.ru"] * 6, ["not a domain"]):
        try:
            await svc.sni_act(ctx, 2, 1, "inc-1", "correction", bad); ok(False, f"домены {bad}")
        except svc.UserError:
            pass
    ok(not astra.acts, "неверные домены не уходят к провайдеру")
    await svc.sni_act(ctx, 2, 1, "inc-1", "correction", ["VPN.Example.com.", "my.site"])
    ok(astra.acts[-1] == ("inc-1", "correction", {"domains": ["vpn.example.com", "my.site"]}), "домены нормализованы и отправлены")
    try:
        await svc.sni_act(ctx, 2, 1, "inc-closed", "dispute"); ok(False, "закрытый")
    except svc.UserError:
        ok(True, "закрытое или чужое ограничение — отказ")
    await svc.sni_act(ctx, 2, 1, "inc-1", "dispute")
    ok(astra.acts[-1][1] == "dispute" and any(u == "admin" for u, _ in msgs), "оспаривание ушло провайдеру, админ в курсе")

    # система закончилась у провайдера — покупка отклоняется до списания
    async with D.session() as s:
        from app.settings import load_settings, save_settings
        await save_settings(s, {"maintenance": False})
    astra.avail = ["ubuntu2404"]
    try:
        await svc.buy_server(ctx, 2, 0, "nl", "windows2022"); ok(False, "windows недоступен")
    except svc.UserError as ex:
        ok("недоступна у провайдера" in str(ex), "недоступная система — понятный отказ")
    async with D.session() as s:
        ok((await s.get(D.User, 2)).balance_kop == 100000, "деньги не списаны")
    astra.avail = []  # проверка не ответила — покупке не мешаем
    srv = await svc.buy_server(ctx, 2, 0, "nl", "ubuntu2404")
    ok(srv.astra_id == 950, "без ответа о системах покупка проходит")
    print("FAILS:", ok.f)

asyncio.run(main())
