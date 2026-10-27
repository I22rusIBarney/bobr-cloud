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
    POSTS = []
    async def post(chat, text): POSTS.append((chat, text)); return None
    async def ping(): return {"ok": True}
    async def ykcheck(): return 200, {}
    ctx.post, ctx.tg_ping = post, ping
    ctx.yk.check = ykcheck
    from app import health, security, events
    from app.settings import save_settings
    health.CONFIRM_DELAY = 0
    https_ok = {"v": True}
    async def https(c): return health._res(https_ok["v"], 12, "" if https_ok["v"] else "ответ 502")
    health.CHECKS["https"] = ("HTTPS-сайт бота", https)
    app = build_app(ctx)
    async with D.session() as s: await save_settings(s, {"ev_health_chat": "@bobr_status", "ev_security_chat": "@bobr_sec", "health_interval_min": 1})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        HA = {"X-Init-Data": init(1, "Админ")}
        await health.tick(ctx)
        daily = [t for ch, t in POSTS if ch == "@bobr_status"]
        ok(len(daily) == 1 and "Healthcheck" in daily[0] and "Astra API" in daily[0] and "⚪️ Мой налог" in daily[0], "первая проверка: суточная сводка, «Мой налог» — не настроен")
        ok("bcloud harden" in daily[0], "сводка напоминает про защиту сервера")
        POSTS.clear(); health._last_run = 0
        https_ok["v"] = False
        await health.tick(ctx)
        ok(len(POSTS) == 1 and "❌ <b>HTTPS-сайт бота</b>" in POSTS[0][1] and "ответ 502" in POSTS[0][1], "сбой: сообщение с причиной")
        POSTS.clear(); health._last_run = 0
        await health.tick(ctx); ok(not POSTS, "сбой продолжается — не повторяем")
        https_ok["v"] = True; health._last_run = 0
        await health.tick(ctx); ok(len(POSTS) == 1 and "восстановлено" in POSTS[0][1], "восстановление: сообщение")
        async def slow(): raise asyncio.TimeoutError()
        ctx.astra.balance = slow; health._last_run = 0; POSTS.clear()
        await health.tick(ctx); ok(POSTS and "Astra API" in POSTS[0][1] and "нет ответа" in POSTS[0][1], "таймаут Astra — сбой")
        r = (await c.get("/api/admin/health?fresh=1", headers=HA)).json()
        ok(len(r["checks"]) == 9 and r["security"]["hardened"] is None, "экран состояния в админке")
        # безопасность
        import os, json, time
        d = health._data_dir(); f = os.path.join(d, "host-events.jsonl")
        with open(os.path.join(d, ".hardened"), "w") as h: json.dump({"ts": int(time.time()), "ssh_port": 22, "password_login": False}, h)
        with open(f, "a") as h:
            h.write(json.dumps({"type": "login", "user": "root", "ip": "1.2.3.4", "ts": time.time()}) + "\n")
            h.write("мусор\n")
        POSTS.clear(); await security.tick(ctx)
        ok(len(POSTS) == 1 and POSTS[0][0] == "@bobr_sec" and "новый IP" in POSTS[0][1] and "1.2.3.4" in POSTS[0][1], "вход по SSH с нового IP — предупреждение")
        ok(os.path.getsize(f) == 0, "файл событий очищен после чтения")
        with open(f, "a") as h: h.write(json.dumps({"type": "login", "user": "root", "ip": "1.2.3.4", "ts": time.time()}) + "\n")
        POSTS.clear(); await security.tick(ctx)
        ok(len(POSTS) == 1 and "новый IP" not in POSTS[0][1], "знакомый IP — просто уведомление")
        with open(f, "a") as h:
            for i in range(25): h.write(json.dumps({"type": "ban", "ip": f"9.9.9.{i}", "jail": "sshd", "ts": time.time()}) + "\n")
        POSTS.clear(); await security.tick(ctx)
        ok(len(POSTS) == 1 and "Подбор паролей" in POSTS[0][1] and "25" in POSTS[0][1], "массовый подбор — предупреждение об атаке")
        POSTS.clear(); await security.tick(ctx); ok(not POSTS, "об атаке не спамим")
        line = await security.daily_line(); ok("заблокировано IP — 25" in line and "входов по SSH — 2" in line, "суточная сводка безопасности")
        r = (await c.get("/api/admin/health", headers=HA)).json(); ok(r["security"]["hardened"]["ssh_port"] == 22 and r["security"]["bans_24h"] == 25, "безопасность в админке")
    print("FAILS:", ok.fails)

asyncio.run(main())
