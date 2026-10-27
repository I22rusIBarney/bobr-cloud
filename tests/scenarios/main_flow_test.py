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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        H2 = {"X-Init-Data": init(2)}; HA = {"X-Init-Data": init(1, "Админ")}
        r = await c.get("/api/me", headers=H2); j = r.json()
        ok(r.status_code == 200 and j["consent"]["needed"] and j["user"]["first_name"] == "Иван", "обзор доступен до согласия и требует согласия")
        r = await c.get("/api/me/history", headers=H2); ok(r.status_code == 403 and "примите" in r.json()["detail"].lower(), "закрытые разделы недоступны без согласия")
        r = await c.post("/api/me/consent", headers=H2, json={"docs": ["terms", "privacy"]}); ok(r.status_code == 400, "частичное согласие отклонено")
        r = await c.post("/api/me/consent", headers=H2, json={"docs": ["terms", "aup", "privacy", "pd"]}); ok(r.status_code == 200, "согласие принято")
        r = await c.get("/api/me", headers=H2); ok(not r.json()["consent"]["needed"], "после согласия доступ открыт")
        r = await c.get("/legal/privacy"); ok(r.status_code == 200 and "не указано" in r.text and "152-ФЗ" in r.text, "страница политики открывается, реквизиты помечены как незаполненные")
        r = await c.get("/api/me/legal/terms", headers=H2); ok(r.status_code == 200 and "оферт" in r.json()["html"], "документ отдаётся в кабинет")

        # админ: промокоды
        await c.post("/api/me/consent", headers=HA, json={"docs": ["terms", "aup", "privacy", "pd"]})
        r = await c.post("/api/admin/promos", headers=HA, json={"code": "bonus100", "kind": "bonus", "value": 100, "max_uses": 1}); ok(r.status_code == 200 and r.json()["code"] == "BONUS100", "создан бонус-промокод")
        r = await c.post("/api/admin/promos", headers=HA, json={"code": "SALE20", "kind": "discount", "value": 20}); ok(r.status_code == 200, "создан промокод на скидку")
        r = await c.post("/api/admin/promos", headers=HA, json={"code": "BAD", "kind": "discount", "value": 95}); ok(r.status_code == 400, "скидка >90% отклонена")
        r = await c.post("/api/me/promo", headers=H2, json={"code": " bonus100 "}); ok(r.status_code == 200 and r.json()["kind"] == "bonus", "бонус активирован")
        r = await c.post("/api/me/promo", headers=H2, json={"code": "BONUS100"}); ok(r.status_code == 400, "повторная активация запрещена")
        H3 = {"X-Init-Data": init(3)}; await c.get("/api/me", headers=H3); await c.post("/api/me/consent", headers=H3, json={"docs": ["terms", "aup", "privacy", "pd"]})
        r = await c.post("/api/me/promo", headers=H3, json={"code": "BONUS100"}); ok(r.status_code == 400 and "закончил" in r.json()["detail"], "лимит активаций соблюдается")
        r = await c.post("/api/me/promo", headers=H2, json={"code": "sale20"}); ok(r.status_code == 200 and r.json()["kind"] == "discount", "скидка активирована")
        j = (await c.get("/api/me", headers=H2)).json(); ok(j["balance_rub"] == 100 and j["discount"]["pct"] == 20, "баланс 100 ₽ и скидка 20% видны в кабинете")

        # покупка со скидкой: 188*1.4=263.2 → 270, со скидкой 20% → 216
        async with D.session() as s:
            await D.change_balance(s, 2, 50000, "admin_adjust")
        r = await c.post("/api/me/servers", headers=H2, json={"idx": 0, "location": "nl", "os": "ubuntu2404"}); ok(r.status_code == 200, "сервер куплен")
        j = (await c.get("/api/me", headers=H2)).json()
        ok(abs(j["balance_rub"] - (600 - 216)) < 0.01, f"списано со скидкой 216 ₽ (баланс {j['balance_rub']})")
        ok(j["discount"] is None, "скидка израсходована")
        ok(j["servers"][0]["price_rub"] == 270 and j["servers"][0]["refund_rub"] <= 216, "продление по полной цене, возврат не больше оплаченного")
        ok(ctx.astra.last["name"] != "2" and not ctx.astra.last["name"].endswith("-2"), f"имя для Astra без Telegram ID ({ctx.astra.last['name']})")

        # пополнение с сохранением карты: пока автоплатежи выключены — флаг игнорируется
        r = await c.post("/api/me/topup", headers=H2, json={"amount_rub": 500, "save_method": True}); ok(r.status_code == 200 and not ctx.yk.last.get("save_payment_method"), "без включённых автоплатежей карта не сохраняется")
        await c.post("/api/admin/settings", headers=HA, json={"values": {"autopay_available": True, "yk_method": "all"}})
        r = await c.post("/api/me/topup", headers=H2, json={"amount_rub": 500, "save_method": True}); ok(ctx.yk.last.get("save_payment_method") is True, "запрос в ЮKassa с save_payment_method")
        pid = r.json()["url"].split("/")[-1]
        r = await c.post("/yookassa/webhook/sec", json={"event": "payment.succeeded", "object": {"id": pid}}); ok(r.status_code == 200, "вебхук принят")
        ms = (await c.get("/api/me/methods", headers=H2)).json(); ok(len(ms) == 1 and ms[0]["last4"] == "4444", "карта сохранена маской *4444")
        j = (await c.get("/api/me", headers=H2)).json(); ok(j["autopay"]["enabled"], "автоплатёж включился после сохранения карты")
        r = await c.post("/api/me/autopay", headers=H2, json={"value": False}); ok(r.json()["enabled"] is False, "автоплатёж выключается")
        r = await c.post("/api/me/autopay", headers=H2, json={"value": True}); ok(r.json()["enabled"] is True, "автоплатёж включается снова")

        # автоплатёж при продлении: баланс обнуляем, срок — через 10 часов
        async with D.session() as s:
            u = await s.get(D.User, 2); u.balance_kop = 1000
            srv = (await s.execute(D.select(D.Server) if hasattr(D, "select") else None)) if False else None
            from sqlalchemy import select
            srv = (await s.execute(select(D.Server).where(D.Server.tg_id == 2))).scalar_one()
            srv.expires_at = D.utcnow() + timedelta(hours=10); srv.status = "active"
            await s.commit()
        await svc.autorenew_and_remind(ctx)
        ok(getattr(ctx.yk, "charged", {}).get("amount_kop") == 26000, f"автоплатёж на недостающие 260 ₽ ({getattr(ctx.yk, 'charged', {}).get('amount_kop')})")
        async with D.session() as s:
            srv = (await s.execute(select(D.Server).where(D.Server.tg_id == 2))).scalar_one()
            u = await s.get(D.User, 2)
        ok(srv.expires_at > D.utcnow() + timedelta(days=5) and u.balance_kop == 0, f"сервер продлён после автоплатежа, баланс {u.balance_kop}")
        ok(any("Автоплатёж" in t for _, t in msgs), "клиент уведомлён об автоплатеже")
        async def broken(sid): raise RuntimeError("astra down")
        ctx.astra.server = broken
        async with D.session() as s:
            u = await s.get(D.User, 2); u.balance_kop = 100000; await s.commit()
        n_renew = sum(1 for x in (await c.get("/api/me/history", headers=H2)).json() if x["kind"] == "renew")
        for _ in range(3):
            await svc.autorenew_and_remind(ctx)
        n2 = sum(1 for x in (await c.get("/api/me/history", headers=H2)).json() if x["kind"] == "renew")
        ok(n2 == n_renew, f"повторного продления нет даже при сбое Astra ({n_renew} → {n2})")

        # удаление способов оплаты: кнопка работает и при пустом списке
        r = await c.delete("/api/me/methods", headers=H2); ok(r.json()["deleted"] == 1, "все карты удалены")
        r = await c.delete("/api/me/methods", headers=H2); ok(r.status_code == 200 and r.json()["deleted"] == 0, "удаление при пустом списке не падает")
        j = (await c.get("/api/me", headers=H2)).json(); ok(not j["autopay"]["enabled"], "без карт автоплатёж выключен")
        r = await c.post("/api/me/autopay", headers=H2, json={"value": True}); ok(r.status_code == 400, "без карты включить автоплатёж нельзя")

        h = (await c.get("/api/me/history", headers=H2)).json()
        kinds = [x["kind"] for x in h]; ok({"promo", "purchase", "topup", "autopay", "renew"} <= set(kinds), f"история операций: {sorted(set(kinds))}")

        # админ: клиенты
        r = await c.get("/api/admin/users?filter=active", headers=HA); ok(r.status_code == 200 and any(u["tg_id"] == 2 for u in r.json()), "фильтр клиентов с услугами")
        card = (await c.get("/api/admin/users/2", headers=HA)).json()
        ok(card["methods"][0]["deleted_by"] == "user" and card["servers"] and card["promos"] and card["consents"], "карточка клиента: услуги, карты, промокоды, согласия")
        r = await c.post("/api/admin/docs/publish", headers=HA); ok(r.status_code == 400, "публикация без реквизитов запрещена")
        await c.post("/api/admin/settings", headers=HA, json={"values": {"seller_name": "Иванов Иван Иванович", "seller_inn": "540123456789", "seller_email": "a@b.ru"}})
        r = await c.post("/api/admin/docs/publish", headers=HA); ok(r.status_code == 200 and r.json()["docs_version"] == 2, "новая редакция опубликована")
        j = (await c.get("/api/me", headers=H2)).json(); ok(j["consent"]["needed"], "после новой редакции клиент снова принимает документы")
        r = await c.get("/legal/terms"); ok("Иванов Иван Иванович" in r.text and "не указано" not in r.text, "реквизиты подставлены в документ")
        r = await c.post("/api/me/consent/revoke", headers=H3); ok(r.status_code == 200 and any("отозвал" in str(t) for _, t in msgs), "отзыв согласия уведомляет админа")
    print("FAILS:", ok.fails)
asyncio.run(main())
