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
    enabled = False; inn = ""; _password = ""
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
    import httpx, json as J
    from app.platega import Platega, API
    TX = {}
    SEEN = {"v2": 0, "v1": 0, "auth": []}
    MODE = {"v2_404": False}
    def handler(req: httpx.Request):
        SEEN["auth"].append((req.headers.get("X-MerchantId"), req.headers.get("X-Secret")))
        if req.headers.get("X-Secret") != "sek":
            return httpx.Response(401, json={"message": "Unauthorized"})
        p = req.url.path
        if req.method == "POST" and p == "/v2/transaction/process":
            if MODE["v2_404"]: return httpx.Response(404)
            SEEN["v2"] += 1; b = J.loads(req.content)
            tid = f"tx-{len(TX)+1}"; TX[tid] = {"status": "PENDING", "amount": b["paymentDetails"]["amount"], "payload": b["payload"]}
            SEEN["last_body"] = b
            return httpx.Response(200, json={"transactionId": tid, "status": "PENDING", "url": f"https://pay.platega.io/{tid}", "expiresIn": "00:15:00"})
        if req.method == "POST" and p == "/transaction/process":
            SEEN["v1"] += 1; b = J.loads(req.content); SEEN["last_body"] = b
            tid = f"tx-{len(TX)+1}"; TX[tid] = {"status": "PENDING", "amount": b["paymentDetails"]["amount"]}
            return httpx.Response(200, json={"transactionId": tid, "status": "PENDING", "redirect": f"https://pay.platega.io/{tid}"})
        if req.method == "GET" and p.startswith("/transaction/"):
            tid = p.rsplit("/", 1)[1]
            if tid not in TX: return httpx.Response(404, json={"message": "not found"})
            t = TX[tid]
            return httpx.Response(200, json={"id": tid, "status": t["status"], "paymentDetails": {"amount": t["amount"], "currency": "RUB"}})
        return httpx.Response(404)
    pg = Platega(); ctx.platega = pg
    def configure(mid, sec):
        Platega.configure(pg, mid, sec)
        if pg.configured:
            pg._http = httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handler), headers={"X-MerchantId": mid, "X-Secret": sec})
    pg.configure = configure
    app = build_app(ctx)
    from app.settings import save_settings
    DOCS4 = {"docs": ["terms", "aup", "privacy", "pd"]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        HA = {"X-Init-Data": init(1, "Админ")}; H = {"X-Init-Data": init(31, "Олег")}
        await c.get("/api/me", headers=H); await c.post("/api/me/consent", headers=H, json=DOCS4)
        j = (await c.get("/api/me", headers=H)).json(); ok([p["code"] for p in j["cfg"]["providers"]] == ["yookassa"], "по умолчанию — только ЮKassa")
        r = (await c.post("/api/admin/integrations/platega", headers=HA, json={"merchant_id": "m-1", "secret": "bad"})).json()
        ok(not r["ok"] and "не приняла" in r["message"], "неверный ключ Platega распознан")
        r = (await c.post("/api/admin/integrations/platega", headers=HA, json={"merchant_id": "m-1", "secret": "sek"})).json()
        ok(r["ok"], "ключи Platega сохранены и проверены")
        info = (await c.get("/api/admin/integrations", headers=HA)).json()
        ok(info["platega"]["configured"] and info["platega"]["callback_url"].endswith("/platega/webhook") and "sek" not in json.dumps(info), "в админке — статус и адрес уведомлений, секрет скрыт")
        j = (await c.get("/api/me", headers=H)).json(); ok(len(j["cfg"]["providers"]) == 1, "подключённая, но выключенная Platega клиенту не видна")
        async with D.session() as s: await save_settings(s, {"pay_platega_on": True})
        j = (await c.get("/api/me", headers=H)).json(); ok([p["code"] for p in j["cfg"]["providers"]] == ["yookassa", "platega"], "включены оба — клиент выбирает")
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 1500, "save_method": True, "provider": "platega"})
        ok(r.status_code == 200 and r.json()["url"].startswith("https://pay.platega.io/"), "пополнение через Platega — ссылка на оплату")
        b = SEEN["last_body"]; ok(b["paymentDetails"] == {"amount": 1500, "currency": "RUB"} and b["return"].endswith("/paid") and "paymentMethod" not in b, "запрос v2: сумма, возврат, способ выбирает клиент")
        async with D.session() as s:
            from sqlalchemy import select
            pay = (await s.execute(select(D.Payment).where(D.Payment.tg_id == 31))).scalars().first()
        ok(pay.provider == "platega" and pay.yk_id == "tx-1" and not pay.save_method, "платёж Platega записан, карта не сохраняется")
        r = await c.post("/platega/webhook", json={"id": "tx-1", "status": "CONFIRMED"}, headers={"X-MerchantId": "m-1", "X-Secret": "wrong"})
        ok(r.status_code == 401, "поддельное уведомление отклонено")
        r = await c.post("/platega/webhook", json={"id": "tx-1", "status": "CONFIRMED"}, headers={"X-MerchantId": "m-1", "X-Secret": "sek"})
        async with D.session() as s: u = await s.get(D.User, 31)
        ok(r.status_code == 200 and u.balance_kop == 0, "уведомление «оплачено», но Platega говорит PENDING — не зачисляем")
        TX["tx-1"]["status"] = "CONFIRMED"
        r = await c.post("/platega/webhook", json={"id": "tx-1", "status": "CONFIRMED"}, headers={"X-MerchantId": "m-1", "X-Secret": "sek"})
        await c.post("/platega/webhook", json={"id": "tx-1", "status": "CONFIRMED"}, headers={"X-MerchantId": "m-1", "X-Secret": "sek"})
        async with D.session() as s: u = await s.get(D.User, 31)
        ok(u.balance_kop == 150000, "подтверждённый платёж зачислен один раз")
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 700, "provider": "platega"})
        TX["tx-2"]["status"] = "CONFIRMED"
        await svc.poll_pending_payments(ctx)
        async with D.session() as s: u = await s.get(D.User, 31)
        ok(u.balance_kop == 220000, "без уведомления — зачислено опросом")
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 500, "provider": "platega"}); TX["tx-3"]["status"] = "CANCELED"
        await svc.poll_pending_payments(ctx)
        async with D.session() as s: p3 = (await s.execute(select(D.Payment).where(D.Payment.yk_id == "tx-3"))).scalar_one()
        ok(p3.status == "canceled", "отменённый платёж помечен")
        MODE["v2_404"] = True
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 300, "provider": "platega"})
        ok(r.status_code == 200 and SEEN["v1"] == 1 and SEEN["last_body"]["paymentMethod"] == 2, "старое API (v1) — запасной путь через СБП")
        MODE["v2_404"] = False
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 300}); 
        async with D.session() as s: last = (await s.execute(select(D.Payment).order_by(D.Payment.id.desc()))).scalars().first()
        ok(r.status_code == 200 and last.provider == "yookassa", "без выбора — первый способ (ЮKassa)")
        async with D.session() as s: await save_settings(s, {"pay_yookassa_on": False})
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 300, "provider": "yookassa"}); ok(r.status_code == 400, "выключенный способ не принимается")
        j = (await c.get("/api/me", headers=H)).json(); ok([p["code"] for p in j["cfg"]["providers"]] == ["platega"], "можно оставить только Platega")
        from app import health
        ok((await health._check_platega(ctx))["ok"] is True, "Platega в мониторинге")
        await c.delete("/api/admin/integrations/platega", headers=HA)
        j = (await c.get("/api/me", headers=H)).json(); ok(j["cfg"]["providers"] == [], "отключили всё — пополнение недоступно")
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 300}); ok(r.status_code == 400 and "недоступна" in r.json()["detail"], "понятная ошибка клиенту")
    print("FAILS:", ok.fails)

asyncio.run(main())
