import asyncio, os, sys, json, time, hmac, hashlib
from urllib.parse import urlencode
from datetime import timedelta
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from app import db as D, services as svc
from app.web.server import build_app
from httpx import AsyncClient, ASGITransport

def init(uid, name="Иван"):
    f = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": name, "username": f"user{uid}"}), "query_id": "q"}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
    sec = hmac.new(b"WebAppData", b"123:TEST", hashlib.sha256).digest()
    return urlencode({**f, "hash": hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest()})

PRESETS = [{"idx": 0, "cpu": 1, "ram": 1, "disk": 10, "price_month_rub": {"nl": 188}}, {"idx": 1, "cpu": 2, "ram": 4, "disk": 60, "price_month_rub": {"nl": 675}}]
class FakeAstra:
    def __init__(s): s.n = 100
    async def catalog(s): return {"locations": [{"code": "nl", "name": "🇳🇱 Нидерланды", "available": True}], "presets": PRESETS, "os": ["ubuntu2404"]}
    async def balance(s): return {"balance_rub": 5000, "discount_pct": 25}
    async def create(s, **kw): s.n += 1; s.last = kw; return {"id": s.n, "spec": {"cpu": 1, "ram": 1, "disk": 10, "os": kw["os"]}, "charged_rub": 188, "expires_in_days": 30}
    async def servers(s): return []
    async def server(s, sid): return {"id": sid, "status": "active", "price_month_rub": 188}
    async def action(s, sid, a, body=None): s.last_action = (a, body); return {"ok": True}
    async def delete(s, sid): return {"ok": True}
    async def close(s): pass
class FakeYK:
    configured = True; shop_id = "1"; _secret = "x"
    def __init__(s): s.pays = {}; s.n = 0
    async def create_payment(s, **kw):
        s.n += 1; pid = f"yk{s.n}"; s.pays[pid] = {"id": pid, "status": "succeeded", "paid": True, "amount": {"value": f"{kw['amount_kop']/100:.2f}", "currency": "RUB"},
            "payment_method": {"id": "pm_1", "saved": bool(kw.get("save_payment_method")), "type": "bank_card", "title": "Bank card *4444", "card": {"last4": "4444", "card_type": "MasterCard", "expiry_month": "12", "expiry_year": "2029"}}}
        s.last = kw; return {"id": pid, "confirmation": {"confirmation_url": "https://pay/" + pid}}
    async def charge_saved(s, **kw):
        s.n += 1; pid = f"yk{s.n}"; s.pays[pid] = {"id": pid, "status": "succeeded", "paid": True, "amount": {"value": f"{kw['amount_kop']/100:.2f}", "currency": "RUB"}, "payment_method": {"id": kw["method_id"], "saved": True}}
        s.charged = kw; return s.pays[pid]
    async def get_payment(s, pid): return s.pays[pid]
    async def close(s): pass
class FakeNalog:
    enabled = False
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
    ctx = svc.Ctx(astra=FakeAstra(), yk=FakeYK(), nalog=FakeNalog(), notify=notify, alert_admin=alert)
    ctx.bot_username = "bobrcloud_bot"
    cat0 = ctx.astra.catalog
    async def cat(): c = await cat0(); c["periods"] = ["1m", "3m", "6m", "1y"]; return c
    ctx.astra.catalog = cat
    app = build_app(ctx)
    from app.settings import save_settings
    UID = 900
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        H = {"X-Init-Data": init(UID)}; await c.get("/api/me", headers=H); await c.post("/api/me/consent", headers=H, json={"docs": ["terms", "aup", "privacy", "pd"]})
        async with D.session() as s: await D.change_balance(s, UID, 3000000, "admin_adjust")
        cj = (await c.get("/api/me/catalog", headers=H)).json()
        ok([p["code"] for p in cj["periods"]] == ["1m", "3m", "6m", "1y"] and cj["periods"][3]["pct"] == 15, "сроки и скидки в каталоге кабинета")
        month = cj["locations"][0]["offers"][1]["price_kop"]
        r = await c.post("/api/me/servers", headers=H, json={"idx": 1, "location": "nl", "os": "ubuntu2404", "period": "1y"})
        ok(r.status_code == 200, "покупка на год " + r.text[:100])
        ok(ctx.astra.last["period"] == "1y", "Astra получила срок 1y")
        expect = -(-month * 12 * 85 // 100 // 100) * 100
        async with D.session() as s:
            from sqlalchemy import select
            srv = (await s.execute(select(D.Server).where(D.Server.tg_id == UID))).scalars().first()
            u = await s.get(D.User, UID)
        ok(srv.period_paid_kop == expect and srv.period_months == 12 and srv.price_kop == month, f"списано {srv.period_paid_kop/100} = 12 мес −15% ({expect/100}), продление по {month/100}")
        ok(u.balance_kop == 3000000 - expect, "баланс уменьшился на сумму за год")
        async with D.session() as s:
            row = await s.get(D.Server, srv.id); row.expires_at = D.utcnow() + timedelta(days=360); row.status = "active"; await s.commit()
            row = await s.get(D.Server, srv.id)
        ref = svc.refund_amount(svc.paid_base(row), row.expires_at, 100, svc.paid_months(row))
        ok(abs(ref - expect * 12 / 12 * 360 / 360) < expect * 0.02 and ref > expect * 0.95, f"возврат 100% за 360 дней ≈ оплаченному ({ref/100} из {expect/100})")
        await svc.renew_server(ctx, srv.id)
        async with D.session() as s: row = await s.get(D.Server, srv.id)
        ok(abs(row.period_months - 13) < 0.05 and abs(row.period_paid_kop - (expect + month)) < month * 0.03, f"досрочное продление: {row.period_months:.2f} мес, {row.period_paid_kop/100}")
        r = await c.post("/api/me/servers", headers=H, json={"idx": 1, "location": "nl", "os": "ubuntu2404", "period": "2y"})
        ok(r.status_code == 400, "неизвестный срок отклонён")
        async with D.session() as s: await save_settings(s, {"periods_enabled": False})
        cj = (await c.get("/api/me/catalog", headers=H)).json(); ok([p["code"] for p in cj["periods"]] == ["1m"], "сроки можно выключить")
        r = await c.post("/api/me/servers", headers=H, json={"idx": 1, "location": "nl", "os": "ubuntu2404", "period": "3m"})
        ok(r.status_code == 400, "выключенный срок не купить")
        r = await c.post("/api/me/servers", headers=H, json={"idx": 0, "location": "nl", "os": "ubuntu2404"})
        ok(r.status_code == 200 and ctx.astra.last["period"] == "1m", "обычная покупка на месяц работает как раньше")
    print("FAILS:", ok.fails)

asyncio.run(main())
