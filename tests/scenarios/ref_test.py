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
    app = build_app(ctx)
    from app import referral as R
    from app.settings import save_settings
    ok(R.parse("ref_" + R.code_for(304403447)) == 304403447 and R.parse("ref_!!") is None and R.parse("") is None, "код ссылки кодируется и разбирается")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        DOCS4 = {"docs": ["terms", "aup", "privacy", "pd"]}
        HI = {"X-Init-Data": init(500, "Пригласивший")}; await c.get("/api/me", headers=HI); await c.post("/api/me/consent", headers=HI, json=DOCS4)
        async with D.session() as s:
            await D.get_or_create_user(s, 600, "friend"); await D.get_or_create_user(s, 700, "self")
        ok(await R.attach(600, "ref_" + R.code_for(500)) == 500, "новый пользователь привязан к пригласившему")
        ok(await R.attach(600, "ref_" + R.code_for(700)) is None, "повторная привязка не меняет пригласившего")
        ok(await R.attach(700, "ref_" + R.code_for(700)) is None, "себя пригласить нельзя")
        ok(await R.attach(700, "ref_" + R.code_for(999999)) is None, "несуществующий пригласивший игнорируется")
        HF = {"X-Init-Data": init(600, "Друг")}; await c.get("/api/me", headers=HF); await c.post("/api/me/consent", headers=HF, json=DOCS4)
        for amount in (1000, 500):
            r = await c.post("/api/me/topup", headers=HF, json={"amount_rub": amount, "save_method": False})
            pid = ctx.yk.last and list(ctx.yk.pays)[-1]
            ok(await svc.credit_payment(ctx, pid), f"оплата {amount} ₽ зачислена")
            ok(not await svc.credit_payment(ctx, pid), "повторный вебхук не начисляет второй раз")
        async with D.session() as s:
            inv = await s.get(D.User, 500); fr = await s.get(D.User, 600)
        ok(inv.balance_kop == 15000, f"пригласившему 10% с двух оплат = 150 ₽ (есть {inv.balance_kop/100})")
        ok(fr.balance_kop == 160000, f"другу 1500 ₽ + 100 ₽ приветственного бонуса один раз (есть {fr.balance_kop/100})")
        st = (await c.get("/api/me/referral", headers=HI)).json()
        ok(st["invited"] == 1 and st["active"] == 1 and st["earned_rub"] == 150 and st["bot"].endswith("?start=ref_" + R.code_for(500)), "статистика в кабинете")
        h = (await c.get("/api/me/history", headers=HI)).json(); ok(h[0]["title"] == "Бонус за друга", "бонус виден в истории")
        r = await c.get("/r/" + R.code_for(500), follow_redirects=False); ok(r.status_code == 302 and "start=ref_" in r.headers["location"], "веб-ссылка ведёт в бота")
        r = await c.get("/r/%21%21"); ok(r.status_code == 404, "битая ссылка — 404")
        # срок программы истёк — бонус не начисляется
        async with D.session() as s:
            fr = await s.get(D.User, 600); fr.referred_at = D.utcnow() - timedelta(days=400); await s.commit()
        await c.post("/api/me/topup", headers=HF, json={"amount_rub": 1000, "save_method": False})
        await svc.credit_payment(ctx, list(ctx.yk.pays)[-1])
        async with D.session() as s:
            inv = await s.get(D.User, 500)
        ok(inv.balance_kop == 15000, "после окончания срока бонусы не начисляются")
        async with D.session() as s:
            await save_settings(s, {"ref_enabled": False})
        st = (await c.get("/api/me/referral", headers=HI)).json(); ok(st["enabled"] is False, "программу можно выключить")
        HA = {"X-Init-Data": init(1, "Админ")}
        card = (await c.get("/api/admin/users/600", headers=HA)).json()
        ok(card["referral"]["referrer"]["tg_id"] == 500, "в карточке клиента виден пригласивший")
        t = (await c.get("/legal/terms")).text; ok("реферальную программу" in t, "условия программы в оферте")
    print("FAILS:", ok.fails)

asyncio.run(main())
