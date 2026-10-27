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
    SUBS = {1}
    async def check_member(chat, uid): return uid in SUBS
    async def probe(chat): return {"ok": True, "title": "Bobr News", "link": "https://t.me/bobrnews"}
    SENT = []
    async def send(uid, text, kb):
        SENT.append((uid, text, kb))
        return "blocked" if uid == 13 else "ok"
    ctx.check_member, ctx.probe_chat, ctx.send_broadcast = check_member, probe, send
    app = build_app(ctx)
    from app.settings import save_settings
    from app import broadcasts as bc, subscription as sub
    DOCS4 = {"docs": ["terms", "aup", "privacy", "pd"]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        HA = {"X-Init-Data": init(1, "Админ")}
        H = {u: {"X-Init-Data": init(u)} for u in (11, 12, 13)}
        for u in H:
            await c.get("/api/me", headers=H[u])
            await c.post("/api/me/consent", headers=H[u], json={"docs": DOCS4["docs"] + (["ads"] if u == 11 else [])})
        j = (await c.get("/api/me", headers=H[11])).json()
        ok(j["ads_consent"] and j["consent"]["optional"][0]["key"] == "ads", "согласие на рекламу дано при первом запуске")
        ok(not (await c.get("/api/me", headers=H[12])).json()["ads_consent"], "без галочки согласия на рекламу нет")
        r = await c.post("/api/me/consent", headers={"X-Init-Data": init(14)}, json={"docs": ["ads"]}); ok(r.status_code in (400, 403), "одной галочки «реклама» мало для входа")
        # ── индивидуальная наценка
        base = (await c.get("/api/me/catalog", headers=H[11])).json()["locations"][0]["offers"][1]["price_kop"]
        r = await c.post("/api/admin/users/11/markup", headers=HA, json={"mode": "custom", "pct": 10})
        ok(r.status_code == 200 and r.json()["mode"] == "custom", "своя наценка 10% сохранена")
        own = (await c.get("/api/me/catalog", headers=H[11])).json()["locations"][0]["offers"][1]["price_kop"]
        ok(own == svc.sell_price_kop(675, {"markup_pct": 10, "markup_fixed_rub": 0, "round_to_rub": 10}) and own < base, f"цена клиента со своей наценкой ({base/100} → {own/100})")
        other = (await c.get("/api/me/catalog", headers=H[12])).json()["locations"][0]["offers"][1]["price_kop"]
        ok(other == base, "у других клиентов цена прежняя")
        card = (await c.get("/api/admin/users/11", headers=HA)).json(); ok(card["own_markup_pct"] == 10, "карточка показывает свою наценку")
        r = await c.post("/api/admin/users/11/markup", headers=HA, json={"mode": "custom", "pct": 5000}); ok(r.status_code == 400, "нереальная наценка отклонена")
        await c.post("/api/admin/users/11/markup", headers=HA, json={"mode": "none"})
        nm = (await c.get("/api/me/catalog", headers=H[11])).json()["locations"][0]["offers"][1]["price_kop"]; ok(nm == 67500, "без наценки — себестоимость")
        await c.post("/api/admin/users/11/markup", headers=HA, json={"mode": "default"})
        ok((await c.get("/api/me/catalog", headers=H[11])).json()["locations"][0]["offers"][1]["price_kop"] == base, "возврат к общей наценке")
        r = await c.post("/api/admin/users/11/markup", headers=HA, json={"no_markup": True}); ok(r.json()["no_markup"] is True, "старый переключатель работает")
        await c.post("/api/admin/users/11/markup", headers=HA, json={"mode": "default"})
        # ── подписка
        r = await c.post("/api/admin/subscription/check", headers=HA, json={"chat": "https://t.me/bobrnews"}); ok(r.json()["ok"] and r.json()["title"] == "Bobr News", "проверка канала в админке")
        async with D.session() as s: await save_settings(s, {"sub_required": True, "sub_chat": "@bobrnews"})
        j = (await c.get("/api/me", headers=H[11])).json()
        ok(j["subscription"]["needed"] and j["subscription"]["link"] == "https://t.me/bobrnews", "кабинет просит подписаться")
        r = await c.get("/api/me/history", headers=H[11]); ok(r.status_code == 403 and "подпишитесь" in r.json()["detail"].lower(), "без подписки разделы закрыты")
        r = await c.post("/api/me/subscription/check", headers=H[11]); ok(r.json()["needed"], "«Я подписался» без подписки — не пускает")
        SUBS.add(11)
        r = await c.post("/api/me/subscription/check", headers=H[11]); ok(not r.json()["needed"], "после подписки — пускает")
        ok((await c.get("/api/me/history", headers=H[11])).status_code == 200, "разделы открылись")
        ok(await sub.required(ctx, 1) is None, "админа подписка не ограничивает")
        async def broken(chat, uid): return None
        ctx.check_member = broken; sub.forget(12)
        ok(await sub.required(ctx, 12) is None, "если проверка не работает — клиента пропускаем")
        ctx.check_member = check_member
        async with D.session() as s: await save_settings(s, {"sub_required": False})
        # ── рассылки
        r = await c.post("/api/admin/broadcasts", headers=HA, json={"text": "<b>Привет", "audience": "all"}); ok(r.status_code == 400 and "разметк" in r.json()["detail"], "битая разметка отклонена")
        r = await c.post("/api/admin/broadcasts", headers=HA, json={"text": "Цена 5 < 10", "audience": "all"}); ok(r.status_code == 400, "голый < отклонён")
        r = await c.post("/api/admin/broadcasts", headers=HA, json={"text": "<b>Тест</b>", "btn_text": "Сайт", "btn_url": "http://x"}); ok(r.status_code == 400, "кнопка без https отклонена")
        r = await c.post("/api/admin/broadcasts", headers=HA, json={"text": "<b>Тест</b> себе", "test": True, "btn_text": "Открыть", "btn_url": "https://t.me/bobrnews"})
        ok(r.status_code == 200 and SENT[-1][0] == 1 and SENT[-1][2][0][1] == "https://t.me/bobrnews", "тест себе отправлен с кнопкой")
        from app import account as acc
        await acc.set_ads_consent(13, True, "bot")
        lst = (await c.get("/api/admin/broadcasts", headers=HA)).json()
        ok(lst["counts"]["news:all"] == 2 and lst["counts"]["service:all"] == 3, f"аудитория учитывает отписку ({lst['counts']['news:all']}/{lst['counts']['service:all']})")
        SENT.clear()
        r = await c.post("/api/admin/broadcasts", headers=HA, json={"text": "🔥 <b>Акция</b>: −20% на всё", "audience": "all", "kind": "news"})
        ok(r.status_code == 200, "рассылка запущена")
        bid = r.json()["id"]
        for _ in range(50):
            await asyncio.sleep(0.1)
            b = (await c.get(f"/api/admin/broadcasts/{bid}", headers=HA)).json()
            if b["status"] != "sending": break
        ok(b["status"] == "done" and b["sent"] == 1 and b["blocked"] == 1, f"рассылка завершена: {b['sent']} доставлено, {b['blocked']} заблокировали")
        ok(sorted(x[0] for x in SENT) == [11, 13] and any(t[2] == "nw:off" for t in SENT[0][2]), "реклама — только с согласием, в сообщении есть «Отписаться»")
        async with D.session() as s: u13 = await s.get(D.User, 13)
        ok(u13.bot_blocked, "заблокировавший бота помечен")
        ok(any("Рассылка #" in str(m) for m in msgs), "админу пришёл отчёт")
        SENT.clear()
        r = await c.post("/api/admin/broadcasts", headers=HA, json={"text": "Плановые работы", "audience": "all", "kind": "service"})
        for _ in range(50):
            await asyncio.sleep(0.1)
            if (await c.get(f"/api/admin/broadcasts/{r.json()['id']}", headers=HA)).json()["status"] != "sending": break
        ok(sorted(x[0] for x in SENT) == [11, 12] and not SENT[0][2], "важные — и отписавшимся, без кнопки отписки; заблокировавших пропускаем")
        SENT.clear()
        r = await c.post("/api/admin/broadcasts", headers=HA, json={"text": "Обновили условия", "audience": "all", "kind": "service", "ask_ads": True})
        for _ in range(50):
            await asyncio.sleep(0.1)
            if (await c.get(f"/api/admin/broadcasts/{r.json()['id']}", headers=HA)).json()["status"] != "sending": break
        ok(SENT and SENT[0][2] and SENT[0][2][0][2] == "nw:on", "в важной рассылке можно предложить подписку на новости")
        r = await c.post("/api/me/news", headers=H[12], json={"enabled": True}); ok(r.status_code == 200, "клиент дал согласие на рекламу в кабинете")
        await c.post("/api/me/news", headers=H[12], json={"enabled": False})
        card = (await c.get("/api/admin/users/12", headers=HA)).json()
        ads = [x for x in card["consents"] if x["doc"] == "ads"]
        ok(len(ads) == 2 and ads[0]["accepted"] is False and ads[1]["accepted"] is True, "журнал: согласие и отзыв согласия на рекламу")
        await c.post("/api/me/consent/revoke", headers=H[11])
        async with D.session() as s: u11 = await s.get(D.User, 11)
        ok(not u11.ads_consent, "отзыв согласия на ПД отзывает и рекламу")
        t = (await c.get("/legal/ads")).text; ok("38-ФЗ" in t and "отозвать" in t.lower(), "документ «Согласие на рекламу» опубликован")
    print("FAILS:", ok.fails)

asyncio.run(main())
