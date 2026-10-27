import asyncio, os, sys, json, time, hmac, hashlib
from urllib.parse import urlencode
from datetime import timedelta
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from app import db as D, services as svc

def init(uid, name="Иван"):
    f = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": name, "username": f"user{uid}"}), "query_id": "q"}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
    sec = hmac.new(b"WebAppData", b"123:TEST", hashlib.sha256).digest()
    return urlencode({**f, "hash": hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest()})

PRESETS = [{"idx": 0, "cpu": 1, "ram": 1, "disk": 10, "price_month_rub": {"nl": 188, "cz": 188}}, {"idx": 1, "cpu": 2, "ram": 4, "disk": 60, "price_month_rub": {"nl": 675, "cz": 675}}]
class FakeAstra:
    def __init__(s): s.n = 100
    async def catalog(s): return {"locations": [{"code": "nl", "name": "🇳🇱 Нидерланды", "channel": "до 25 Гбит/с · shared, не гарантирован", "available": True}, {"code": "cz", "name": "🇨🇿 Чехия", "channel": "до 10 Гбит/с", "available": True, "virtual": True, "description": "IP Чехии · сервер во Франкфурте (DE)"}, {"code": "ch", "name": "🇨🇭 Швейцария", "available": False, "virtual": True, "description": "IP Швейцарии · сервер во Франкфурте (DE)"}], "presets": PRESETS, "os": ["ubuntu2404", "win2022"], "configurator": {"cpu": {"min": 1, "max": 16, "step": 1}, "ram": {"min": 1, "max": 64, "step": 1}, "disk": {"min": 20, "max": 1024, "step": 10}}}
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


from html.parser import HTMLParser
ALLOWED = {"b", "i", "s", "u", "code", "pre", "a", "tg-spoiler"}
class V(HTMLParser):
    def __init__(s): super().__init__(convert_charrefs=True); s.stack = []; s.err = []
    def handle_starttag(s, t, a):
        if t not in ALLOWED: s.err.append("tag " + t)
        if s.stack and s.stack[-1] in ("code", "pre"): s.err.append("nested in code")
        s.stack.append(t)
    def handle_endtag(s, t):
        if not s.stack or s.stack[-1] != t: s.err.append("bad close " + t)
        else: s.stack.pop()
def check_html(txt):
    v = V(); v.feed(txt); v.close()
    if v.stack: v.err.append("unclosed " + str(v.stack))
    import re
    if re.search(r"<(?![/a-z])", txt): v.err.append("raw <")
    return v.err

LOG = []; fails = []
def rec(where, text, kb=None, html=True):
    errs = check_html(text) if html else []
    if len(text) > 4096: errs.append("too long")
    for row in (kb.inline_keyboard if kb else []):
        for b in row:
            cd = getattr(b, "callback_data", None)
            if cd and len(cd.encode()) > 64: errs.append("callback >64: " + cd)
    if errs: fails.append((where, errs))
    btns = [" | ".join(b.text for b in row) for row in (kb.inline_keyboard if kb else [])]
    LOG.append(f"━━ {where} ━━\n{text}\n" + "".join(f"  [{x}]\n" for x in btns))

class U:
    def __init__(s, i): s.id = i; s.username = "ivan"; s.first_name = "Иван"; s.last_name = None
from aiogram.types import Message as _M, CallbackQuery as _C
class Msg(_M):
    def __init__(s, uid, text=""): s.from_user = U(uid); s.text = text
    async def answer(s, text, reply_markup=None, parse_mode=None, **k): rec("answer:" + (s.text or "")[:20], text, reply_markup, parse_mode == "HTML")
    async def edit_text(s, text, reply_markup=None, parse_mode=None, **k): rec("edit:" + s.where, text, reply_markup, parse_mode == "HTML")
    async def edit_reply_markup(s, **k): pass
class Cb(_C):
    def __init__(s, uid, data): s.from_user = U(uid); s.data = data; s.message = Msg(uid); s.message.where = data
    async def answer(s, text=None, show_alert=False, **k):
        if text:
            LOG.append(f"   (всплывающее: {text})")
            if len(text) > 200: fails.append((s.data, ["callback answer >200"]))

class St:
    async def clear(s): pass
    async def set_state(s, x): pass

msgs = []
async def notify(uid, t): msgs.append(t); rec("уведомление", t)
async def alert(t): pass
async def nbtn(uid, t, b, u): msgs.append(t); rec("уведомление с кнопкой [" + b + "]", t)

async def main():
    await D.init_db()
    ctx = svc.Ctx(astra=FakeAstra(), yk=FakeYK(), nalog=FakeNalog(), notify=notify, alert_admin=alert)
    ctx.notify_btn = nbtn; ctx.bot_username = "bobrcloud_bot"
    from app import bot as B, account as acc, tickets as tk, referral as R
    cat0 = ctx.astra.catalog
    async def cat(): c = await cat0(); c["periods"] = ["1m", "3m", "6m", "1y"]; return c
    ctx.astra.catalog = cat
    B.set_ctx(ctx)
    UID = 42
    async with D.session() as s:
        u = await D.get_or_create_user(s, UID, "ivan"); u.first_name = "Иван"; await s.commit()
    await acc.accept_consent(UID, "bot", ["terms", "aup", "privacy", "pd"])
    st = St()
    rec("согласие (экран)", B.CONSENT_TEXT, B.consent_kb(UID))
    await B.start(Msg(UID, "/start"), st)
    await B.home(Cb(UID, "m:home"), st)
    await B.buy_locations(Cb(UID, "m:buy"))
    await B.location_closed(Cb(UID, "b:w:ch"))
    await B.location_watch(Cb(UID, "lw:on:ch"))
    await B.buy_locations(Cb(UID, "m:buy"))
    await B.buy_tariffs(Cb(UID, "b:l:nl"))
    await B.buy_tariffs(Cb(UID, "b:l:cz"))
    await B.start(Msg(UID, "/start loc_cz"), st)
    await B.start(Msg(UID, "/start loc_<b>"), st)
    await B.buy_os(Cb(UID, "b:p:nl:1"))
    await B.buy_os(Cb(UID, "b:p:nl:0"))
    await B.buy_confirm(Cb(UID, "b:o:nl:1:ubuntu2404"))   # не хватает
    await B.topup_menu(Cb(UID, "m:topup"), st)
    await B.topup_custom(Cb(UID, "t:custom"), st)
    await B.topup_amount(Msg(UID, "abc"), st)
    # PROVIDERS_TEST: два способа оплаты — бот спрашивает, каким платить
    class PG:
        configured = True
        async def create_payment(self, **kw): return {"id": "tx-9", "confirmation": {"confirmation_url": "https://pay.platega.io/tx-9"}}
    ctx.platega = PG()
    from app.settings import save_settings as _ss
    async with D.session() as s: await _ss(s, {"pay_platega_on": True})
    await B.topup_preset(Cb(UID, "t:500"))
    await B.topup_provider(Cb(UID, "t:pv:500:platega"))
    async with D.session() as s: await _ss(s, {"pay_platega_on": False})
    await B.topup_preset(Cb(UID, "t:3000"))
    pid = list(ctx.yk.pays)[-1]
    async with D.session() as s:
        from sqlalchemy import select
        pay = (await s.execute(select(D.Payment).where(D.Payment.yk_id == pid))).scalar_one()
    await B.topup_check(Cb(UID, f"t:chk:{pay.id}"))
    await B.topup_check(Cb(UID, f"t:chk:{pay.id}"))
    async with D.session() as s:
        await D.change_balance(s, UID, 0, "x")
        u = await s.get(D.User, UID); u.promo_discount_pct = 20; u.promo_code = "SALE20"; await s.commit()
    await B.buy_confirm(Cb(UID, "b:o:nl:1:ubuntu2404"))   # хватает, со скидкой
    await B.buy_confirm(Cb(UID, "b:o:nl:1:ubuntu2404:3m"))
    await B.buy_go(Cb(UID, "b:go:nl:1:ubuntu2404:3m"))
    await svc.sync_servers(ctx)
    await B.servers_list(Cb(UID, "s:list"))
    async with D.session() as s:
        srv = (await s.execute(select(D.Server).where(D.Server.tg_id == UID))).scalars().first()
    i = srv.id
    await B.server_card(Cb(UID, f"s:v:{i}"))
    await B.toggle_autorenew(Cb(UID, f"s:ar:{i}"))
    await B.confirm_action(Cb(UID, f"s:c:{i}:change-ip"))
    await B.confirm_action(Cb(UID, f"s:c:{i}:password"))
    await B.simple_action(Cb(UID, f"s:a:{i}:reboot"))
    ctx.astra.action = (lambda *a, **k: _vnc())
    await B.simple_action(Cb(UID, f"s:a:{i}:vnc"))
    await B.reinstall_choose(Cb(UID, f"s:ri:{i}"))
    await B.reinstall_confirm(Cb(UID, f"s:rc:{i}:ubuntu2404"))
    await B.renew_now(Cb(UID, f"s:rn:{i}"))
    await B.delete_confirm(Cb(UID, f"s:d:{i}"))
    await B.delete_do(Cb(UID, f"s:dy:{i}"))
    await B.servers_list(Cb(UID, "s:list"))
    await B.support_menu(Cb(UID, "m:support"), st)
    await B.support_message(Msg(UID, "Не работает SSH <b>"))
    await B.support_media(Msg(UID, ""))
    t = await tk.latest_open(UID)
    await tk.admin_reply(ctx, t.id, "Посмотрели, всё исправили & проверили <ok>.")
    await tk.set_status(ctx, t.id, "closed")
    async with D.session() as s: await D.get_or_create_user(s, 1, "admin")
    info = {"link": "https://t.me/bobrnews", "title": "Bobr News"}
    rec("подписка (экран)", B.subscribe_text(info), B.subscribe_kb(info))
    async def no(chat, uid): return False
    ctx.check_member = no
    from app.settings import save_settings
    async with D.session() as s: await save_settings(s, {"sub_required": True, "sub_chat": "@bobrnews"})
    await B.subscribe_check(Cb(UID, "sub:check"), st)
    async def yes(chat, uid): return True
    ctx.check_member = yes
    await B.subscribe_check(Cb(UID, "sub:check"), st)
    await B.news_toggle(Cb(UID, "nw:off"))
    # TRANSFER_TEST: передача сервера
    OFF = []
    async def _offer(uid, text, tid): OFF.append(tid); rec("запрос получателю", text, __import__("aiogram.types", fromlist=["x"]).InlineKeyboardMarkup(inline_keyboard=[[__import__("aiogram.types", fromlist=["x"]).InlineKeyboardButton(text="✅ Принять", callback_data=f"tr:a:{tid}"), __import__("aiogram.types", fromlist=["x"]).InlineKeyboardButton(text="❌ Отклонить", callback_data=f"tr:d:{tid}")]]))
    ctx.send_transfer_offer = _offer
    async with D.session() as s:
        f = await D.get_or_create_user(s, 43, "friend43"); f.first_name = "Друг"; await s.commit()
        srv = D.Server(astra_id=777, tg_id=UID, name="vpn-home", location="de", cpu=1, ram=1, disk=10, os="ubuntu2404", price_kop=27000, status="active", ip="9.9.9.9", expires_at=D.utcnow() + __import__("datetime").timedelta(days=20))
        s.add(srv); await s.commit(); await s.refresh(srv); sid = srv.id
    await acc.accept_consent(43, "bot", ["terms", "aup", "privacy", "pd"])
    class St2:
        data = {}
        async def clear(self): pass
        async def set_state(self, x): pass
        async def update_data(self, **k): self.data.update(k)
        async def get_data(self): return self.data
    st2 = St2()
    await B.server_card(Cb(UID, f"s:v:{sid}"), st)
    await B.transfer_start(Cb(UID, f"s:tr:{sid}"), st2)
    await B.transfer_to(Msg(UID, "@nobody"), st2)
    await B.transfer_to(Msg(UID, "@friend43"), st2)
    await B.transfer_send(Cb(UID, f"tr:y:{sid}:43"))
    await B.server_card(Cb(UID, f"s:v:{sid}"), st)
    await B.transfer_answer(Cb(43, f"tr:a:{OFF[-1]}"))
    # SHARE_TEST: общий доступ (сервер теперь у 43 — он владелец, UID — участник)
    SOFF = []
    async def _soffer(uid, text, tid): SOFF.append(tid); rec("приглашение в общий доступ", text, B.InlineKeyboardMarkup(inline_keyboard=[[B.InlineKeyboardButton(text="✅ Принять", callback_data=f"sh:a:{tid}"), B.InlineKeyboardButton(text="❌ Отклонить", callback_data=f"sh:d:{tid}")]]))
    ctx.send_share_offer = _soffer
    st3 = St2()
    await B.server_card(Cb(43, f"s:v:{sid}"), st)
    await B.share_menu(Cb(43, f"s:sh:{sid}"), st3)
    await B.share_start(Cb(43, f"sh:i:{sid}"), st3)
    await B.share_to(Msg(43, f"{UID}"), st3)
    await B.share_send(Cb(43, f"sh:y:{sid}:{UID}"))
    await B.share_answer(Cb(UID, f"sh:a:{SOFF[-1]}"))
    await B.servers_list(Cb(UID, "s:list"))
    await B.server_card(Cb(UID, f"s:v:{sid}"), st)
    await B.renew_now(Cb(UID, f"s:rn:{sid}"))
    await B.server_card(Cb(43, f"s:v:{sid}"), st)
    await B.share_menu(Cb(43, f"s:sh:{sid}"), st3)
    await B.simple_action(Cb(UID, f"s:a:{sid}:vnc"))
    async def _novnc(*a, **k): return {"ok": True, "status": "queued"}
    ctx.astra.action = _novnc
    await B.simple_action(Cb(UID, f"s:a:{sid}:vnc"))
    await B.server_howto(Cb(UID, f"s:hw:{sid}"))
    from app import serverlog as _sl
    await B.server_log_view(Cb(43, f"s:lg:{sid}"))
    await B.monitor_toggle(Cb(43, f"mon:off:{sid}"))
    await B.server_log_view(Cb(UID, f"s:lg:{sid}"))
    async with D.session() as s:
        x = await s.get(D.Server, sid); x.mon_down_at = D.utcnow(); await s.commit()
        await save_settings(s, {"announce_text": "Плановые работы в Германии 28.10 с 02:00 до 04:00 мск", "announce_level": "warn"})
        u = await s.get(D.User, UID); u.promo_discount_pct = 15; u.promo_until = D.utcnow() + __import__("datetime").timedelta(days=7); await s.commit()
    await B.server_card(Cb(UID, f"s:v:{sid}"), st)
    rec("главное меню с объявлением и скидкой", await B.home_text(UID), B.home_kb(False))
    await B.share_leave_confirm(Cb(UID, f"sh:l:{sid}"))
    await B.share_leave(Cb(UID, f"sh:ly:{sid}"))
    # PROFILE_TEST: профиль в боте
    async with D.session() as s:
        s.add(D.PaymentMethod(tg_id=UID, yk_id="pm_1", title="MasterCard *4444", last4="4444", expiry="12/29", active=True))
        u = await s.get(D.User, UID); u.autopay_enabled = True; await s.commit()
    await save_settings(D.session().__aenter__.__self__ if False else None, {}) if False else None
    async with D.session() as s: await save_settings(s, {"autopay_available": True, "sub_required": False, "yk_method": "all"})
    await B.profile_home(Cb(UID, "p:home"), st)
    await B.profile_methods(Cb(UID, "p:methods"))
    await B.method_delete_ask(Cb(UID, "p:md:1"))
    await B.method_delete(Cb(UID, "p:mdy:1"))
    await B.method_delete_all_ask(Cb(UID, "p:mda"))
    await B.method_delete_all(Cb(UID, "p:mday"))
    await B.autopay_toggle(Cb(UID, "p:ap"))
    await B.profile_history(Cb(UID, "p:hist"))
    await B.profile_promo(Cb(UID, "p:promo"), st)
    await B.profile_promo_code(Msg(UID, "NOPE"), st)
    await B.profile_ref(Cb(UID, "p:ref"))
    await B.profile_news(Cb(UID, "p:news"))
    await B.profile_docs(Cb(UID, "p:docs"))
    await B.topup_preset(Cb(UID, "t:500"))
    await B.topup_go(Cb(UID, "t:go:500:yookassa:1"))
    rec("главное меню (админ)", await B.home_text(1), B.home_kb(True))
    print("\n".join(LOG))
    print("\nПРОБЛЕМЫ:", fails if fails else "нет")

async def _vnc(): return {"url": "https://vnc.example/abc?x=1&y=2"}
asyncio.run(main())
