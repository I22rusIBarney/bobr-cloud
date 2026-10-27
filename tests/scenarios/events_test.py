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
    POSTS, FILES = [], []
    async def post(chat, text):
        if chat == "@noaccess": return "Forbidden: bot is not a member of the channel chat"
        POSTS.append((chat, text)); return None
    async def post_file(chat, path, caption):
        import gzip, sqlite3, os, shutil
        dst = os.path.join(os.path.dirname(sys.argv[1]), "restored.db")
        with gzip.open(path) as g, open(dst, "wb") as f: shutil.copyfileobj(g, f)
        c = sqlite3.connect(dst); keys = [r[0] for r in c.execute("select key from settings")]; users = c.execute("select count(*) from users").fetchone()[0]; c.close()
        FILES.append((chat, os.path.basename(path), caption, keys, users)); return None
    async def nbtn(uid, t, b, u): msgs.append(("btn", uid, t))
    ctx.post, ctx.post_file, ctx.notify_btn = post, post_file, nbtn
    from app import events, backup
    ctx.alert_admin = events.make_alert(ctx)
    app = build_app(ctx)
    from app.settings import save_settings, set_secrets
    DOCS4 = {"docs": ["terms", "aup", "privacy", "pd"]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        HA = {"X-Init-Data": init(1, "Админ")}
        H = {"X-Init-Data": init(21, "Пётр")}
        await c.get("/api/me", headers=H); await c.post("/api/me/consent", headers=H, json=DOCS4)
        async with D.session() as s:
            await save_settings(s, {"ev_sales_chat": "@bobr_sales", "ev_payments_chat": "-1001234567890", "ev_tickets_chat": "", "ev_referrals_on": False, "ev_alerts_chat": "@noaccess"})
            await set_secrets(s, {"yk_shop_id": "1", "yk_secret": "SUPERSECRET"})
        info = (await c.get("/api/admin/events", headers=HA)).json(); ok(len(info["events"]) == 9 and any(e["key"] == "locations" for e in info["events"]), "9 типов событий в админке, есть «Локации»")
        r = (await c.post("/api/admin/events/test", headers=HA, json={"event": "sales", "chat": "@bobr_sales"})).json(); ok(r["ok"] and POSTS[-1][0] == "@bobr_sales", "проверка канала событий")
        r = (await c.post("/api/admin/events/test", headers=HA, json={"event": "alerts", "chat": "@noaccess"})).json(); ok(not r["ok"] and "администратором" in r["error"], "понятная ошибка, если бот не в канале")
        POSTS.clear(); msgs.clear()
        await c.post("/api/me/topup", headers=H, json={"amount_rub": 2000, "save_method": False})
        await svc.credit_payment(ctx, list(ctx.yk.pays)[-1])
        ok(any(ch == -1001234567890 and "Пополнение" in t and "Пётр" in t for ch, t in POSTS), "пополнение — в канал по ID")
        r = await c.post("/api/me/servers", headers=H, json={"idx": 1, "location": "nl", "os": "ubuntu2404"})
        ok(any(ch == "@bobr_sales" and "Покупка" in t and "Маржа" in t for ch, t in POSTS), "покупка — в канал продаж с маржой")
        r = await c.post("/api/me/tickets", headers=H, json={"subject": "Вопрос", "text": "Как подключиться?"})
        ok(any(x[0] == "btn" and "Обращение" in x[2] for x in msgs), "обращения без канала — админам с кнопкой")
        await ctx.alert_admin("Тест <алерта>")
        ok(any("Тест &lt;алерта&gt;" in str(m) and "Не удалось отправить в @noaccess" in str(m) for m in msgs), "алерт: канал недоступен — ушло админам с пометкой")
        n = len(POSTS)
        from app import referral as R
        async with D.session() as s: await D.get_or_create_user(s, 22, "f")
        await R.attach(22, "ref_" + R.code_for(21))
        ok(len(POSTS) == n, "выключенное событие не отправляется")
        r = (await c.post("/api/admin/backup", headers=HA)).json()
        ok(r["ok"] and FILES and FILES[-1][1].startswith("bcloud-") and FILES[-1][1].endswith(".db.gz"), f"бэкап отправлен файлом ({r.get('size_kb')} КБ)")
        ok(not any(k.startswith("secret:") for k in FILES[-1][3]) and FILES[-1][4] >= 2, "в копии есть клиенты и нет ключей ЮKassa")
        ok(FILES[-1][0] == 1, "без чата бэкапов — админу в личку")
        async with D.session() as s: await save_settings(s, {"backup_hours": 24})
        before = len(FILES)
        await backup.scheduled(ctx); ok(len(FILES) == before, "по расписанию: рано — не повторяем")
        async with D.session() as s:
            row = await s.get(D.Setting, "backup_last_ts"); row.value = "0"; await s.commit()
        await backup.scheduled(ctx); ok(len(FILES) == before + 1, "по расписанию: пора — копия ушла")
        r = await c.post("/api/admin/settings", headers=HA, json={"values": {"backup_hours": 5}}); ok(r.status_code == 400, "неверная частота отклонена")
    print("FAILS:", ok.fails)

asyncio.run(main())
