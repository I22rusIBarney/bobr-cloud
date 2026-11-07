"""Деньги при сбоях и гонках: таймаут автоплатежа, параллельные покупки со скидкой, двойное удаление,
повтор служебного списания, пароли в копии без ключа."""
import asyncio, os, sys, sqlite3, gzip
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  SERVICE_TOKEN="svc-token", DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
import httpx
from sqlalchemy import select, func
from app import db as D, services as svc, backup
from app.settings import save_settings
from app.web.server import build_app
from httpx import AsyncClient, ASGITransport

PRESETS = [{"idx": 0, "cpu": 1, "ram": 1, "disk": 10, "price_month_rub": {"nl": 100}}]
class FakeAstra:
    n = 100
    async def catalog(s): return {"locations": [{"code": "nl", "name": "NL", "available": True}], "presets": PRESETS, "os": ["ubuntu2404"]}
    async def create(s, **kw):
        await asyncio.sleep(0.05); s.n += 1
        return {"id": s.n, "spec": {"cpu": 1, "ram": 1, "disk": 10, "os": kw["os"]}, "expires_in_days": 30}
    async def delete(s, sid): await asyncio.sleep(0.05); return {"ok": True}
    async def server(s, sid): return {"id": sid, "status": "active"}
    async def action(s, *a, **k): return {}
    async def close(s): pass
class FakeYK:
    configured = True; shop_id = "1"; _secret = "x"
    def __init__(s): s.pays = {}; s.timeout = False
    async def charge_saved(s, **kw):
        pid = f"yk{len(s.pays) + 1}"
        s.pays[pid] = {"id": pid, "status": "succeeded", "paid": True, "metadata": {k: str(v) for k, v in kw["metadata"].items()},
                       "amount": {"value": f"{kw['amount_kop'] / 100:.2f}", "currency": "RUB"}, "payment_method": {"id": kw["method_id"], "saved": True}}
        if s.timeout:  # списание у ЮKassa прошло, а ответ до нас не дошёл
            raise httpx.ReadTimeout("timeout")
        return s.pays[pid]
    async def get_payment(s, pid): return s.pays[pid]
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

async def balance(uid):
    async with D.session() as s:
        return (await s.get(D.User, uid)).balance_kop

async def main():
    await D.init_db()
    ctx = svc.Ctx(astra=FakeAstra(), yk=FakeYK(), nalog=FakeNalog(), notify=notify, alert_admin=alert)
    app = build_app(ctx)
    async with D.session() as s:
        await save_settings(s, {"autopay_available": True, "round_to_rub": 1, "markup_pct": 0})
        for uid in (2, 3, 4, 5):
            u = await D.get_or_create_user(s, uid, f"u{uid}")
            u.consent_version, u.balance_kop = 1, 100000
        u2 = await s.get(D.User, 2)
        u2.autopay_enabled = True
        s.add(D.PaymentMethod(tg_id=2, yk_id="pm_1", title="Card *4444"))
        await s.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        # 1. автоплатёж: ЮKassa списала, ответ потерян — платёж ждёт, вебхук зачисляет по номеру из metadata
        ctx.yk.timeout = True
        st = await svc.charge_autopay(ctx, 2, 5000, "vps")
        async with D.session() as s:
            pay = (await s.execute(select(D.Payment).where(D.Payment.autopay.is_(True)))).scalars().one()
        ok(st == "pending" and pay.status == "pending" and pay.yk_id is None, "таймаут автоплатежа: платёж не отменён, ждёт уведомления")
        before = await balance(2)
        r = await c.post("/yookassa/webhook/sec", json={"event": "payment.succeeded", "object": {"id": "yk1", "metadata": {"payment_id": str(pay.id + 1)}}})
        ok(r.status_code == 200 and await balance(2) == before, "чужой номер в metadata не зачисляется")
        r = await c.post("/yookassa/webhook/sec", json={"event": "payment.succeeded", "object": {"id": "yk1", "metadata": {"payment_id": str(pay.id)}}})
        ok(r.status_code == 200 and await balance(2) == before + pay.amount_kop, "вебхук нашёл платёж по номеру и зачислил")
        await c.post("/yookassa/webhook/sec", json={"event": "payment.succeeded", "object": {"id": "yk1", "metadata": {"payment_id": str(pay.id)}}})
        ok(await balance(2) == before + pay.amount_kop, "повторное уведомление не зачисляет второй раз")
        r = await c.post("/yookassa/webhook/wrong", json={})
        ok(r.status_code == 403, "неверный секрет вебхука — 403")

        # 2. скидка из промокода — на одну покупку, даже если купить дважды одновременно
        async with D.session() as s:
            u = await s.get(D.User, 3); u.promo_discount_pct, u.promo_code = 50, "HALF"; await s.commit()
        await asyncio.gather(*(svc.buy_server(ctx, 3, 0, "nl", "ubuntu2404") for _ in range(2)))
        ok(100000 - await balance(3) == 5000 + 10000, "две параллельные покупки: скидка 50% только у одной")

        # 3. двойное удаление — возврат один раз
        srv = await svc.buy_server(ctx, 4, 0, "nl", "ubuntu2404")
        res = await asyncio.gather(*(svc.cancel_server(ctx, 4, srv.id) for _ in range(2)), return_exceptions=True)
        async with D.session() as s:
            n = (await s.execute(select(func.count()).select_from(D.Tx).where(D.Tx.tg_id == 4, D.Tx.kind == "refund_cancel"))).scalar_one()
        ok(n == 1, f"двойное удаление: возврат один раз ({res})")

        # 4. служебное списание: два одинаковых запроса одновременно — одно списание
        H = {"Authorization": "Bearer svc-token"}
        rs = await asyncio.gather(*(c.post("/api/svc/charge", headers=H, json={"tg_id": 5, "amount_kop": 1000, "ref": "sub-1"}) for _ in range(3)))
        ok(all(r.status_code == 200 for r in rs) and await balance(5) == 99000, "повтор /charge с тем же ref не списывает второй раз")

        # 5. заголовки безопасности
        r = await c.get("/paid")
        ok(r.headers.get("x-content-type-options") == "nosniff" and "max-age" in r.headers.get("strict-transport-security", ""), "заголовки безопасности на месте")

    # 6. копия без ключа — без root-паролей серверов клиентов
    async with D.session() as s:
        row = (await s.execute(select(D.Server).where(D.Server.tg_id == 3))).scalars().first()
        row.root_password = "S3cret-root"; await s.commit()
    out = os.path.join(TMP, "bk"); os.makedirs(out, exist_ok=True)
    gz = backup._make(backup.db_path(), out)
    raw = os.path.join(out, "check.db")
    with gzip.open(gz) as f, open(raw, "wb") as g:
        g.write(f.read())
    pw = sqlite3.connect(raw).execute("SELECT count(*) FROM servers WHERE root_password IS NOT NULL").fetchone()[0]
    ok(pw == 0, "в копии без ключа нет root-паролей")
    ok(sqlite3.connect(backup.db_path()).execute("PRAGMA journal_mode").fetchone()[0] == "wal", "база работает в режиме WAL")

    print(f"FAILS: {ok.fails}")
    sys.exit(1 if ok.fails else 0)

asyncio.run(main())
