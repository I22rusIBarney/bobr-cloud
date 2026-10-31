"""Сквозной тест бэкенда с подставными Astra и ЮKassa. Запуск: pytest tests/test_flows.py -s"""
import asyncio, os, sys, json, time, hmac, hashlib
from urllib.parse import urlencode
from datetime import timedelta

TMP = None
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
    global D, svc
    from app import db as D, services as svc
    from app.web.server import build_app
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
        import re
        ok(ctx.astra.last["name"] == "🇳🇱 @user2" and "2" not in ctx.astra.last["name"].replace("user2", ""),
           f"имя для Astra: флаг и ник, без Telegram ID ({ctx.astra.last['name']})")
        ok(re.fullmatch(r"vps-[0-9a-f]{6}", j["servers"][0]["name"]) is not None, f"клиент видит своё название ({j['servers'][0]['name']})")

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
        # ───── обращения в поддержку ─────
        btns = []
        async def notify_btn(uid, text, button, url): btns.append((uid, text, url))
        ctx.notify_btn = notify_btn
        H4 = {"X-Init-Data": init(4, "Пётр")}
        await c.get("/api/me", headers=H4); await c.post("/api/me/consent", headers=H4, json={"docs": ["terms", "aup", "privacy", "pd"]})
        async with D.session() as s:
            s.add(D.Server(astra_id=777, tg_id=4, name="Мой сайт", location="de", cpu=2, ram=4, disk=60, os="debian12", price_kop=95000, status="active", ip="10.0.0.7"))
            await s.commit()
            sid = (await s.execute(select(D.Server).where(D.Server.astra_id == 777))).scalar_one().id
        r = await c.post("/api/me/tickets", headers=H4, json={"subject": "Не пингуется", "text": "Сервер не отвечает по SSH с утра", "server_id": sid})
        ok(r.status_code == 200, "клиент создал обращение")
        tid = r.json()["id"]
        ok(any(u == 1 and f"ticket={tid}" in url for u, t, url in btns), "админу пришло уведомление с кнопкой на обращение")
        r = await c.post("/api/me/tickets", headers=H4, json={"text": "чужой сервер", "server_id": 1}); ok(r.status_code == 400, "нельзя привязать чужой сервер")
        lst = (await c.get("/api/admin/tickets", headers=HA)).json()
        ok(lst["unread"] >= 1 and lst["items"][0]["client"]["first_name"] == "Пётр", "обращение в списке админа с именем клиента")
        d = (await c.get(f"/api/admin/tickets/{tid}", headers=HA)).json()
        ok(d["client"]["tg_id"] == 4 and d["servers"][0]["astra_id"] == 777 and d["provider_support"] == "AstraSupportroBot", "в тикете видны клиент и его серверы")
        r = await c.post(f"/api/admin/tickets/{tid}/messages", headers=HA, json={"text": "проверю у провайдера", "internal": True})
        ok(r.status_code == 200 and not any(u == 4 for u, *_ in btns), "внутренняя заметка клиенту не уходит")
        r = await c.post(f"/api/admin/tickets/{tid}/escalate", headers=HA, json={})
        e = r.json()
        ok(r.status_code == 200 and "ID сервера: 777" in e["text"] and "Пётр" not in e["text"] and "t.me/AstraSupportroBot?text=" in e["url"], "передача провайдеру: тех. данные без данных клиента")
        cl = (await c.get(f"/api/me/tickets/{tid}", headers=H4)).json()
        ok(cl["status"] == "provider" and all(m["author"] != "note" for m in cl["messages"]), "клиент видит статус «у провайдера», но не заметки")
        await c.post(f"/api/admin/tickets/{tid}/messages", headers=HA, json={"text": "Провайдер перезапустил узел, проверьте"})
        ok(any(u == 4 and f"ticket={tid}" in url and "Ответ поддержки" in t for u, t, url in btns), "ответ ушёл клиенту с кнопкой «Открыть обращение»")
        lst = (await c.get("/api/me/tickets", headers=H4)).json(); ok(lst[0]["unread"] and lst[0]["status"] == "answered", "у клиента отметка о новом ответе")
        await c.get(f"/api/me/tickets/{tid}", headers=H4)
        lst = (await c.get("/api/me/tickets", headers=H4)).json(); ok(not lst[0]["unread"], "после просмотра отметка снята")
        await c.post(f"/api/me/tickets/{tid}/messages", headers=H4, json={"text": "Заработало, спасибо"})
        d = (await c.get(f"/api/admin/tickets/{tid}", headers=HA)).json(); ok(d["status"] == "open", "ответ клиента возвращает тикет в «ждёт ответа»")
        await c.post(f"/api/admin/tickets/{tid}/status", headers=HA, json={"status": "closed"})
        ok(any(u == 4 and "решено" in t for u, t in msgs), "клиенту сообщили о закрытии")
        H5 = {"X-Init-Data": init(5)}; await c.get("/api/me", headers=H5); await c.post("/api/me/consent", headers=H5, json={"docs": ["terms", "aup", "privacy", "pd"]})
        r = await c.get(f"/api/me/tickets/{tid}", headers=H5); ok(r.status_code == 404, "чужое обращение недоступно")
        # сообщение боту без открытых обращений создаёт новое
        from app import tickets as tk
        t2 = await tk.create(ctx, 4, "", "Как сменить ОС?", source="bot")
        ok(t2.subject.startswith("Как сменить ОС"), "тема берётся из первого сообщения")
        # ───── персонально без наценки ─────
        async with D.session() as s:
            srv = (await s.execute(select(D.Server).where(D.Server.astra_id == 777))).scalar_one()
            srv.cost_kop = 67500; srv.price_kop = 95000; await s.commit()
        price = lambda cat, idx: next(o["price_kop"] for o in cat["locations"][0]["offers"] if o["idx"] == idx)
        before = (await c.get("/api/me/catalog", headers=H4)).json()
        r = await c.post("/api/admin/users/4/markup", headers=HA, json={"no_markup": True})
        ok(r.status_code == 200 and r.json()["repriced"] and r.json()["repriced"][0]["new_rub"] == 675, f"продление сервера пересчитано по себестоимости ({r.json().get('repriced')})")
        after = (await c.get("/api/me/catalog", headers=H4)).json()
        other = (await c.get("/api/me/catalog", headers=H5)).json()
        ok(price(after, 0) == 18800 and price(before, 0) == 27000 and price(other, 0) == 27000, f"цены без наценки только у этого клиента ({price(before,0)} → {price(after,0)}, у другого {price(other,0)})")
        card = (await c.get("/api/admin/users/4", headers=HA)).json(); ok(card["no_markup"] is True, "в карточке клиента видно «без наценки»")
        async with D.session() as s:
            await D.change_balance(s, 4, 100000, "admin_adjust")
        r = await c.post("/api/me/servers", headers=H4, json={"idx": 0, "location": "nl", "os": "ubuntu2404"})
        h4 = (await c.get("/api/me/history", headers=H4)).json()
        ok(any(x["kind"] == "purchase" and x["amount_rub"] == -188 for x in h4), "покупка списала себестоимость 188 ₽")
        r = await c.post("/api/admin/users/4/markup", headers=HA, json={"no_markup": False})
        ok(all(x["new_rub"] >= 270 for x in r.json()["repriced"]), "при включении наценки цены продления вернулись")
        # ───── оформление ─────
        r = await c.post("/api/admin/settings", headers=HA, json={"values": {"theme_effect": "halloween", "theme_bg": "aurora"}})
        ok(r.status_code == 200, "тема сохранена")
        r = await c.post("/api/admin/settings", headers=HA, json={"values": {"theme_effect": "lasers"}}); ok(r.status_code == 400, "неизвестный эффект отклонён")
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
        r = await c.put("/api/admin/theme/background", headers={**HA, "Content-Type": "image/png"}, content=png)
        ok(r.status_code == 200 and r.json()["theme_bg"] == "image", "своя картинка загружена")
        r = await c.put("/api/admin/theme/background", headers={**HA, "Content-Type": "text/plain"}, content=b"hello"); ok(r.status_code == 400, "не-картинка отклонена")
        r = await c.put("/api/admin/theme/background", headers={**H4, "Content-Type": "image/png"}, content=png); ok(r.status_code == 403, "клиент не может менять фон сервиса")
        r = await c.get("/media/bg"); ok(r.status_code == 200 and r.content == png and r.headers["content-type"] == "image/png", "картинка отдаётся клиентам")
        th = (await c.get("/api/me", headers=H4)).json()["theme"]; ok(th["bg"] == "image" and th["image"].startswith("/media/bg") and th["effect"] == "halloween", "тема приходит в кабинет")
        r = await c.delete("/api/admin/theme/background", headers=HA)
        ok((await c.get("/media/bg")).status_code == 404 and (await c.get("/api/me", headers=H4)).json()["theme"]["bg"] == "none", "картинка удалена, фон сброшен")
        r = await c.get("/static/fx.js"); ok(r.status_code == 200 and "FX" in r.text, "скрипт эффектов отдаётся")
    print("FAILS:", ok.fails)


def test_flows(tmp_path, monkeypatch):
    """Сквозной сценарий: согласия, промокоды, покупка со скидкой, карта, автоплатёж, админка, документы."""
    for k, v in dict(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test",
                     WEBHOOK_SECRET="sec", DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/t.db").items():
        monkeypatch.setenv(k, v)
    from app.config import env
    env.cache_clear()
    ok.fails = 0
    asyncio.run(main())
    assert ok.fails == 0
