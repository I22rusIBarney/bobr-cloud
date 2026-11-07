"""Деньги в редких случаях: возврат платежа и чарджбэк, потерянный ответ ЮKassa (сверка по списку платежей),
бесплатное продление, возврат участнику общего доступа, пароли после восстановления, события хоста."""
import asyncio, os, sys, json
from datetime import timedelta
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from sqlalchemy import select, func
from app import db as D, services as svc, security
from app.settings import save_settings
from app.web.server import build_app
from httpx import AsyncClient, ASGITransport

PRESETS = [{"idx": 0, "cpu": 1, "ram": 1, "disk": 10, "price_month_rub": {"nl": 300}}]
class FakeAstra:
    n = 100
    async def catalog(s): return {"locations": [{"code": "nl", "name": "NL", "available": True}], "presets": PRESETS, "os": ["ubuntu2404"]}
    async def create(s, **kw):
        s.n += 1
        return {"id": s.n, "spec": {"cpu": 1, "ram": 1, "disk": 10, "os": kw["os"]}, "expires_in_days": 30}
    async def delete(s, sid): return {"ok": True}
    async def server(s, sid): return {"id": sid, "status": "active", "root_password": f"pw-{sid}", "ip": f"10.0.0.{sid % 250}"}
    async def action(s, *a, **k): return {}
    async def close(s): pass
class FakeYK:
    configured = True; shop_id = "1"; _secret = "x"
    def __init__(s): s.pays, s.refunds = {}, {}
    def add(s, pid, local_id, kop, status="succeeded"):
        s.pays[pid] = {"id": pid, "status": status, "paid": status == "succeeded", "metadata": {"payment_id": str(local_id)},
                       "amount": {"value": f"{kop / 100:.2f}", "currency": "RUB"}, "payment_method": {}}
    async def get_payment(s, pid): return s.pays[pid]
    async def get_refund(s, rid): return s.refunds[rid]
    async def list_payments(s, since, cursor=None): return {"items": list(s.pays.values())}
    async def close(s): pass
class FakePlatega:
    configured = True; merchant_id = "m"; _secret = "s"
    def __init__(s): s.st = {}
    def verify_callback(s, m, k): return True
    async def get_payment(s, tid):
        raw = s.st[tid]
        status = "succeeded" if raw == "CONFIRMED" else "canceled"
        return {"id": tid, "status": status, "paid": status == "succeeded", "amount": {"value": "500.00", "currency": "RUB"},
                "cancellation_details": {"reason": raw.lower()} if status == "canceled" else None, "payment_method": {}, "raw_status": raw}
    async def close(s): pass
class FakeNalog:
    enabled = False
    async def close(s): pass
msgs = []
async def notify(uid, t, *a): msgs.append((uid, t))
async def alert(t): msgs.append(("admin", t))

def ok(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond: ok.fails += 1
ok.fails = 0

async def user(uid):
    async with D.session() as s:
        return await s.get(D.User, uid)

async def pay_row(uid, kop, provider="yookassa", yk_id=None, status="pending", ago=0):
    async with D.session() as s:
        p = D.Payment(tg_id=uid, amount_kop=kop, provider=provider, yk_id=yk_id, status=status, created_at=D.utcnow() - timedelta(minutes=ago))
        s.add(p); await s.commit()
        return p

async def main():
    await D.init_db()
    ctx = svc.Ctx(astra=FakeAstra(), yk=FakeYK(), nalog=FakeNalog(), notify=notify, alert_admin=alert, platega=FakePlatega())
    app = build_app(ctx)
    async with D.session() as s:
        await save_settings(s, {"round_to_rub": 1, "markup_pct": 0, "refund_pct": 100})
        for uid in range(2, 9):
            u = await D.get_or_create_user(s, uid, f"u{uid}")
            u.consent_version, u.balance_kop = 1, 100000
        await s.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        # 1. возврат платежа в ЮKassa: деньги списываются (баланс может уйти в минус), клиент блокируется, повтор не списывает
        p = await pay_row(2, 150000, yk_id="ykR", status="succeeded")
        ctx.yk.add("ykR", p.id, 150000)
        ctx.yk.refunds["rf1"] = {"id": "rf1", "payment_id": "ykR", "status": "succeeded", "amount": {"value": "1500.00", "currency": "RUB"}}
        ctx.yk.refunds["rf0"] = {"id": "rf0", "payment_id": "ykR", "status": "pending", "amount": {"value": "1500.00", "currency": "RUB"}}
        await c.post("/yookassa/webhook/sec", json={"event": "refund.succeeded", "object": {"id": "rf0", "payment_id": "ykR"}})
        ok((await user(2)).balance_kop == 100000, "незавершённый возврат ничего не списывает")
        for _ in range(2):
            r = await c.post("/yookassa/webhook/sec", json={"event": "refund.succeeded", "object": {"id": "rf1", "payment_id": "ykR"}})
        u = await user(2)
        ok(r.status_code == 200 and u.balance_kop == 100000 - 150000 and u.banned and not u.autopay_enabled,
           f"возврат: списано один раз, баланс в минусе, клиент заблокирован ({u.balance_kop})")
        ok(any(m[0] == "admin" and "возврат" in m[1].lower() for m in msgs), "админ получил алерт о возврате")

        # 2. чарджбэк Platega по уже зачисленному платежу
        p = await pay_row(3, 50000, provider="platega", yk_id="pl1", status="succeeded")
        ctx.platega.st["pl1"] = "CHARGEBACK"
        await c.post("/platega/webhook", json={"id": "pl1"})
        await c.post("/platega/webhook", json={"id": "pl1"})
        u = await user(3)
        ok(u.balance_kop == 50000 and u.banned, f"чарджбэк Platega: списано один раз и клиент заблокирован ({u.balance_kop})")

        # 3. сверка: ответ ЮKassa на создание платежа потерялся, вебхука нет — опрос находит платёж по номеру
        p = await pay_row(4, 30000, ago=5)
        ctx.yk.add("ykLost", p.id, 30000)
        old = await pay_row(4, 20000, ago=60 * 26)  # у ЮKassa его нет совсем — закрываем, чтобы не висел
        await svc.poll_pending_payments(ctx)
        async with D.session() as s:
            a, b = await s.get(D.Payment, p.id), await s.get(D.Payment, old.id)
        ok(a.status == "succeeded" and a.yk_id == "ykLost" and (await user(4)).balance_kop == 130000, "опрос нашёл потерянный платёж и зачислил")
        ok(b.status == "canceled" and b.cancel_reason == "not_created", "платёж, которого нет у ЮKassa, закрыт через сутки")

    # 4. бесплатное продление просроченного сервера — возврата за неоплаченный месяц нет
    srv = await svc.buy_server(ctx, 5, 0, "nl", "ubuntu2404")
    async with D.session() as s:
        row = await s.get(D.Server, srv.id); row.status, row.expires_at = "active", D.utcnow() - timedelta(days=2); await s.commit()
    await svc.renew_server(ctx, srv.id, free=True, actor=1)
    before = (await user(5)).balance_kop
    refund = await svc.cancel_server(ctx, 5, srv.id)
    ok(refund == 0 and (await user(5)).balance_kop == before, f"бесплатный месяц не возвращается деньгами ({refund})")

    # 5. участник продлил сервер владельца — при удалении его часть возврата уходит ему
    srv = await svc.buy_server(ctx, 6, 0, "nl", "ubuntu2404")
    async with D.session() as s:
        row = await s.get(D.Server, srv.id); row.status = "active"
        s.add(D.ServerShare(server_id=srv.id, owner_tg=6, member_tg=7, status="active")); await s.commit()
    await svc.renew_server(ctx, srv.id, payer=7)
    o0, m0 = (await user(6)).balance_kop, (await user(7)).balance_kop
    refund = await svc.cancel_server(ctx, 6, srv.id)
    got_o, got_m = (await user(6)).balance_kop - o0, (await user(7)).balance_kop - m0
    ok(got_m > 25000 and got_o > 25000 and refund == got_o, f"возврат поделён: владельцу {got_o}, участнику {got_m}")

    # 6. после восстановления копии без ключа пароли root подтягиваются из Astra
    srv = await svc.buy_server(ctx, 8, 0, "nl", "ubuntu2404")
    async with D.session() as s:
        row = await s.get(D.Server, srv.id); row.status, row.root_password = "active", None; await s.commit()
    n = await svc.refill_passwords(ctx)
    async with D.session() as s:
        row = await s.get(D.Server, srv.id)
    ok(n == 1 and row.root_password == f"pw-{srv.astra_id}", "пароль root снова на месте после восстановления")

    # 7. события хоста: файл пишет root — бот читает его, даже не имея права на запись в сам файл
    path = security.events_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps({"type": "login", "user": "root", "ip": "1.2.3.4", "ts": 1}) + "\n")
    os.chmod(path, 0o444)
    ev = security._read_and_clear()
    ok(len(ev) == 1 and not os.path.exists(path), "события хоста прочитаны и убраны без права записи в файл")

    print(f"FAILS: {ok.fails}")
    sys.exit(1 if ok.fails else 0)

asyncio.run(main())
