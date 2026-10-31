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
from sqlalchemy import select

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
    def __init__(s): s.pays = {}; s.n = 0; s.charge_status = "succeeded"; s.no_bank = False
    async def create_payment(s, **kw):
        s.n += 1; pid = f"yk{s.n}"; s.pays[pid] = {"id": pid, "status": "succeeded", "paid": True, "amount": {"value": f"{kw['amount_kop']/100:.2f}", "currency": "RUB"},
            "payment_method": {"id": "pm_sbp", "saved": bool(kw.get("save_payment_method")), "type": "sbp", "title": "",
                               **({} if s.no_bank else {"payer_bank_details": {"bank_id": "100000000111", "bic": "044525225"}})}}
        s.last = kw; return {"id": pid, "confirmation": {"confirmation_url": "https://pay/" + pid}}
    async def charge_saved(s, **kw):
        s.n += 1; pid = f"yk{s.n}"; st = s.charge_status
        s.pays[pid] = {"id": pid, "status": st, "paid": st == "succeeded", "amount": {"value": f"{kw['amount_kop']/100:.2f}", "currency": "RUB"},
                       "payment_method": {"id": kw["method_id"], "saved": True},
                       **({"cancellation_details": {"reason": "insufficient_funds"}} if st == "canceled" else {})}
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
    yk = FakeYK()
    ctx = svc.Ctx(astra=FakeAstra(), yk=yk, nalog=FakeNalog(), notify=notify, alert_admin=alert)
    app = build_app(ctx)
    from app import banks
    from app.settings import save_settings
    banks._dir = {"100000000111": {"name": "Сбербанк", "logo_src": "https://qr.nspk.ru/proxyapp/logo/bank100000000111.png"}}
    banks._loaded_at = time.time()
    async with D.session() as s:
        await save_settings(s, {"autopay_available": True})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        H = {"X-Init-Data": init(41)}
        await c.get("/api/me", headers=H); await c.post("/api/me/consent", headers=H, json={"docs": ["terms", "aup", "privacy", "pd"]})
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 500, "save_method": True})
        ok(r.status_code == 200 and r.json()["url"].startswith("https://pay/"), "первое пополнение — через страницу банка, с привязкой")
        await svc.credit_payment(ctx, "yk1")
        ms = (await c.get("/api/me/methods", headers=H)).json()
        ok(len(ms) == 1 and ms[0]["type"] == "sbp" and ms[0]["bank"] == {"id": "100000000111", "name": "Сбербанк", "logo": "/bank-logo/100000000111.png"},
           f"привязанный счёт СБП — с банком и эмблемой: {ms}")
        mid = ms[0]["id"]
        bal0 = (await c.get("/api/me", headers=H)).json()["balance_rub"]
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 700, "method_id": mid})
        bal1 = (await c.get("/api/me", headers=H)).json()["balance_rub"]
        ok(r.status_code == 200 and r.json() == {"status": "succeeded"} and yk.charged["method_id"] == "pm_sbp" and bal1 == bal0 + 700,
           f"оплата привязанным счётом — без страницы банка, баланс пополнен: {r.json()} {bal0}→{bal1}")
        H2 = {"X-Init-Data": init(42)}
        await c.get("/api/me", headers=H2); await c.post("/api/me/consent", headers=H2, json={"docs": ["terms", "aup", "privacy", "pd"]})
        r = await c.post("/api/me/topup", headers=H2, json={"amount_rub": 700, "method_id": mid})
        ok(r.status_code == 400 and "больше не привязан" in r.json()["detail"], "чужим привязанным способом не заплатить")
        yk.charge_status = "pending"
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 300, "method_id": mid})
        ok(r.json() == {"status": "pending"}, "банк обрабатывает — «в обработке», итог придёт вебхуком")
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 300, "method_id": mid})
        ok(r.status_code == 400 and "ещё обрабатывается" in r.json()["detail"], "второй платёж, пока первый в обработке, — не создаём")
        async with D.session() as s:
            for p in (await s.execute(select(D.Payment).where(D.Payment.status == "pending"))).scalars().all():
                p.status = "canceled"
            await s.commit()
        yk.charge_status = "canceled"
        r = await c.post("/api/me/topup", headers=H, json={"amount_rub": 300, "method_id": mid})
        ok(r.status_code == 400 and "отклонён банком" in r.json()["detail"], f"отказ банка — понятная ошибка: {r.json()}")
        # счёт, привязанный до того, как bcloud начал запоминать банк: банк находим в платеже привязки
        async with D.session() as s:
            m = await s.get(D.PaymentMethod, mid)
            m.bank_id, m.bank_bic, m.bank_checked = None, None, False
            await s.commit()
        ms = (await c.get("/api/me/methods", headers=H)).json()
        ok(ms[0]["bank"] and ms[0]["bank"]["name"] == "Сбербанк", "банк у старой привязки дозаполнен из платежа")
        ok((await c.get("/bank-logo/../x.png")).status_code == 404 and (await c.get("/bank-logo/abc.png")).status_code == 404,
           "эмблема — только по номеру банка")
    ok(svc._method_fields({"id": "x", "type": "sbp", "payer_bank_details": {"bank_id": "100000000004", "bic": "044525974"}})["bank_id"] == "100000000004",
       "банк из payer_bank_details")
    ok(banks._parse({"dictionary": [{"bankName": "Т-Банк", "logoURL": "https://qr.nspk.ru/proxyapp/logo/bank100000000004.png",
                                     "schema": "bank100000000004"}]}) == {"100000000004": {"name": "Т-Банк", "logo_src": "https://qr.nspk.ru/proxyapp/logo/bank100000000004.png"}},
       "справочник НСПК разбирается")
    print(f"FAILS: {ok.fails}")


asyncio.run(main())
