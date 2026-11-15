"""Доп. IPv4 и IPv6: списание, ключ идемпотентности, итог операции, возврат при отказе, цены без наценки."""
import asyncio, os, sys
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from app import db as D, services as svc
from app.astra import AstraError


def ok(c, l):
    print(("PASS " if c else "FAIL ") + l); ok.f += 0 if c else 1
ok.f = 0


class FakeAstra:
    def __init__(s):
        s.card = {"id": 700, "status": "active", "ip": "1.1.1.1"}
        s.calls, s.status, s.fail = [], {"status": "completed", "new_ip": "9.9.9.9"}, None
    async def server(s, sid): return dict(s.card)
    async def add_ip(s, sid, key):
        s.calls.append(("add-ip", key))
        if s.fail: raise s.fail
        s.card["extra_ips"] = ["9.9.9.9"]
        return {"pending": True, "operation_id": "op1"}
    async def add_ip_status(s, sid, op): return s.status
    async def ipv6(s, sid, op, key):
        s.calls.append(("ipv6-" + op, key))
        if s.fail: raise s.fail
        s.card["ipv6"] = {"subnet": "2a01:db8::/64"} if op != "delete" else None
        return {"operation_id": "op6"}
    async def ipv6_status(s, sid, op): return {"operation": {"status": "completed"}}
    async def close(s): pass


msgs = []
async def notify(uid, t): msgs.append((uid, t))
async def alert(t): msgs.append(("admin", t))


async def bal(tg):
    async with D.session() as s:
        return (await s.get(D.User, tg)).balance_kop


async def main():
    await D.init_db()
    svc.watch_net.__defaults__ = (45, 0.01)  # итог операции — без 20-секундных пауз
    astra = FakeAstra()
    ctx = svc.Ctx(astra=astra, yk=None, nalog=None, notify=notify, alert_admin=alert)
    async with D.session() as s:
        s.add(D.User(tg_id=2, first_name="Иван", balance_kop=100000))
        s.add(D.User(tg_id=3, first_name="Друг", balance_kop=100000, no_markup=True))
        s.add(D.Server(astra_id=700, tg_id=2, name="srv", location="nl", cpu=1, ram=1, disk=10, os="ubuntu2404", price_kop=27000, status="active", ip="1.1.1.1"))
        s.add(D.Server(astra_id=701, tg_id=3, name="srv3", location="pl", cpu=1, ram=1, disk=10, os="ubuntu2404", price_kop=27000, status="active", ip="1.1.1.2"))
        await s.commit()

    r = await svc.net_order(ctx, 2, 1, "add-ip")
    await asyncio.sleep(0.2)
    ok(r["pending"] and await bal(2) == 100000 - 15000, "доп. IPv4: списано 150 ₽")
    ok(astra.calls[-1][0] == "add-ip" and astra.calls[-1][1].startswith("bc-add-ip-"), "запрос с ключом идемпотентности")
    ok(any(u == 2 and "9.9.9.9" in t for u, t in msgs), "клиент получил новый адрес")
    async with D.session() as s:
        ok(svc.ip_list((await s.get(D.Server, 1)).extra_ips) == ["9.9.9.9"], "адрес сохранён у сервера")

    astra.status = {"status": "cancelled", "refunded": True}
    await svc.net_order(ctx, 2, 1, "add-ip")
    await asyncio.sleep(0.2)
    ok(await bal(2) == 100000 - 15000, "Astra отменила операцию — деньги вернулись")
    ok(any(u == 2 and "не получилось" in t for u, t in msgs), "клиент узнал об отмене")

    r = await svc.net_order(ctx, 2, 1, "ipv6-add")
    await asyncio.sleep(0.2)
    ok(await bal(2) == 100000 - 15000 - 3000, "IPv6: списано 30 ₽")
    async with D.session() as s:
        ok((await s.get(D.Server, 1)).ipv6 == "2a01:db8::/64", "подсеть IPv6 сохранена")
    await svc.net_order(ctx, 2, 1, "ipv6-delete")
    await asyncio.sleep(0.2)
    ok(await bal(2) == 100000 - 15000 - 3000, "удаление IPv6 бесплатно")

    astra.fail = AstraError(409, {"error": "ipv6_unavailable"})
    try:
        await svc.net_order(ctx, 3, 2, "ipv6-add")
        ok(False, "IPv6 в Польше — ошибка")
    except svc.UserError as ex:
        ok("недоступен" in str(ex) and "возвращены" in str(ex), "IPv6 недоступен — понятная ошибка, деньги возвращены")
    ok(await bal(3) == 100000, "баланс клиента без наценки не изменился")
    astra.fail = None
    await svc.net_order(ctx, 3, 2, "add-ip")
    ok(await bal(3) == 100000 - 10000, "клиент без наценки платит цену Astra — 100 ₽")

    try:
        await svc.net_order(ctx, 3, 1, "add-ip")
        ok(False, "чужой сервер")
    except svc.UserError:
        ok(True, "чужой сервер — отказ")
    async with D.session() as s:
        u = await s.get(D.User, 2); u.balance_kop = 0; await s.commit()
    try:
        await svc.net_order(ctx, 2, 1, "add-ip")
        ok(False, "без денег")
    except svc.UserError as ex:
        ok("Нужно" in str(ex), "не хватает денег — отказ до запроса к Astra")
    print("FAILS:", ok.f)

asyncio.run(main())
