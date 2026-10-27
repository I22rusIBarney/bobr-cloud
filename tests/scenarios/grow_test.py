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
    SHO = []
    async def shoffer(uid, text, tid): SHO.append((uid, text, tid))
    ctx.send_share_offer = shoffer
    KB = []
    async def nkb(uid, text, rows): KB.append((uid, text, rows)); msgs.append((uid, text))
    ctx.notify_kb = nkb
    app = build_app(ctx)
    from sqlalchemy import select
    DOCS4 = {"docs": ["terms", "aup", "privacy", "pd"]}
    from app import monitor, retention, analytics, serverlog, confirm
    from app.settings import save_settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        A, B, X = {"X-Init-Data": init(51, "Аня")}, {"X-Init-Data": init(52, "Боря")}, {"X-Init-Data": init(59, "Чужой")}
        for h in (A, B, X): await c.get("/api/me", headers=h); await c.post("/api/me/consent", headers=h, json=DOCS4)
        async with D.session() as s:
            await D.change_balance(s, 51, 300000, "admin_adjust"); await D.change_balance(s, 52, 100000, "admin_adjust")
        r = await c.post("/api/me/servers", headers=A, json={"idx": 1, "location": "nl", "os": "ubuntu2404"}); sid = r.json()["id"]
        async with D.session() as s:
            srv = await s.get(D.Server, sid); srv.status = "active"; srv.ip = "10.0.0.1"; await s.commit()
        # ── журнал
        await c.post(f"/api/me/servers/{sid}/action", headers=A, json={"action": "reboot"})
        await c.post(f"/api/me/servers/{sid}/autorenew", headers=A, json={"value": False})
        r = await c.post(f"/api/me/servers/{sid}/share", headers=A, json={"to": "52"}); await c.post(f"/api/me/shares/{r.json()['id']}/accept", headers=B)
        await c.post(f"/api/me/servers/{sid}/renew", headers=B)
        r = await c.get(f"/api/me/servers/{sid}/log", headers=A); L = r.json()
        acts = [x["action"] for x in L]
        ok(r.status_code == 200 and {"purchase", "reboot", "autorenew_off", "share_invite", "share_join", "renew"} <= set(acts), "журнал: покупка, перезагрузка, автопродление, доступ, продление")
        ok(next(x for x in L if x["action"] == "renew")["who"] == "Боря", "журнал: продление записано на участника")
        r = await c.get(f"/api/me/servers/{sid}/log", headers=B); ok(r.status_code == 200 and len(r.json()) == len(L), "участник видит журнал")
        r = await c.get(f"/api/me/servers/{sid}/log", headers=X); ok(r.status_code in (400, 404), "посторонний журнал не видит")
        # ── мониторинг
        state = {"up": False}
        async def probe(ip): return state["up"]
        KB.clear()
        async with D.session() as s:
            x = await s.get(D.Server, sid); x.mon_quiet_until = None; await s.commit()
        for _ in range(2): await monitor.tick(ctx, probe)
        ok(not KB, "мониторинг: две неудачи подряд — ещё не тревога")
        await monitor.tick(ctx, probe)
        ok(len(KB) == 2 and {u for u, _, _ in KB} == {51, 52} and "не отвечает" in KB[0][1], "мониторинг: третья неудача — сообщение владельцу и участнику")
        own = next(rows for u, _, rows in KB if u == 51); mem = next(rows for u, _, rows in KB if u == 52)
        ok(any(d == f"s:a:{sid}:reboot" for row in own for _, d in row) and any(d.startswith("mon:off") for row in own for _, d in row)
           and not any(d.startswith("mon:off") for row in mem for _, d in row), "в тревоге кнопка перезагрузки; «не следить» — только владельцу")
        await monitor.tick(ctx, probe); ok(len(KB) == 2, "повторно о том же падении не пишем")
        j = (await c.get("/api/me", headers=A)).json(); ok(j["servers"][0]["down_since"], "кабинет: сервер помечен как не отвечающий")
        state["up"] = True; msgs.clear()
        await monitor.tick(ctx, probe)
        ok(sum(1 for u, t in msgs if "снова отвечает" in t) == 2, "восстановление — сообщение обоим")
        ok({"monitor_down", "monitor_up"} <= {x["action"] for x in (await c.get(f"/api/me/servers/{sid}/log", headers=A)).json()}, "падение и восстановление в журнале")
        # выключен владельцем — не тревога
        state["up"] = False; KB.clear()
        async def stopped(sid_): return {"id": sid_, "status": "active", "power_status": "stopped"}
        orig = ctx.astra.server; ctx.astra.server = stopped
        for _ in range(4): await monitor.tick(ctx, probe)
        ok(not KB, "сервер выключен клиентом — тревоги нет")
        ctx.astra.server = orig
        async with D.session() as s:
            x = await s.get(D.Server, sid); x.mon_quiet_until = None; x.mon_fail = 0; await s.commit()
        await c.post(f"/api/me/servers/{sid}/action", headers=A, json={"action": "start"})
        for _ in range(4): await monitor.tick(ctx, probe)
        ok(not KB, "после включения/перезагрузки мониторинг затихает")
        r = await c.post(f"/api/me/servers/{sid}/monitor", headers=B, json={"value": False}); ok(r.status_code == 400, "участник не выключает мониторинг")
        r = await c.post(f"/api/me/servers/{sid}/monitor", headers=A, json={"value": False}); ok(r.status_code == 200 and r.json()["monitor"] is False, "владелец выключил мониторинг")
        async with D.session() as s:
            x = await s.get(D.Server, sid); x.mon_quiet_until = None; await s.commit()
        for _ in range(4): await monitor.tick(ctx, probe)
        ok(not KB, "выключенный мониторинг не тревожит")
        ok(await monitor.probe("127.0.0.1") is True, "проверка: отказ в соединении = сервер в сети")
        # ── коды подтверждения
        r = await c.post(f"/api/me/servers/{sid}/action", headers=A, json={"action": "password"}); ok(r.status_code == 428, "сброс пароля без кода — нужен код")
        msgs.clear()
        r = await c.post(f"/api/me/servers/{sid}/code", headers=A, json={"action": "password"}); ok(r.status_code == 200 and msgs and msgs[-1][0] == 51, "код отправлен в бот владельцу")
        import re as _re
        code = _re.search(r"<code>(\d{6})</code>", msgs[-1][1]).group(1)
        r = await c.post(f"/api/me/servers/{sid}/action", headers=A, json={"action": "password", "code": "000000" if code != "000000" else "111111"}); ok(r.status_code == 428 and "Осталось попыток: 4" in r.json()["detail"], "неверный код — попытки уменьшаются")
        r = await c.post(f"/api/me/servers/{sid}/action", headers=A, json={"action": "password", "code": code}); ok(r.status_code == 200, "верный код — действие выполнено")
        r = await c.post(f"/api/me/servers/{sid}/action", headers=A, json={"action": "password", "code": code}); ok(r.status_code == 428, "код одноразовый")
        r = await c.post(f"/api/me/servers/{sid}/code", headers=B, json={"action": "delete"}); ok(r.status_code in (400, 404), "участник код на удаление не получит")
        r = await c.post(f"/api/me/servers/{sid}/action", headers=B, json={"action": "reboot"}); ok(r.status_code == 200, "обычные действия — без кода")
        r = await c.post(f"/api/me/servers/{sid}/code", headers=A, json={"action": "reboot"}); ok(r.status_code == 400, "для безопасных действий код не выдаётся")
        # ── объявление
        async with D.session() as s: await save_settings(s, {"announce_text": "Плановые работы 28.10", "announce_level": "warn", "announce_until": "2099-01-01"}); await s.commit()
        j = (await c.get("/api/me", headers=A)).json(); ok(j["announce"] and j["announce"]["level"] == "warn", "объявление в кабинете")
        async with D.session() as s: await save_settings(s, {"announce_until": "2020-01-01"}); await s.commit()
        j = (await c.get("/api/me", headers=A)).json(); ok(j["announce"] is None, "истёкшее объявление не показывается")
        # ── брошенная оплата
        C = {"X-Init-Data": init(70, "Галя")}; await c.get("/api/me", headers=C); await c.post("/api/me/consent", headers=C, json={**DOCS4, "ads": True})
        async with D.session() as s:
            u = await s.get(D.User, 70); u.ads_consent = True
            p = D.Payment(yk_id="pp1", tg_id=70, amount_kop=50000, status="pending", created_at=D.utcnow() - timedelta(hours=2))
            old = D.Payment(yk_id="pp0", tg_id=70, amount_kop=30000, status="canceled", created_at=D.utcnow() - timedelta(hours=3))
            s.add_all([p, old]); await s.commit(); pid = p.id
        KB.clear(); await retention.abandoned(ctx)
        ok(len(KB) == 1 and KB[0][0] == 70 and any(d == "t:500" for row in KB[0][2] for _, d in row), "через час — одно напоминание о последней попытке с кнопкой оплаты")
        await retention.abandoned(ctx); ok(len(KB) == 1, "напоминание не повторяется")
        async with D.session() as s:
            p = await s.get(D.Payment, pid); p.created_at = D.utcnow() - timedelta(hours=25); await s.commit()
        KB.clear(); await retention.abandoned(ctx)
        async with D.session() as s: u = await s.get(D.User, 70)
        ok(KB and "Скидка 10%" in KB[0][1] and u.promo_discount_pct == 10 and u.promo_until, "через сутки — персональная скидка 10% со сроком")
        j = (await c.get("/api/me", headers=C)).json(); ok(j["discount"] and j["discount"]["until"], "кабинет видит скидку со сроком")
        async with D.session() as s:
            u = await s.get(D.User, 70); u.promo_until = D.utcnow() - timedelta(minutes=1); await s.commit()
        j = (await c.get("/api/me", headers=C)).json(); ok(j["discount"] is None, "истёкшая скидка не показывается")
        async with D.session() as s: await D.change_balance(s, 70, 100000, "admin_adjust")
        r = await c.post("/api/me/servers", headers=C, json={"idx": 0, "location": "nl", "os": "ubuntu2404"})
        async with D.session() as s:
            spent = 100000 - (await s.get(D.User, 70)).balance_kop
            sv = await s.get(D.Server, r.json()["id"])
        ok(spent == sv.price_kop, "истёкшая скидка к покупке не применяется")
        await retention.expire_discounts()
        async with D.session() as s: u = await s.get(D.User, 70)
        ok(u.promo_discount_pct == 0, "истёкшая скидка снимается")
        async with D.session() as s:
            s.add(D.Payment(yk_id="pp9", tg_id=52, amount_kop=10000, status="pending", created_at=D.utcnow() - timedelta(hours=8))); await s.commit()
        KB.clear(); await retention.abandoned(ctx); ok(not any(u == 52 for u, _, _ in KB), "старые незавершённые оплаты (до обновления) не напоминаем")
        # ── возврат ушедших
        W = {"X-Init-Data": init(80, "Дима")}; await c.get("/api/me", headers=W); await c.post("/api/me/consent", headers=W, json=DOCS4)
        async with D.session() as s:
            u = await s.get(D.User, 80); u.ads_consent = True
            s.add(D.Server(astra_id=9001, tg_id=80, name="old", location="nl", price_kop=27000, status="deleted", ip="1.2.3.4", deleted_at=D.utcnow() - timedelta(days=20), created_at=D.utcnow() - timedelta(days=80)))
            s.add(D.Server(astra_id=9002, tg_id=51, name="old2", location="nl", price_kop=27000, status="deleted", ip="1.2.3.5", deleted_at=D.utcnow() - timedelta(days=20)))
            await s.commit()
        KB.clear(); n = await retention.winback(ctx, force=True)
        ok(n == 1 and KB[0][0] == 80 and "15%" in KB[0][1], "ушедшему 20 дней назад — предложение со скидкой 15%")
        ok(not any(u == 51 for u, _, _ in KB), "у кого есть работающий сервер — не пишем")
        KB.clear(); n = await retention.winback(ctx, force=True); ok(n == 0, "повторно не раньше чем через 90 дней")
        # ── аналитика
        a = await analytics.build()
        cur = a["series"][-1]
        ok(len(a["series"]) == 12 and cur["revenue_rub"] > 0 and cur["new_clients"] >= 4, "аналитика: 12 месяцев, выручка и новые клиенты текущего месяца")
        ok(a["kpi"]["mrr_rub"] > 0 and a["kpi"]["active_servers"] >= 1, "аналитика: MRR и активные серверы")
        ok(a["sources"]["abandon"]["sent"] == 1 and a["sources"]["abandon"]["won"] == 1 and a["sources"]["winback"]["sent"] == 1, "аналитика: персональные скидки и конверсия")
        r = await c.get("/api/admin/analytics", headers={"X-Init-Data": init(1, "Админ")}); ok(r.status_code == 200 and r.json()["series"], "API аналитики для админа")
        r = await c.get("/api/admin/analytics", headers=A); ok(r.status_code in (401, 403), "не админу аналитика закрыта")
        # ── удаление с кодом
        r = await c.delete(f"/api/me/servers/{sid}", headers=A); ok(r.status_code == 428, "удаление без кода — нужен код")
        msgs.clear(); await c.post(f"/api/me/servers/{sid}/code", headers=A, json={"action": "delete"})
        code = _re.search(r"<code>(\d{6})</code>", msgs[-1][1]).group(1)
        r = await c.delete(f"/api/me/servers/{sid}?code={code}", headers=A); ok(r.status_code == 200, "удаление с кодом")
        async with D.session() as s: x = await s.get(D.Server, sid)
        ok(x.deleted_at is not None, "дата удаления записана (для возврата ушедших)")
        async with D.session() as s: await save_settings(s, {"confirm_codes": False}); await s.commit()
        r = await c.post("/api/me/servers", headers=A, json={"idx": 0, "location": "nl", "os": "ubuntu2404"}); s2 = r.json()["id"]
        async with D.session() as s:
            x = await s.get(D.Server, s2); x.status = "active"; await s.commit()
        r = await c.post(f"/api/me/servers/{s2}/action", headers=A, json={"action": "password"}); ok(r.status_code == 200, "коды выключены в админке — без кода")
    print("FAILS:", ok.fails)

asyncio.run(main())
