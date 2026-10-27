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
    CALLS = []
    orig = ctx.yk.create_payment
    REJECT = {"sbp": False}
    async def cp(**kw):
        CALLS.append(kw)
        if REJECT["sbp"] and kw.get("method") == "sbp":
            raise svc.YooKassaError(400, {"description": "payment_method_data not allowed"})
        return await orig(**kw)
    ctx.yk.create_payment = cp
    app = build_app(ctx)
    from app.settings import save_settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        H = {"X-Init-Data": init(41)}
        await c.get("/api/me", headers=H); await c.post("/api/me/consent", headers=H, json={"docs": ["terms", "aup", "privacy", "pd"]})
        j = (await c.get("/api/me", headers=H)).json()
        p = j["cfg"]["providers"][0]
        ok(p["label"] == "СБП · ЮKassa" and p["autopay"] is False, "по умолчанию «Только СБП», подпись подстроилась, без сохранения карты")
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 500, "save_method": True})
        ok(r.status_code == 200 and CALLS[-1]["method"] == "sbp" and not CALLS[-1].get("save_payment_method"), "платёж ЮKassa сразу через СБП, карта не сохраняется")
        async with D.session() as s: await save_settings(s, {"yk_method": "all"})
        await c.post("/api/me/topup", headers=H, json={"amount_rub": 500})
        ok(CALLS[-1]["method"] is None, "«Все способы» — страница выбора ЮKassa")
        async with D.session() as s: await save_settings(s, {"yk_method": "bank_card", "autopay_available": True})
        j = (await c.get("/api/me", headers=H)).json(); ok(j["cfg"]["providers"][0]["autopay"] and j["cfg"]["providers"][0]["label"] == "Банковская карта · ЮKassa", "«Только карта» — можно сохранить карту")
        await c.post("/api/me/topup", headers=H, json={"amount_rub": 500, "save_method": True})
        ok(CALLS[-1]["method"] == "bank_card" and CALLS[-1]["save_payment_method"], "карта с сохранением для автоплатежей")
        async with D.session() as s: await save_settings(s, {"yk_method": "sbp", "pay_yookassa_label": "Быстрая оплата СБП"})
        j = (await c.get("/api/me", headers=H)).json(); ok(j["cfg"]["providers"][0]["label"] == "Быстрая оплата СБП", "своя подпись сохраняется")
        REJECT["sbp"] = True; n = len(CALLS)
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 500})
        ok(r.status_code == 200 and CALLS[-1]["method"] is None and len(CALLS) == n + 2, "СБП не подключён в магазине — клиент всё равно может оплатить")
        ok(any("СБП" in str(m) or "sbp" in str(m) for m in msgs), "админ получил предупреждение")
        r = await c.post("/api/admin/settings", headers={"X-Init-Data": init(1)}, json={"values": {"yk_method": "crypto"}}); ok(r.status_code == 400, "неизвестный способ отклонён")
    print("FAILS:", ok.fails)

asyncio.run(main())
