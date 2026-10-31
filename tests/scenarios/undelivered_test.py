"""Astra не выдала сервер: деньги клиенту возвращаются, даже если статус успел смениться; рабочий пропавший — без возврата."""
import asyncio, os, sys
from datetime import timedelta
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from app import db as D, services as svc


class FakeAstra:
    async def servers(s): return []
    async def close(s): pass


class Stub:
    configured = False; enabled = False
    async def close(s): pass


sent, alerts = [], []
async def notify(uid, t): sent.append((uid, t))
async def alert(t): alerts.append(t)


def ok(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond: ok.fails += 1
ok.fails = 0


async def bal(tg):
    async with D.session() as s:
        return (await s.get(D.User, tg)).balance_kop


async def main():
    await D.init_db()
    ctx = svc.Ctx(astra=FakeAstra(), yk=Stub(), nalog=Stub(), notify=notify, alert_admin=alert)
    old = D.utcnow() - timedelta(minutes=10)
    async with D.session() as s:
        s.add(D.User(tg_id=7, username="c"))
        await s.commit()
        for aid, st, ip, paid in [(101, "provisioning", None, 30000),  # обычный провал создания
                                  (102, "active", None, 20000),        # статус сменился опросом, IP так и не было
                                  (103, "active", "198.51.100.3", 40000)]:  # рабочий сервер пропал — это не провал
            s.add(D.Server(astra_id=aid, tg_id=7, name=f"s{aid}", location="nl", price_kop=paid, status=st, ip=ip,
                           period_paid_kop=paid, created_at=old))
        # уже удалён как «пропавший» без возврата (старая логика) и один — с возвратом
        s.add(D.Server(astra_id=104, tg_id=7, name="s104", location="nl", price_kop=15000, status="deleted", ip=None,
                       period_paid_kop=15000, created_at=old, deleted_at=D.utcnow() - timedelta(hours=1)))
        s.add(D.Server(astra_id=105, tg_id=7, name="s105", location="nl", price_kop=15000, status="deleted", ip=None,
                       period_paid_kop=15000, created_at=old, deleted_at=D.utcnow() - timedelta(hours=1)))
        s.add(D.Tx(tg_id=7, amount_kop=15000, kind="refund_cancel", ref="105"))
        await s.commit()
    await svc.sync_servers(ctx)
    ok(await bal(7) == 50000, f"не выданные Astra серверы — деньги клиенту: {await bal(7)}")
    ok(any("пропал" in a and "#103" in a for a in alerts) and not any("#101" in a or "#102" in a for a in alerts if "пропал" in a),
       "рабочий пропавший сервер — сообщение админу, не выданные — нет")
    await svc.sync_servers(ctx)
    ok(await bal(7) == 50000, "второй проход — без повторного возврата")
    n = await svc.refund_undelivered(ctx)
    ok(n == 1 and await bal(7) == 65000, f"разовая починка: вернул за удалённый без возврата, остальных не трогает: {n}, {await bal(7)}")
    ok(await svc.refund_undelivered(ctx) == 0 and await bal(7) == 65000, "починка повторно ничего не возвращает")
    ok(any(uid == 7 and "Возврат" in t for uid, t in sent), "клиенту — сообщение о возврате")
    print(f"FAILS: {ok.fails}")


asyncio.run(main())
