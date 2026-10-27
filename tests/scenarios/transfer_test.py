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
    OFFERS = []
    async def offer(uid, text, tid): OFFERS.append((uid, text, tid))
    ctx.send_transfer_offer = offer
    app = build_app(ctx)
    from sqlalchemy import select
    DOCS4 = {"docs": ["terms", "aup", "privacy", "pd"]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        A, B, C = {"X-Init-Data": init(51, "Аня")}, {"X-Init-Data": init(52, "Боря")}, {"X-Init-Data": init(53, "Вова")}
        for h in (A, B): await c.get("/api/me", headers=h); await c.post("/api/me/consent", headers=h, json=DOCS4)
        await c.get("/api/me", headers=C)  # Вова открыл бота, но документы не принял
        async with D.session() as s: await D.change_balance(s, 51, 200000, "admin_adjust")
        r = await c.post("/api/me/servers", headers=A, json={"idx": 1, "location": "nl", "os": "ubuntu2404"}); sid = r.json()["id"]
        async with D.session() as s:
            srv = await s.get(D.Server, sid); srv.status = "active"; srv.ip = "1.1.1.1"; await s.commit()
        r = await c.post(f"/api/me/servers/{sid}/transfer", headers=A, json={"to": "@nobody_here"}); ok(r.status_code == 400 and "нет в боте" in r.json()["detail"], "получатель не найден — понятная ошибка")
        r = await c.post(f"/api/me/servers/{sid}/transfer", headers=A, json={"to": "user53"}); ok(r.status_code == 400, "не принявшему документы передать нельзя")
        r = await c.post(f"/api/me/servers/{sid}/transfer", headers=A, json={"to": "51"}); ok(r.status_code == 400 and "самому себе" in r.json()["detail"], "себе передать нельзя")
        r = await c.post(f"/api/me/servers/{sid}/transfer", headers=B, json={"to": "51"}); ok(r.status_code == 400, "чужой сервер передать нельзя")
        r = await c.post(f"/api/me/servers/{sid}/transfer", headers=A, json={"to": "@User52"}); ok(r.status_code == 200 and OFFERS and OFFERS[-1][0] == 52, "по @username (без учёта регистра) — запрос получателю")
        ok("Вам передают сервер" in OFFERS[-1][1] and "Аня" in OFFERS[-1][1], "в запросе — от кого и что за сервер")
        tid = r.json()["id"]
        r = await c.post(f"/api/me/servers/{sid}/transfer", headers=A, json={"to": "52"}); ok(r.status_code == 400 and "ждёт подтверждения" in r.json()["detail"], "второй запрос на тот же сервер нельзя")
        jb = (await c.get("/api/me", headers=B)).json(); ja = (await c.get("/api/me", headers=A)).json()
        ok(len(jb["transfers"]["incoming"]) == 1 and len(ja["transfers"]["outgoing"]) == 1, "кабинет: у получателя входящий, у владельца исходящий")
        r = await c.post(f"/api/me/transfers/{tid}/accept", headers=A); ok(r.status_code == 400, "владелец не может принять за получателя")
        r = await c.post(f"/api/me/transfers/{tid}/cancel", headers=A); ok(r.status_code == 200 and any("отменил" in str(m) for m in msgs), "владелец отменил — получатель уведомлён")
        r = await c.post(f"/api/me/transfers/{tid}/accept", headers=B); ok(r.status_code == 400 and "отменил" in r.json()["detail"], "отменённый запрос не принять")
        r = await c.post(f"/api/me/servers/{sid}/transfer", headers=A, json={"to": "52"}); tid2 = r.json()["id"]
        r = await c.post(f"/api/me/transfers/{tid2}/decline", headers=B); ok(r.status_code == 200 and any("отклонил" in str(m) for m in msgs), "отклонение — владелец уведомлён")
        r = await c.post(f"/api/me/servers/{sid}/transfer", headers=A, json={"to": "52"}); tid3 = r.json()["id"]
        r = await c.post(f"/api/me/transfers/{tid3}/accept", headers=B); ok(r.status_code == 200, "получатель принял")
        async with D.session() as s: srv = await s.get(D.Server, sid)
        ok(srv.tg_id == 52, "сервер теперь у получателя")
        ja = (await c.get("/api/me", headers=A)).json(); jb = (await c.get("/api/me", headers=B)).json()
        ok(not ja["servers"] and len(jb["servers"]) == 1 and jb["servers"][0]["refund_rub"] > 0, "у владельца исчез, у получателя есть, возврат — получателю")
        ok(any("передан" in str(m) for m in msgs), "владелец получил уведомление о передаче")
        r = await c.post(f"/api/me/servers/{sid}/action", headers=A, json={"action": "reboot"}); ok(r.status_code in (400, 404), "старый владелец больше не управляет сервером")
        r = await c.post(f"/api/me/transfers/{tid3}/accept", headers=B); ok(r.status_code == 400, "повторное принятие невозможно")
        r = await c.post(f"/api/me/servers/{sid}/transfer", headers=B, json={"to": "51"}); tid4 = r.json()["id"]
        async with D.session() as s:
            t = await s.get(D.ServerTransfer, tid4); t.created_at = D.utcnow() - timedelta(hours=25); await s.commit()
        r = await c.post(f"/api/me/transfers/{tid4}/accept", headers=A); ok(r.status_code == 400 and "истёк" in r.json()["detail"], "запрос старше 24 часов истекает")
        t = (await c.get("/legal/terms")).text; ok("4.7" in t and "передать сервер" in t, "пункт о передаче в оферте")
    print("FAILS:", ok.fails)

asyncio.run(main())
