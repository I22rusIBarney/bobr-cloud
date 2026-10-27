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
    POSTS = []
    CHATS = []
    async def post(chat, text): POSTS.append(text); CHATS.append(str(chat)); return None
    ctx.post = post
    from app.settings import save_settings
    async with D.session() as s: await save_settings(s, {"ev_sales_chat": "@sales", "ev_locations_chat": "@locs"})
    CAT = {"locations": [{"code": "nl", "name": "🇳🇱 Нидерланды", "available": True}], "presets": [dict(p, price_month_rub=dict(p["price_month_rub"])) for p in PRESETS], "os": ["ubuntu2404"]}
    async def cat(): return json.loads(json.dumps(CAT))
    ctx.astra.catalog = cat
    await svc.watch_locations(ctx); ok(not POSTS, "первый запуск — только запоминаем")
    CAT["locations"].append({"code": "cz", "name": "🇨🇿 Чехия", "available": True})
    await svc.watch_locations(ctx); ok(len(POSTS) == 1 and "появилась локация 🇨🇿 Чехия" in POSTS[0] and "без доступных тарифов" in POSTS[0], "новая локация без цен — предупреждение")
    ok(CHATS and all("locs" in c for c in CHATS), "события о локациях — в свой канал «Локации», не в «Покупки»")
    for p in CAT["presets"]: p["price_month_rub"]["cz"] = 400
    POSTS.clear(); await svc.watch_locations(ctx); ok(len(POSTS) == 1 and "снова в продаже" in POSTS[0], "появились цены — в продаже")
    offs = await svc.offers(ctx, "cz"); ok(len(offs) == 2 and offs[0]["price_kop"] > 0, "тарифы Чехии сразу в боте и кабинете")
    CAT["locations"].append({"code": "ch", "name": "🇨🇭 Швейцария", "available": True})
    for p in CAT["presets"]: p["price_month_rub"]["ch"] = 600
    POSTS.clear(); await svc.watch_locations(ctx); ok(len(POSTS) == 1 and "Новая локация в продаже: 🇨🇭 Швейцария" in POSTS[0] and "Тарифов: 2" in POSTS[0], "новая локация с ценами — сразу в продаже")
    CAT["locations"][1]["available"] = False
    POSTS.clear(); await svc.watch_locations(ctx); ok(len(POSTS) == 1 and "недоступна" in POSTS[0] and "Чехия" in POSTS[0], "локация закрыта — предупреждение")
    POSTS.clear(); await svc.watch_locations(ctx); ok(not POSTS, "без изменений — тишина")
    # ── рассылка клиентам о новых локациях
    async with D.session() as s:
        for uid, ads in ((100, True), (101, False)):
            u = await D.get_or_create_user(s, uid, f"u{uid}"); u.consent_version = 1; u.ads_consent = ads
        await s.commit()
    SENT = []
    async def sendb(uid, text, kb): SENT.append((uid, text, kb)); return "ok"
    ctx.send_broadcast = sendb; ctx.bot_username = "bobr_bot"
    pend = await svc._setting_json("loc_announce_pending", [])
    ok(sorted(pend) == ["ch", "cz"], "в очереди объявлений — Чехия (открылась впервые) и Швейцария")
    POSTS.clear(); await svc.announce_locations(ctx); await asyncio.sleep(0.4)
    ok([u for u, _, _ in SENT] == [100], "рассылка ушла только клиенту с согласием на рекламу")
    txt, kb = SENT[0][1], SENT[0][2]
    ok("Новая локация — 🇨🇭 Швейцария" in txt and "от <b>" in txt, "текст: страна и цена от")
    ok(any(u == "https://t.me/bobr_bot?start=loc_ch" for _, u, _ in kb) and any(cb == "nw:off" for _, _, cb in kb), "кнопки: «Собрать сервер» по ссылке и «Отписаться»")
    ok(any("Клиентам ушла рассылка" in p and "Получателей: 1" in p for p in POSTS), "админу — событие о рассылке")
    ok("cz" not in await svc._setting_json("loc_announce_pending", []) and "cz" not in await svc._setting_json("announced_locations", []), "закрытую до рассылки Чехию не объявили — объявим при открытии")
    CAT["locations"][1]["available"] = True
    SENT.clear(); await svc.watch_locations(ctx); await svc.announce_locations(ctx); await asyncio.sleep(0.4)
    ok(len(SENT) == 1 and "Чехия" in SENT[0][1], "Чехия снова в продаже — объявили один раз")
    CAT["locations"][2]["available"] = False; await svc.watch_locations(ctx)
    CAT["locations"][2]["available"] = True
    SENT.clear(); await svc.watch_locations(ctx); await svc.announce_locations(ctx); await asyncio.sleep(0.3)
    ok(not SENT, "повторное открытие уже объявленной страны — без рассылки")
    async with D.session() as s:
        s.add(D.Broadcast(text="x", audience="all", kind="news", status="sending", total=1)); await s.commit()
    CAT["locations"].append({"code": "se", "name": "🇸🇪 Швеция", "available": True})
    for p in CAT["presets"]: p["price_month_rub"]["se"] = 500
    SENT.clear(); await svc.watch_locations(ctx); await svc.announce_locations(ctx)
    ok(not SENT and await svc._setting_json("loc_announce_pending", []) == ["se"], "идёт другая рассылка — ждём в очереди")
    from app.settings import save_settings as _ss
    async with D.session() as s: await _ss(s, {"loc_announce": False}); await s.commit()
    await svc.announce_locations(ctx)
    ok(await svc._setting_json("loc_announce_pending", []) == [], "выключено в админке — очередь очищается, рассылки нет")
    # ── «Сообщить, когда появится»
    from app import locwatch
    async with D.session() as s: await _ss(s, {"loc_announce": True}); await s.commit()
    CAT["locations"][1]["available"] = False  # Чехия закрыта (была в продаже)
    CAT["locations"].append({"code": "pt", "name": "🇵🇹 Португалия", "available": False, "description": "IP Португалии · сервер во Франкфурте (DE)"})
    await svc.watch_locations(ctx)
    cl = {x["code"]: x for x in await locwatch.closed_locations(ctx, 101)}
    ok(cl.get("cz", {}).get("status") == "soldout" and cl.get("pt", {}).get("status") == "soon" and "nl" not in cl, "закрытые: Чехия — «нет мест», Португалия — «скоро», открытые не в списке")
    await locwatch.set_watch(101, "cz", True); await locwatch.set_watch(100, "cz", True); await locwatch.set_watch(101, "pt", True)
    ok((await locwatch.closed_locations(ctx, 101))[0]["watching"] in (True, False) and {"cz", "pt"} <= await locwatch.watching(101), "подписка «сообщить» сохраняется")
    CAT["locations"][1]["available"] = True
    SENT.clear(); KB = []
    async def nkb(uid, text, rows): KB.append((uid, text, rows))
    ctx.notify_kb = nkb
    await svc.watch_locations(ctx); n = await locwatch.notify(ctx)
    ok(n == 0 and not KB, "открылась только что — ждём 10 минут (защита от мигания)")
    locwatch.STABLE_S = 0
    n = await locwatch.notify(ctx)
    ok(n == 2 and {u for u, _, _ in KB} == {100, 101} and "можно заказывать" in KB[0][1] and KB[0][2][0][0][1] == "b:l:cz", "через 10 минут — обоим подписавшимся с кнопкой «Собрать сервер»")
    ok("cz" not in await locwatch.watching(101), "после уведомления подписка снимается")
    KB.clear(); n = await locwatch.notify(ctx); ok(n == 0, "повторно не пишем")
    # новая страна: подписчикам с согласием на рекламу — только общая рассылка, без дубля
    await locwatch.set_watch(100, "pt", True)
    CAT["locations"][-1]["available"] = True
    for p in CAT["presets"]: p["price_month_rub"]["pt"] = 450
    async with D.session() as s:
        await s.execute(D.update(D.Broadcast).values(status="done")); await s.commit()
    SENT.clear(); KB.clear()
    await svc.watch_locations(ctx); await locwatch.notify(ctx); await svc.announce_locations(ctx); await asyncio.sleep(0.4); await locwatch.notify(ctx)
    ok([u for u, _, _ in SENT] == [100] and [u for u, _, _ in KB] == [101], "новая страна: с согласием — рассылка, без согласия — личное уведомление, без дублей")
    ok(not await locwatch.watching(100) and not await locwatch.watching(101), "все подписки на Португалию сняты")
    t = __import__("app.legal", fromlist=["x"]).render_fragment("terms", {**__import__("app.settings", fromlist=["x"]).DEFAULTS}, "", "")
    ok("Эстония" not in t and "виртуальная геолокация" in t, "оферта без жёсткого списка стран")
    print("FAILS:", ok.fails)

asyncio.run(main())
