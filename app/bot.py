"""Telegram-бот (aiogram 3): меню клиента, покупка, пополнение, управление серверами."""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import account as acc
from . import services as svc
from . import tickets as tk
from . import referral
from . import subscription as sub
from . import transfers as tr
from . import shares as sh
from .legal import DOCS, REQUIRED
from .config import env
from .db import Payment, Server, ServerShare, User, change_balance, get_or_create_user, session
from .services import Ctx, UserError
from .settings import announcement, fmt_rub, load_settings

log = logging.getLogger(__name__)
router = Router()
_ctx: Ctx | None = None

# единые статусы: эмодзи + слово
STATUS = {"provisioning": "⚡ Создаётся", "active": "🟢 Активен", "suspended": "🟡 Ожидает оплаты", "expired": "🔴 Остановлен"}
STATUS_ICON = {"provisioning": "⚡", "active": "🟢", "suspended": "🟡", "expired": "🔴"}


def ctx() -> Ctx:
    assert _ctx is not None
    return _ctx


def set_ctx(c: Ctx) -> None:
    global _ctx
    _ctx = c


def e(x) -> str:
    return html.escape(str(x))


class TopUp(StatesGroup):
    amount = State()


class PromoInput(StatesGroup):
    code = State()


class TransferInput(StatesGroup):
    to = State()


class ShareInput(StatesGroup):
    to = State()


class Guard(BaseMiddleware):
    """Регистрирует пользователя, режет забаненных и не пускает дальше без принятия документов."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        u = getattr(event, "from_user", None)
        if u is None:
            return await handler(event, data)
        async with session() as s:
            is_new = await s.get(User, u.id) is None
            user = await get_or_create_user(s, u.id, u.username)
            first = " ".join(x for x in (u.first_name, u.last_name) if x)[:128] or None
            if first and user.first_name != first:
                user.first_name = first
                await s.commit()
            if user.bot_blocked:  # снова пишет боту — значит, разблокировал: рассылки возобновятся
                user.bot_blocked = False
                await s.commit()
        # пришёл по реферальной ссылке t.me/бот?start=ref_xxx — запоминаем пригласившего (только для новых)
        text = getattr(event, "text", None) or ""
        if is_new and isinstance(event, Message) and text.startswith("/start ") and not user.banned:
            try:
                inviter = await referral.attach(u.id, text.split(maxsplit=1)[1])
                if inviter:
                    await ctx().notify(inviter, referral.joined_text(user))
                    from . import events
                    async with session() as s:
                        inv = await s.get(User, inviter)
                    await events.emit(ctx(), "referrals", f"👋 <b>Новый реферал</b>\n\n{events.who(user)}\nпо ссылке {events.who(inv)}")
            except Exception:
                log.exception("referral attach")
        if user.banned:
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ ограничен", show_alert=True)
            return None
        if await acc.needs_consent(u.id):
            if isinstance(event, CallbackQuery) and (event.data or "").startswith("cs:"):
                return await handler(event, data)
            await show_consent(event)
            return None
        if isinstance(event, CallbackQuery) and event.data in ("sub:check", "nw:off", "nw:on"):
            return await handler(event, data)
        info = await sub.required(ctx(), u.id)
        if info:
            await show_subscribe(event, info)
            return None
        return await handler(event, data)


# ───────────────────────── согласие с документами ─────────────────────────

_checked: dict[int, set[str]] = {}


def consent_kb(tg_id: int) -> InlineKeyboardMarkup:
    base = env().public_url.rstrip("/")
    done = _checked.get(tg_id, set())
    b = InlineKeyboardBuilder()
    for k in (*REQUIRED, "ads"):  # последний пункт — необязательное согласие на рекламу
        mark = "✅" if k in done else "⬜️"
        b.row(InlineKeyboardButton(text=f"{mark} {DOCS[k]['check']}", callback_data=f"cs:t:{k}"))
    # ссылки на документы — отдельными кнопками, чтобы их можно было прочитать до согласия
    links = [InlineKeyboardButton(text="📄 " + t, url=f"{base}/legal/{k}") for k, t in
             (("terms", "Соглашение"), ("aup", "Правила"), ("privacy", "Политика"), ("pd", "Согласие"), ("ads", "Реклама"))]
    if base.startswith("https://"):
        b.row(*links[:2])
        b.row(*links[2:4])
        b.row(links[4])
    all_ok = all(k in done for k in REQUIRED)
    b.row(InlineKeyboardButton(text="✅ Продолжить" if all_ok else "Отметьте обязательные пункты", callback_data="cs:ok"))
    return b.as_markup()


CONSENT_TEXT = (
    "🛡 <b>Добро пожаловать</b>\n\n"
    "Перед началом работы ознакомьтесь с документами и отметьте пункты ниже. "
    "Каждый документ открывается кнопкой 📄.\n"
    "Последний пункт — по желанию: без него не будем присылать новости и акции.\n\n"
    "<i>Без согласия на обработку персональных данных мы не сможем оказывать услуги.</i>"
)


async def show_consent(target: Message | CallbackQuery) -> None:
    uid = target.from_user.id
    _checked.setdefault(uid, set())
    if isinstance(target, CallbackQuery):
        await target.answer("Сначала примите документы", show_alert=False)
        await target.message.answer(CONSENT_TEXT, reply_markup=consent_kb(uid), parse_mode="HTML")
    else:
        await target.answer(CONSENT_TEXT, reply_markup=consent_kb(uid), parse_mode="HTML")


# ───────────────────────── подписка на канал ─────────────────────────


def subscribe_kb(info: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if info.get("link"):
        b.row(InlineKeyboardButton(text="📢 Подписаться", url=info["link"]))
    b.row(InlineKeyboardButton(text="✅ Я подписался", callback_data="sub:check"))
    return b.as_markup()


def subscribe_text(info: dict) -> str:
    return (f"📢 <b>Подпишитесь на {e(info['title'])}</b>\n\n"
            "Там новости сервиса, акции и статус работ.\n"
            "После подписки вернитесь сюда и нажмите «Я подписался».")


async def show_subscribe(target: Message | CallbackQuery, info: dict) -> None:
    if isinstance(target, CallbackQuery):
        await target.answer("Сначала подпишитесь на канал")
        await target.message.answer(subscribe_text(info), reply_markup=subscribe_kb(info), parse_mode="HTML", disable_web_page_preview=True)
    else:
        await target.answer(subscribe_text(info), reply_markup=subscribe_kb(info), parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data == "sub:check")
async def subscribe_check(c: CallbackQuery, state: FSMContext):
    sub.forget(c.from_user.id)
    info = await sub.required(ctx(), c.from_user.id)
    if info:
        await c.answer("Подписка пока не найдена. Подпишитесь и нажмите ещё раз.", show_alert=True)
        return
    await c.answer("✅ Спасибо за подписку!")
    await state.clear()
    try:
        await c.message.edit_text("✅ <b>Подписка подтверждена</b>\n\nДобро пожаловать!", reply_markup=None, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await c.message.answer(await home_text(c.from_user.id), reply_markup=home_kb(c.from_user.id in env().admins), parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("cs:t:"))
async def consent_toggle(c: CallbackQuery):
    k = c.data.split(":")[2]
    if k not in REQUIRED and k != "ads":
        return await c.answer()
    done = _checked.setdefault(c.from_user.id, set())
    done.symmetric_difference_update({k})
    try:
        await c.message.edit_reply_markup(reply_markup=consent_kb(c.from_user.id))
    except TelegramBadRequest:
        pass
    await c.answer()


@router.callback_query(F.data == "cs:ok")
async def consent_ok(c: CallbackQuery, state: FSMContext):
    done = _checked.get(c.from_user.id, set())
    if not all(k in done for k in REQUIRED):
        return await c.answer("Отметьте первые четыре пункта — без них мы не можем оказывать услуги.", show_alert=True)
    try:
        await acc.accept_consent(c.from_user.id, "bot", list(done))
    except UserError as ex:
        return await c.answer(str(ex), show_alert=True)
    _checked.pop(c.from_user.id, None)
    await c.answer("Спасибо!")
    try:
        await c.message.edit_text("✅ <b>Документы приняты</b>\n\nДобро пожаловать — всё готово к работе.", reply_markup=None, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await state.clear()
    await c.message.answer(await home_text(c.from_user.id), reply_markup=home_kb(c.from_user.id in env().admins), parse_mode="HTML", disable_web_page_preview=True)


router.message.middleware(Guard())
router.callback_query.middleware(Guard())


# ───────────────────────── helpers ─────────────────────────


async def show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
        except TelegramBadRequest:
            pass  # "message is not modified"
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


def kb_rows(rows: list[list[tuple[str, str]]], back: str | None = "m:home", back_text: str = "← Назад") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    if back:
        b.row(InlineKeyboardButton(text=back_text, callback_data=back))
    return b.as_markup()


def rub(kop: int) -> str:
    """Сумма с неразрывным пробелом между тысячами: 1 250 ₽."""
    return fmt_rub(kop).replace(" ", "\u00a0")


def loc_name(cat: dict, code: str) -> str:
    l = next((x for x in cat.get("locations", []) if x.get("code") == code), None)
    return (l or {}).get("name") or code.upper()


# прежнее стандартное приветствие — если админ его не менял, показываем новое
_OLD_WELCOME = "Виртуальные серверы в 🇳🇱 🇩🇪 🇪🇪 🇵🇱. Выдача за 1–3 минуты."


async def home_text(tg_id: int) -> str:
    async with session() as s:
        user = await s.get(User, tg_id)
        cfg = await load_settings(s)
    body = cfg["welcome_text"]
    if body.strip() == _OLD_WELCOME:
        from .settings import DEFAULTS
        body = DEFAULTS["welcome_text"]
    ann = announcement(cfg)
    head = (("⚠️ " if ann["level"] == "warn" else "📢 ") + f"<b>{e(ann['text'])}</b>\n\n") if ann else ""
    return (
        head + f"🦫 <b>{e(cfg['service_name'])}</b>\n"
        f"<i>Облачная инфраструктура для ваших проектов.</i>\n\n"
        f"{e(body)}\n\n"
        f"💰 Баланс: <b>{rub(user.balance_kop)}</b>" + _discount_line(user)
    )


def _discount_line(u: User, lead: str = "\n") -> str:
    if not u.promo_discount_pct or (u.promo_until and u.promo_until < svc.utcnow()):
        return ""
    if u.promo_until:
        return f"{lead}🎁 Персональная скидка {u.promo_discount_pct}% на новый сервер до {u.promo_until.astimezone(timezone.utc):%d.%m}"
    return f"{lead}🎁 Скидка {u.promo_discount_pct}% по промокоду {e(u.promo_code or '')} на следующую покупку"


def home_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [("🛒 Купить сервер", "m:buy")],
        [("🖥 Мои серверы", "s:list"), ("💳 Пополнить", "m:topup")],
    ]
    b = InlineKeyboardBuilder.from_markup(kb_rows(rows, back=None))
    app_url = env().public_url.rstrip("/") + "/app"
    if app_url.startswith("https://"):
        b.row(InlineKeyboardButton(text="📱 Личный кабинет", web_app=WebAppInfo(url=app_url)))
    if is_admin and app_url.startswith("https://"):
        admin_btn = InlineKeyboardButton(text="⚙️ Админка", web_app=WebAppInfo(url=env().public_url.rstrip("/") + "/admin"))
    else:
        admin_btn = None
    b.row(InlineKeyboardButton(text="👤 Профиль", callback_data="p:home"), InlineKeyboardButton(text="💬 Поддержка", callback_data="m:support"))
    if admin_btn:  # только администраторам, последней строкой
        b.row(admin_btn)
    return b.as_markup()


# ───────────────────────── старт / меню ─────────────────────────


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    arg = (m.text or "").split(maxsplit=1)[1] if " " in (m.text or "") else ""
    if re.fullmatch(r"loc_[a-z]{2,8}", arg):  # кнопка «Собрать сервер» из рассылки о новой локации
        try:
            return await tariffs_screen(m, arg[4:], m.from_user.id)
        except Exception:
            log.exception("start loc")
    await show(m, await home_text(m.from_user.id), home_kb(m.from_user.id in env().admins))


@router.callback_query(F.data == "m:home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, await home_text(c.from_user.id), home_kb(c.from_user.id in env().admins))


@router.message(Command("admin"))
async def admin_cmd(m: Message):
    if m.from_user.id not in env().admins:
        return
    url = env().public_url.rstrip("/") + "/admin"
    if not url.startswith("https://"):
        await m.answer("⚠️ Для Mini App нужен HTTPS в PUBLIC_URL.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Открыть панель", web_app=WebAppInfo(url=url))]])
    await m.answer("⚙️ <b>Панель администратора</b>\n\nЦены, клиенты, платежи и обращения.", reply_markup=kb, parse_mode="HTML")


# ───────────────────────── покупка ─────────────────────────


@router.callback_query(F.data == "m:buy")
async def buy_locations(c: CallbackQuery):
    try:
        locs = await svc.locations(ctx())
    except Exception:
        log.exception("catalog")
        await show(c, "⚠️ <b>Каталог временно недоступен</b>\n\nПопробуйте через пару минут.", kb_rows([]))
        return
    rows = [[(f"{l['flag']} {l['name']}" + (" · IP-локация" if l.get("virtual") else ""), f"b:l:{l['code']}")] for l in locs]
    virt = [f"{l['flag']} {e(l['name'])} — {e(l['description'])}" for l in locs if l.get("virtual") and l.get("description")]
    note = ("\n\n<i>IP-локации: адрес выбранной страны, сам сервер — в другом дата-центре.</i>\n" + "\n".join(virt)) if virt else ""
    await show(c, f"🌍 <b>Выберите расположение сервера</b>\n\nЦена зависит от локации. Выдача — за 1–3 минуты.{note}", kb_rows(rows))


@router.callback_query(F.data.startswith("b:l:"))
async def buy_tariffs(c: CallbackQuery):
    await tariffs_screen(c, c.data.split(":")[2], c.from_user.id)


async def tariffs_screen(c: Message | CallbackQuery, loc: str, uid: int):
    """Тарифы локации — из меню покупки или по ссылке из рассылки (start=loc_cz)."""
    offs = await svc.offers(ctx(), loc, uid)
    name = loc_name(await ctx().catalog(), loc)
    if not offs:
        await show(c, f"⚠️ <b>{svc.flag(loc)} {e(name)}</b>\n\nВ этой локации сейчас нет доступных тарифов.", kb_rows([], back="m:buy"))
        return
    rows = [[(f"{o['cpu']} vCPU · {o['ram']} ГБ · {o['disk']} ГБ — {rub(o['price_kop'])}", f"b:p:{loc}:{o['idx']}")] for o in offs]
    cat_ = await ctx().catalog()
    desc = next((l.get("description") for l in cat_.get("locations", []) if l.get("code") == loc and l.get("virtual")), None)
    limited = [f"{o['cpu']} vCPU · {o['ram']} ГБ · {o['disk']} ГБ — до {e(o['speed'])}" for o in offs if o.get("speed_limited")]
    base = svc.channel_speed(svc.loc_channel(await ctx().catalog(), loc))
    if limited:
        speed_note = "\n\n🌐 <b>Скорость канала</b>\n" + "\n".join(limited) + (f"\nОстальные тарифы — до {e(base)}" if base and len(limited) < len(offs) else "")
    else:
        speed_note = f"\n\n🌐 Канал: до {e(base)} на всех тарифах" if base else ""
    kb = kb_rows(rows, back="m:buy")
    app_url = env().public_url.rstrip("/") + "/app"
    async with session() as s_:
        custom_on = (await load_settings(s_)).get("custom_enabled", True)
    custom = custom_on and svc.configurator(cat_) is not None and app_url.startswith("https://")
    if custom:  # свою конфигурацию удобнее собрать ползунками в кабинете
        rows_ = [list(r) for r in kb.inline_keyboard]
        rows_.insert(len(rows_) - 1, [InlineKeyboardButton(text="🎛 Собрать свою конфигурацию", web_app=WebAppInfo(url=app_url + "?tab=new"))])
        kb = InlineKeyboardMarkup(inline_keyboard=rows_)
    head = f"🖥 <b>Конфигурация · {svc.flag(loc)} {e(name)}</b>\n" + (f"<i>{e(desc)}</i>\n" if desc else "")
    tail = "\n\nНужно другое сочетание процессора, памяти и диска — соберите его в кабинете." if custom else ""
    await show(c, f"{head}\nПроцессор · память · диск NVMe.\nЦены — за 30 дней.{speed_note}{tail}", kb)


@router.callback_query(F.data.startswith("b:p:"))
async def buy_os(c: CallbackQuery):
    _, _, loc, idx = c.data.split(":")
    cat = await ctx().catalog()
    oses = cat.get("os", [])
    b = InlineKeyboardBuilder()
    for o in oses:
        b.button(text=svc.os_label(o), callback_data=f"b:o:{loc}:{idx}:{o}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="← Назад", callback_data=f"b:l:{loc}"))
    await show(c, "⚙️ <b>Операционная система</b>\n\nВыберите образ — сменить его можно позже переустановкой.", b.as_markup())


PERIOD_LABEL = {"1m": "1 мес", "3m": "3 мес", "6m": "6 мес", "1y": "1 год"}


@router.callback_query(F.data.startswith("b:o:"))
async def buy_confirm(c: CallbackQuery):
    parts = c.data.split(":")  # b:o:loc:idx:os[:period]
    loc, idx, os_ = parts[2], parts[3], parts[4]
    period = parts[5] if len(parts) > 5 else "1m"
    offs = {o["idx"]: o for o in await svc.offers(ctx(), loc, c.from_user.id)}
    o = offs.get(int(idx))
    if not o:
        await show(c, "⚠️ <b>Тариф недоступен</b>\n\nВыберите другую конфигурацию.", kb_rows([], back="m:buy"))
        return
    async with session() as s:
        user = await s.get(User, c.from_user.id)
        cfg = await load_settings(s)
    cat = await ctx().catalog()
    opts = svc.period_options(cfg, cat)
    opt = next((x for x in opts if x["code"] == period), opts[0])
    period = opt["code"]
    month = o["price_kop"]
    full = svc.period_price(month, opt["months"], opt["pct"])
    promo_off = month - svc.apply_discount(month, user.promo_discount_pct or 0)
    pay_kop = max(0, full - promo_off)
    enough = user.balance_kop >= pay_kop
    name = loc_name(cat, loc)
    price = f"<b>{rub(pay_kop)}</b>"
    notes = []
    if opt["months"] > 1 and opt["pct"]:
        notes.append(f"−{opt['pct']}% за срок")
    if promo_off:
        notes.append(f"−{user.promo_discount_pct}% на первый месяц по промокоду")
    if pay_kop != month * opt["months"]:
        price += f" <s>{rub(month * opt['months'])}</s>"
    term = "30 дней" if opt["months"] == 1 else f"{PERIOD_LABEL[period]}, дальше продление помесячно — {rub(month)}"
    text = (
        "🛒 <b>Подтверждение заказа</b>\n\n"
        f"🌍 {svc.flag(loc)} {e(name)}\n"
        f"🖥 {o['cpu']} vCPU · {o['ram']} ГБ RAM · {o['disk']} ГБ NVMe\n"
        + (f"🌐 Канал: до {e(o['speed'])}\n" if o.get("speed") else "") +
        f"⚙️ {e(svc.os_label(os_))}\n"
        f"🔄 {term}\n\n"
        f"💳 К оплате: {price}" + (f"\n<i>{' · '.join(notes)}</i>" if notes else "") + "\n"
        f"💰 Баланс: {rub(user.balance_kop)}"
    )
    rows = []
    if len(opts) > 1:  # выбор срока: текущий отмечен галочкой
        btns = [(("✅ " if x["code"] == period else "") + PERIOD_LABEL[x["code"]] + (f" −{x['pct']}%" if x["pct"] else ""),
                 f"b:o:{loc}:{idx}:{os_}:{x['code']}") for x in opts]
        rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows += [[(f"✅ Оплатить {rub(pay_kop)}", f"b:go:{loc}:{idx}:{os_}:{period}")]] if enough else [[("💳 Пополнить баланс", "m:topup")]]
    if not enough:
        text += f"\n\n⚠️ Не хватает <b>{rub(pay_kop - user.balance_kop)}</b> — пополните баланс."
    await show(c, text, kb_rows(rows, back=f"b:p:{loc}:{idx}"))


@router.callback_query(F.data.startswith("b:go:"))
async def buy_go(c: CallbackQuery):
    parts = c.data.split(":")  # b:go:loc:idx:os[:period]
    loc, idx, os_ = parts[2], parts[3], parts[4]
    period = parts[5] if len(parts) > 5 else "1m"
    await c.answer("⚡ Создаём сервер…")
    try:
        srv = await svc.buy_server(ctx(), c.from_user.id, int(idx), loc, os_, period=period)
    except UserError as ex:
        await show(c, f"❌ {e(ex)}", kb_rows([], back="m:home"))
        return
    await show(
        c,
        f"✅ <b>Заказ оплачен</b>\n\n🖥 {e(srv.name)}\n⚡ Создаётся — обычно 1–3 минуты.\n\nПришлём IP и пароль в этот чат, как только сервер будет готов.",
        kb_rows([[("🖥 Мои серверы", "s:list")]], back="m:home", back_text="← Меню"),
    )


# ───────────────────────── пополнение ─────────────────────────


@router.callback_query(F.data == "m:topup")
async def topup_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[("100 ₽", "t:100"), ("300 ₽", "t:300"), ("500 ₽", "t:500")], [("1 000 ₽", "t:1000"), ("3 000 ₽", "t:3000")], [("Другая сумма", "t:custom")]]
    async with session() as s:
        user = await s.get(User, c.from_user.id)
    await show(c, f"💳 <b>Пополнение баланса</b>\n\n💰 Сейчас: <b>{rub(user.balance_kop)}</b>\n\nВыберите сумму. Оплата картой или через СБП.", kb_rows(rows))


@router.callback_query(F.data == "t:custom")
async def topup_custom(c: CallbackQuery, state: FSMContext):
    await state.set_state(TopUp.amount)
    await show(c, "💳 <b>Своя сумма</b>\n\nОтправьте сумму в рублях целым числом, например <b>750</b>.", kb_rows([], back="m:topup"))


@router.message(TopUp.amount)
async def topup_amount(m: Message, state: FSMContext):
    txt = (m.text or "").strip().replace(" ", "")
    if not txt.isdigit():
        await m.answer("⚠️ Нужна сумма целым числом, например <b>500</b>.", parse_mode="HTML")
        return
    await state.clear()
    await _choose_provider(m, int(txt))


@router.callback_query(F.data.regexp(r"^t:\d+$"))
async def topup_preset(c: CallbackQuery):
    await _choose_provider(c, int(c.data.split(":")[1]))


async def _choose_provider(target: Message | CallbackQuery, amount: int) -> None:
    """Если включено несколько способов оплаты — спрашиваем, каким платить."""
    async with session() as s:
        cfg = await load_settings(s)
    provs = svc.payment_providers(ctx(), cfg)
    if len(provs) <= 1:
        if isinstance(target, CallbackQuery):
            await target.answer()
        if provs and provs[0]["autopay"] and cfg["autopay_available"]:
            await _ask_save_card(target, amount, provs[0]["code"])
            return
        await _start_payment(target, amount, provs[0]["code"] if provs else None)
        return
    rows = [[(f"💳 {p['label']}", f"t:pv:{amount}:{p['code']}")] for p in provs]
    await show(target, f"💳 <b>Пополнение на {rub(amount * 100)}</b>\n\nВыберите способ оплаты.", kb_rows(rows, back="m:topup"))


@router.callback_query(F.data.startswith("t:pv:"))
async def topup_provider(c: CallbackQuery):
    _, _, amount, code = c.data.split(":")
    await c.answer()
    async with session() as s:
        cfg = await load_settings(s)
    prov = next((p for p in svc.payment_providers(ctx(), cfg) if p["code"] == code), None)
    if prov and prov["autopay"] and cfg["autopay_available"]:
        await _ask_save_card(c, int(amount), code)
        return
    await _start_payment(c, int(amount), code)


async def _ask_save_card(target: Message | CallbackQuery, amount: int, code: str) -> None:
    base = env().public_url.rstrip("/")
    rows = [[("💳 Оплатить без сохранения карты", f"t:go:{amount}:{code}:0")],
            [("💾 Оплатить и сохранить карту", f"t:go:{amount}:{code}:1")]]
    text = (f"💳 <b>Пополнение на {rub(amount * 100)}</b>\n\n"
            "Можно сохранить карту для автоплатежей: если на продление сервера не хватит баланса, "
            "спишем только недостающую сумму и сразу пришлём уведомление. Удалить карту можно в любой момент в «👤 Профиль → Способы оплаты».\n\n"
            + (f'Сохраняя карту, вы соглашаетесь с <a href="{base}/legal/autopay">Правилами автоплатежей</a>.' if base.startswith("https://") else "Сохраняя карту, вы соглашаетесь с Правилами автоплатежей."))
    await show(target, text, kb_rows(rows, back="m:topup"))


@router.callback_query(F.data.startswith("t:go:"))
async def topup_go(c: CallbackQuery):
    _, _, amount, code, save = c.data.split(":")
    await c.answer()
    await _start_payment(c, int(amount), code, save == "1")


async def _start_payment(target: Message | CallbackQuery, amount: int, provider: str | None = None, save: bool = False) -> None:
    tg_id = target.from_user.id
    try:
        pay, url = await svc.create_topup(ctx(), tg_id, amount, save_method=save, provider=provider, source="bot")
    except UserError as ex:
        await show(target, f"❌ {e(ex)}", kb_rows([], back="m:topup"))
        return
    except Exception:
        log.exception("create_topup")
        await show(target, "❌ <b>Не удалось создать платёж</b>\n\nПопробуйте через пару минут.", kb_rows([], back="m:topup"))
        return
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"💳 Оплатить {rub(amount * 100)}", url=url))
    b.row(InlineKeyboardButton(text="🔄 Я оплатил(а)", callback_data=f"t:chk:{pay.id}"))
    b.row(InlineKeyboardButton(text="← Назад", callback_data="m:home"))
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer(
        f"💳 <b>Счёт на {rub(amount * 100)}</b>\n\nНажмите «Оплатить» — откроется страница {'Platega' if provider == 'platega' else 'ЮKassa'}.\nБаланс пополнится автоматически сразу после оплаты.",
        reply_markup=b.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("t:chk:"))
async def topup_check(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    async with session() as s:
        pay = await s.get(Payment, pid)
    if not pay or pay.tg_id != c.from_user.id:
        await c.answer("❌ Платёж не найден", show_alert=True)
        return
    if pay.status == "succeeded":
        await c.answer("✅ Платёж уже зачислен", show_alert=True)
        return
    try:
        done = await svc.credit_payment(ctx(), pay.yk_id)
    except Exception:
        log.exception("manual check")
        await c.answer("⚠️ Не удалось проверить. Попробуйте через минуту.", show_alert=True)
        return
    await c.answer("✅ Зачислено на баланс" if done else "🔄 Оплата пока не поступила. Если вы уже оплатили — проверьте через минуту.", show_alert=not done)


# ───────────────────────── серверы ─────────────────────────


def _left(srv: Server) -> str:
    if not srv.expires_at:
        return "—"
    d = (srv.expires_at - svc.utcnow()).total_seconds() / 86400
    if d < 0:
        return "<b>срок истёк</b>"
    rest = f"осталось {int(d)} дн." if d >= 1 else (f"осталось {int(d * 24)} ч." if d * 24 >= 1 else "меньше часа")
    return f"<b>{srv.expires_at.astimezone(timezone.utc):%d.%m.%Y}</b> · {rest}"


@router.callback_query(F.data == "s:list")
async def servers_list(c: CallbackQuery):
    from sqlalchemy import select

    async with session() as s:
        rows = (await s.execute(select(Server).where(Server.tg_id == c.from_user.id, Server.status != "deleted").order_by(Server.id))).scalars().all()
    shared = [srv for srv, _ in await sh.shared_servers(c.from_user.id)]
    if not rows and not shared:
        await show(c, "🖥 <b>Мои серверы</b>\n\nСерверов пока нет. Соберите первый — он будет готов через 1–3 минуты.", kb_rows([[("🛒 Купить сервер", "m:buy")]]))
        return
    kb = [[(f"{STATUS_ICON.get(r.status, '•')} {svc.flag(r.location)} {r.name} · {r.ip or 'создаётся'}", f"s:v:{r.id}")] for r in rows]
    kb += [[(f"👥 {svc.flag(r.location)} {r.name} · {r.ip or 'создаётся'}", f"s:v:{r.id}")] for r in shared]
    active = sum(1 for r in rows + shared if r.status == "active")
    note = "\n👥 — общий доступ: сервер другого владельца." if shared else ""
    await show(c, f"🖥 <b>Мои серверы</b>\n\nВсего: <b>{len(rows) + len(shared)}</b> · активных: <b>{active}</b>{note}\nВыберите сервер, чтобы открыть управление.", kb_rows(kb))


def card_text(srv: Server, speed: str = "") -> str:
    return (
        f"🖥 <b>{e(srv.name)}</b>\n"
        f"{STATUS.get(srv.status, e(srv.status))} · {svc.flag(srv.location)} {e(srv.location.upper())}\n"
        + (f"🔴 <b>Не отвечает</b> с {srv.mon_down_at.astimezone(timezone.utc):%d.%m %H:%M} UTC\n" if srv.mon_down_at else "") +
        f"{srv.cpu} vCPU · {srv.ram} ГБ RAM · {srv.disk} ГБ · {e(svc.os_label(srv.os))}\n"
        + (f"🌐 Канал: до {e(speed)}\n" if speed else "") + "\n"
        f"🛡 <b>Доступ</b>\n"
        f"IP: <code>{e(srv.ip or '—')}</code>\nЛогин: <code>root</code>\nПароль: <code>{e(srv.root_password or '—')}</code>\n\n"
        f"💳 <b>Оплата</b>\n"
        f"Оплачен до: {_left(srv)}\n"
        f"Продление: {rub(srv.price_kop)} за 30 дней\n"
        f"Автопродление: {'✅ включено' if srv.auto_renew else '⚠️ выключено'}"
    )


def member_kb(srv: Server) -> InlineKeyboardMarkup:
    i = srv.id
    rows = [
        [("🔄 Продлить со своего баланса", f"s:rn:{i}")],
        [("Включить", f"s:a:{i}:start"), ("Выключить", f"s:a:{i}:stop"), ("Перезагрузить", f"s:a:{i}:reboot")],
        [("🖥 Консоль VNC", f"s:a:{i}:vnc")],
        [("📖 Как подключиться", f"s:hw:{i}"), ("📜 Журнал", f"s:lg:{i}")],
        [("🚪 Выйти из доступа", f"sh:l:{i}")],
    ]
    return kb_rows(rows, back="s:list", back_text="← Мои серверы")


def card_kb(srv: Server, transfers: bool = True, shares: bool = True) -> InlineKeyboardMarkup:
    i = srv.id
    rows = [
        [("🔄 Продлить на 30 дней", f"s:rn:{i}"), (("Автопродление: вкл" if srv.auto_renew else "Автопродление: выкл"), f"s:ar:{i}")],
        [("Включить", f"s:a:{i}:start"), ("Выключить", f"s:a:{i}:stop"), ("Перезагрузить", f"s:a:{i}:reboot")],
        [("🖥 Консоль VNC", f"s:a:{i}:vnc"), ("🌍 Сменить IP", f"s:c:{i}:change-ip")],
        [("⚙️ Переустановить ОС", f"s:ri:{i}"), ("🛡 Сбросить пароль", f"s:c:{i}:password")],
        [("📖 Как подключиться", f"s:hw:{i}"), ("📜 Журнал", f"s:lg:{i}")],
        ([("👥 Общий доступ", f"s:sh:{i}")] if shares else []) + ([("🔁 Передать другу", f"s:tr:{i}")] if transfers else []),
        [("❌ Удалить сервер", f"s:d:{i}")],
    ]
    rows = [r for r in rows if r]
    return kb_rows(rows, back="s:list", back_text="← Мои серверы")


def _uname(u: dict | User | None) -> str:
    if u is None:
        return "клиент"
    g = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k))
    return e(g("first_name") or "") + (f" @{e(g('username'))}" if g("username") else f" (ID {g('tg_id')})")


async def _card(srv: Server, role: str = "owner", viewer: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка сервера. Админу дополнительно показываем ID сервера у Astra."""
    async with session() as s:
        cfg = await load_settings(s)
        owner = await s.get(User, srv.tg_id) if role == "member" else None
    try:
        channel = svc.loc_channel(await ctx().catalog(), srv.location)
    except Exception:  # Astra недоступна — карточку всё равно показываем
        channel = ""
    text = card_text(srv, svc.speed_of(cfg, srv.cpu, srv.ram, srv.disk, channel))
    if viewer is not None and viewer in env().admins:
        text = text.replace("\n\n🛡 <b>Доступ</b>", f"\n🆔 У Astra: <code>{srv.astra_id}</code> <i>(видите только вы)</i>\n\n🛡 <b>Доступ</b>", 1)
    if role == "member":
        text = text.rsplit("\nАвтопродление:", 1)[0] + f"\n\n👥 <b>Общий доступ</b> · владелец {_uname(owner)}\n<i>Можно включать, выключать, перезагружать сервер и продлевать его со своего баланса.</i>"
        return text, member_kb(srv)
    mem = [m for m in await sh.members(srv.id, srv.tg_id) if m["status"] == "active"]
    if mem:
        text += "\n\n👥 Доступ: " + ", ".join(_uname(m["user"]) for m in mem)
    return text, card_kb(srv, bool(cfg.get("transfers_enabled", True)), bool(cfg.get("shares_enabled", True)))


async def _own(c: CallbackQuery, sid: int) -> Server | None:
    async with session() as s:
        srv = await s.get(Server, sid)
    if not srv or srv.tg_id != c.from_user.id or srv.status == "deleted":
        await c.answer("❌ Сервер не найден", show_alert=True)
        return None
    return srv


async def _access(c: CallbackQuery, sid: int) -> tuple[Server, str] | None:
    """Свой сервер или общий доступ к чужому."""
    async with session() as s:
        srv = await s.get(Server, sid)
        role = await sh.role(s, c.from_user.id, srv)
    if role is None:
        await c.answer("❌ Сервер не найден", show_alert=True)
        return None
    return srv, role


async def _outgoing(tg_id: int, server_id: int) -> dict | None:
    p = await tr.pending_for(tg_id)
    return next((x for x in p["outgoing"] if x["server_id"] == server_id), None)


@router.callback_query(F.data.startswith("s:v:"))
async def server_card(c: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    acc_ = await _access(c, int(c.data.split(":")[2]))
    if acc_ and acc_[1] == "member":
        await show(c, *await _card(acc_[0], "member", c.from_user.id))
        return
    srv = acc_[0] if acc_ else None
    if srv:
        out = await _outgoing(c.from_user.id, srv.id)
        text, kb = await _card(srv, viewer=c.from_user.id)
        if out:
            u = out["user"] or {}
            text += f"\n\n🔁 <b>Ожидает подтверждения</b> от {e(u.get('first_name') or '')}" + (f" @{e(u['username'])}" if u.get("username") else f" (ID {u.get('tg_id')})")
            rows = [list(r) for r in kb.inline_keyboard]
            rows.insert(len(rows) - 1, [InlineKeyboardButton(text="✖️ Отменить передачу", callback_data=f"tr:c:{out['id']}")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await show(c, text, kb)


# ───────────────────────── передача сервера ─────────────────────────


@router.callback_query(F.data.startswith("s:tr:"))
async def transfer_start(c: CallbackQuery, state: FSMContext):
    srv = await _own(c, int(c.data.split(":")[2]))
    if not srv:
        return
    if srv.status not in ("active", "suspended"):
        return await c.answer("Передать можно только созданный сервер.", show_alert=True)
    async with session() as s:
        if not (await load_settings(s)).get("transfers_enabled", True):
            return await c.answer("Передача серверов сейчас отключена.", show_alert=True)
    await state.set_state(TransferInput.to)
    await state.update_data(server_id=srv.id)
    await show(c, f"🔁 <b>Передать {e(srv.name)}</b>\n\nОтправьте @username или Telegram ID друга. Он должен быть клиентом бота.\n\n"
                  "<i>Друг получит запрос и примет его. После этого сервер со всем оплаченным сроком перейдёт к нему, "
                  "а продление — с его баланса. Заранее удалите с сервера свои данные.</i>",
               kb_rows([], back=f"s:v:{srv.id}", back_text="← Отмена"))


@router.message(TransferInput.to)
async def transfer_to(m: Message, state: FSMContext):
    data = await state.get_data()
    sid = data.get("server_id")
    try:
        u = await tr.find_recipient(m.text or "")
        if u.tg_id == m.from_user.id:
            raise UserError("Нельзя передать сервер самому себе.")
    except UserError as ex:
        await m.answer(f"⚠️ {e(ex)}\n\nОтправьте @username или ID ещё раз.", parse_mode="HTML", reply_markup=kb_rows([], back=f"s:v:{sid}", back_text="← Отмена"))
        return
    await state.clear()
    who = e(u.first_name or "") + (f" @{e(u.username)}" if u.username else f" (ID {u.tg_id})")
    await m.answer(f"🔁 <b>Передать сервер {who}?</b>\n\nПосле его согласия сервер исчезнет из вашего списка. Отменить можно, пока запрос не принят.",
                   parse_mode="HTML", reply_markup=kb_rows([[("✅ Отправить запрос", f"tr:y:{sid}:{u.tg_id}")]], back=f"s:v:{sid}", back_text="← Отмена"))


@router.callback_query(F.data.startswith("tr:y:"))
async def transfer_send(c: CallbackQuery):
    _, _, sid, to = c.data.split(":")
    try:
        r = await tr.create(ctx(), c.from_user.id, int(sid), to)
    except UserError as ex:
        return await c.answer(str(ex)[:190], show_alert=True)
    await c.answer("✅ Запрос отправлен")
    await show(c, "✅ <b>Запрос отправлен</b>\n\nКак только друг примет его, сервер перейдёт к нему, а вам придёт уведомление. Запрос действует 24 часа.",
               kb_rows([[("🖥 К серверу", f"s:v:{sid}")]], back="s:list", back_text="← Мои серверы"))


@router.callback_query(F.data.startswith("tr:a:") | F.data.startswith("tr:d:") | F.data.startswith("tr:c:"))
async def transfer_answer(c: CallbackQuery):
    _, act, tid = c.data.split(":")
    try:
        if act == "a":
            r = await tr.accept(ctx(), int(tid), c.from_user.id)
        elif act == "d":
            await tr.decline(ctx(), int(tid), c.from_user.id)
        else:
            await tr.cancel(ctx(), int(tid), c.from_user.id)
    except UserError as ex:
        await c.answer(str(ex)[:190], show_alert=True)
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        return
    if act == "a":
        await c.answer("✅ Сервер ваш")
        await show(c, f"✅ <b>Сервер {e(r['name'])} теперь ваш</b>\n\nРекомендуем сразу сменить пароль root. Продление — с вашего баланса.",
                   kb_rows([[("🖥 Открыть сервер", f"s:v:{r['server_id']}")]], back="s:list", back_text="← Мои серверы"))
    elif act == "d":
        await c.answer("Запрос отклонён")
        await show(c, "❌ <b>Передача отклонена</b>\n\nСервер остаётся у владельца.", None)
    else:
        await c.answer("Передача отменена")
        await show(c, "✖️ <b>Передача отменена</b>\n\nСервер остаётся у вас.", kb_rows([], back="s:list", back_text="← Мои серверы"))


# ───────────────────────── журнал, инструкция, мониторинг ─────────────────────────


def howto_text(srv: Server) -> str:
    ip = e(srv.ip or "IP-адрес")
    return (f"📖 <b>Как подключиться к {e(srv.name)}</b>\n\n"
            f"<b>Windows 10/11</b> — откройте «Терминал» (или PowerShell) и введите:\n<code>ssh root@{ip}</code>\n"
            "На вопрос о ключе ответьте <code>yes</code>, затем вставьте пароль — при вводе он не отображается, это нормально.\n\n"
            f"<b>macOS и Linux</b> — откройте «Терминал», команда та же:\n<code>ssh root@{ip}</code>\n\n"
            "<b>Телефон</b> — приложение Termius (iOS, Android): New Host → адрес "
            f"<code>{ip}</code>, пользователь <code>root</code>, пароль из карточки сервера.\n\n"
            "🆘 <b>Сервер не отвечает?</b>\n"
            "1. Проверьте, что он оплачен и включён — статус в карточке.\n"
            "2. Перезагрузите его кнопкой «Перезагрузить».\n"
            "3. Откройте «Консоль VNC» — там видно, что происходит на экране.\n"
            "4. Не помогло — напишите в поддержку, укажите IP.")


@router.callback_query(F.data.startswith("s:hw:"))
async def server_howto(c: CallbackQuery):
    a = await _access(c, int(c.data.split(":")[2]))
    if a:
        await show(c, howto_text(a[0]), kb_rows([[("💬 Поддержка", "m:support")]], back=f"s:v:{a[0].id}", back_text="← К серверу"))


@router.callback_query(F.data.startswith("s:lg:"))
async def server_log_view(c: CallbackQuery, sid: int | None = None):
    a = await _access(c, sid or int(c.data.split(":")[2]))
    if not a:
        return
    srv, role = a
    from . import serverlog
    items = await serverlog.recent(srv.id, srv.tg_id, 15)
    lines = [f"{it['icon']} {datetime.fromisoformat(it['at']).astimezone(timezone.utc):%d.%m %H:%M} · {e(it['title'])}"
             + (f" — {e(it['detail'])}" if it["detail"] else "") + ("" if it["role"] == "system" else f" · <i>{e(it['who'])}</i>") for it in items]
    body = "\n".join(lines) if lines else "Пока пусто — здесь появятся включения, перезагрузки, продления и другие действия."
    rows = []
    if role == "owner":
        async with session() as s:
            mon_on = (await load_settings(s)).get("monitor_enabled", True)
        if mon_on:
            rows.append([(("🔔 Следим за доступностью: вкл" if srv.monitor else "🔕 Следим за доступностью: выкл"), f"mon:{'off' if srv.monitor else 'on'}:{srv.id}")])
    await show(c, f"📜 <b>Журнал · {e(srv.name)}</b>\n<i>Время — UTC, последние 15 событий</i>\n\n{body}", kb_rows(rows, back=f"s:v:{srv.id}", back_text="← К серверу"))


@router.callback_query(F.data.startswith("mon:"))
async def monitor_toggle(c: CallbackQuery):
    _, act, sid = c.data.split(":")
    from . import monitor
    try:
        on = await monitor.set_monitor(c.from_user.id, int(sid), act == "on")
    except UserError as ex:
        return await c.answer(str(ex)[:190], show_alert=True)
    await c.answer("Следим за доступностью" if on else "Больше не следим за этим сервером")
    if c.message and c.message.text and c.message.text.startswith("📜"):
        await server_log_view(c, int(sid))
    else:
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass


# ───────────────────────── общий доступ ─────────────────────────

SHARE_RIGHTS = ("Участник видит IP и пароль, может включать, выключать и перезагружать сервер, открывать VNC и продлевать его со своего баланса. "
                "Переустановка, сброс пароля, смена IP, удаление и передача — только у вас.")


async def _share_screen(c: CallbackQuery, srv: Server) -> None:
    mem = await sh.members(srv.id, c.from_user.id)
    lines, rows = [], []
    for m in mem:
        if m["status"] == "active":
            lines.append(f"✅ {_uname(m['user'])}")
            rows.append([(f"🔒 Закрыть доступ: {(m['user'] or {}).get('first_name') or 'клиент'}", f"sh:r:{m['id']}")])
        else:
            lines.append(f"⏳ {_uname(m['user'])} — ждём ответа")
            rows.append([(f"✖️ Отменить приглашение: {(m['user'] or {}).get('first_name') or 'клиент'}", f"sh:r:{m['id']}")])
    if len(mem) < sh.MAX_MEMBERS:
        rows.insert(0, [("➕ Пригласить", f"sh:i:{srv.id}")])
    body = "\n".join(lines) if lines else "Пока доступ есть только у вас."
    await show(c, f"👥 <b>Общий доступ · {e(srv.name)}</b>\n\n{body}\n\n<i>{SHARE_RIGHTS}</i>", kb_rows(rows, back=f"s:v:{srv.id}", back_text="← К серверу"))


@router.callback_query(F.data.startswith("s:sh:"))
async def share_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    srv = await _own(c, int(c.data.split(":")[2]))
    if not srv:
        return
    async with session() as s:
        if not (await load_settings(s)).get("shares_enabled", True):
            return await c.answer("Общий доступ к серверам сейчас отключён.", show_alert=True)
    await _share_screen(c, srv)


@router.callback_query(F.data.startswith("sh:i:"))
async def share_start(c: CallbackQuery, state: FSMContext):
    srv = await _own(c, int(c.data.split(":")[2]))
    if not srv:
        return
    if srv.status not in ("active", "suspended"):
        return await c.answer("Поделиться можно только созданным сервером.", show_alert=True)
    await state.set_state(ShareInput.to)
    await state.update_data(server_id=srv.id)
    await show(c, f"👥 <b>Пригласить к {e(srv.name)}</b>\n\nОтправьте @username или Telegram ID. Это должен быть клиент бота.\n\n<i>{SHARE_RIGHTS}</i>",
               kb_rows([], back=f"s:sh:{srv.id}", back_text="← Отмена"))


@router.message(ShareInput.to)
async def share_to(m: Message, state: FSMContext):
    sid = (await state.get_data()).get("server_id")
    try:
        u = await tr.find_recipient(m.text or "")
        if u.tg_id == m.from_user.id:
            raise UserError("Это вы — сервер и так ваш.")
    except UserError as ex:
        await m.answer(f"⚠️ {e(ex)}\n\nОтправьте @username или ID ещё раз.", parse_mode="HTML", reply_markup=kb_rows([], back=f"s:sh:{sid}", back_text="← Отмена"))
        return
    await state.clear()
    await m.answer(f"👥 <b>Открыть доступ {_uname(u)}?</b>\n\nСервер появится у него в списке после того, как он примет приглашение. Закрыть доступ можно в любой момент.",
                   parse_mode="HTML", reply_markup=kb_rows([[("✅ Отправить приглашение", f"sh:y:{sid}:{u.tg_id}")]], back=f"s:sh:{sid}", back_text="← Отмена"))


@router.callback_query(F.data.startswith("sh:y:"))
async def share_send(c: CallbackQuery):
    _, _, sid, to = c.data.split(":")
    try:
        await sh.invite(ctx(), c.from_user.id, int(sid), to)
    except UserError as ex:
        return await c.answer(str(ex)[:190], show_alert=True)
    await c.answer("✅ Приглашение отправлено")
    srv = await _own(c, int(sid))
    if srv:
        await _share_screen(c, srv)


@router.callback_query(F.data.startswith("sh:r:"))
async def share_remove(c: CallbackQuery):
    shid = int(c.data.split(":")[2])
    try:
        await sh.remove(ctx(), shid, c.from_user.id)
    except UserError as ex:
        await c.answer(str(ex)[:190], show_alert=True)
    else:
        await c.answer("Готово")
    async with session() as s:
        row = await s.get(ServerShare, shid)
    srv = await _own(c, row.server_id) if row else None
    if srv:
        await _share_screen(c, srv)


@router.callback_query(F.data.startswith("sh:a:") | F.data.startswith("sh:d:"))
async def share_answer(c: CallbackQuery):
    _, act, shid = c.data.split(":")
    try:
        if act == "a":
            r = await sh.accept(ctx(), int(shid), c.from_user.id)
        else:
            await sh.decline(ctx(), int(shid), c.from_user.id)
    except UserError as ex:
        await c.answer(str(ex)[:190], show_alert=True)
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        return
    if act == "a":
        await c.answer("✅ Доступ открыт")
        await show(c, f"✅ <b>Сервер {e(r['name'])} теперь в вашем списке</b>\n\nОн отмечен 👥 в «Мои серверы» и в личном кабинете.",
                   kb_rows([[("🖥 Открыть сервер", f"s:v:{r['server_id']}")]], back="s:list", back_text="← Мои серверы"))
    else:
        await c.answer("Приглашение отклонено")
        await show(c, "❌ <b>Приглашение отклонено</b>", None)


@router.callback_query(F.data.startswith("sh:l:"))
async def share_leave_confirm(c: CallbackQuery):
    a = await _access(c, int(c.data.split(":")[2]))
    if not a:
        return
    srv, role = a
    if role != "member":
        return await c.answer("Это ваш сервер.", show_alert=True)
    await show(c, f"🚪 <b>Выйти из доступа к {e(srv.name)}?</b>\n\nСервер пропадёт из вашего списка. Вернуть доступ сможет только владелец.",
               kb_rows([[("🚪 Выйти", f"sh:ly:{srv.id}")]], back=f"s:v:{srv.id}", back_text="← Отмена"))


@router.callback_query(F.data.startswith("sh:ly:"))
async def share_leave(c: CallbackQuery):
    try:
        await sh.leave(ctx(), int(c.data.split(":")[2]), c.from_user.id)
    except UserError as ex:
        return await c.answer(str(ex)[:190], show_alert=True)
    await c.answer("Вы вышли из доступа")
    await servers_list(c)


@router.callback_query(F.data.startswith("s:ar:"))
async def toggle_autorenew(c: CallbackQuery):
    srv = await _own(c, int(c.data.split(":")[2]))
    if not srv:
        return
    async with session() as s:
        row = await s.get(Server, srv.id)
        row.auto_renew = not row.auto_renew
        await s.commit()
        srv = row
    from . import serverlog
    await serverlog.add(srv.id, c.from_user.id, "autorenew_on" if srv.auto_renew else "autorenew_off")
    await show(c, *await _card(srv, viewer=c.from_user.id))


@router.callback_query(F.data.startswith("s:rn:"))
async def renew_now(c: CallbackQuery):
    a = await _access(c, int(c.data.split(":")[2]))
    if not a:
        return
    srv, role = a
    await c.answer("🔄 Продлеваем…")
    try:
        srv = await svc.renew_server(ctx(), srv.id, payer=c.from_user.id)
    except UserError as ex:
        await c.message.answer(f"❌ {e(ex)}", parse_mode="HTML")
        return
    if role == "member":
        async with session() as s:
            me = await s.get(User, c.from_user.id)
        await ctx().notify(srv.tg_id, f"👥 {_uname(me)} продлил(а) сервер <b>{e(srv.name)}</b> на 30 дней со своего баланса.")
    await show(c, *await _card(srv, role, c.from_user.id))


@router.callback_query(F.data.startswith("s:a:"))
async def simple_action(c: CallbackQuery):
    _, _, sid, action = c.data.split(":")
    await c.answer("🔄 Отправляем команду…")
    try:
        resp = await svc.server_action(ctx(), c.from_user.id, int(sid), action)
    except UserError as ex:
        await c.message.answer(f"❌ {e(ex)}", parse_mode="HTML")
        return
    if action == "vnc":
        url = resp.get("link") if isinstance(resp, dict) else None
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🖥 Открыть консоль", url=url)]]) if url and url.startswith("https://") else None
        await c.message.answer(f"🖥 <b>Консоль VNC</b>\n\n{e(url)}\n\n<i>Ссылка действует ограниченное время.</i>", parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    else:
        done = {"start": "Сервер включается.", "stop": "Сервер выключается.", "reboot": "Сервер перезагружается.",
                "change-ip": "IP-адрес меняется — новый появится в карточке сервера через пару минут.",
                "password": "Пароль сбрасывается — новый появится в карточке сервера через пару минут."}
        await c.message.answer(f"✅ {done.get(action, 'Команда отправлена.')}")


@router.callback_query(F.data.startswith("s:c:"))
async def confirm_action(c: CallbackQuery):
    _, _, sid, action = c.data.split(":")
    srv = await _own(c, int(sid))
    if not srv:
        return
    async with session() as s:
        cfg = await load_settings(s)
    warn, btn = {
        "password": ("🛡 <b>Сброс пароля</b>\n\nПароль root будет сброшен, <b>данные на диске будут стёрты</b>. IP-адрес сохранится.", "✅ Сбросить и стереть"),
        "change-ip": (f"🌍 <b>Смена IP</b>\n\nСервер получит новый адрес, старый перестанет работать.\nСтоимость: <b>{rub(int(cfg['change_ip_price_rub']) * 100)}</b> — спишем с баланса.", "✅ Сменить IP"),
    }[action]
    await show(c, f"{warn}\n\nПродолжить?", kb_rows([[(btn, f"s:a:{sid}:{action}")]], back=f"s:v:{sid}"))


@router.callback_query(F.data.startswith("s:ri:"))
async def reinstall_choose(c: CallbackQuery):
    sid = c.data.split(":")[2]
    if not await _own(c, int(sid)):
        return
    cat = await ctx().catalog()
    b = InlineKeyboardBuilder()
    for o in cat.get("os", []):
        b.button(text=svc.os_label(o), callback_data=f"s:rc:{sid}:{o}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="← Назад", callback_data=f"s:v:{sid}"))
    await show(c, "⚙️ <b>Переустановка системы</b>\n\nВыберите образ. <b>Все данные на сервере будут стёрты.</b>", b.as_markup())


@router.callback_query(F.data.startswith("s:rc:"))
async def reinstall_confirm(c: CallbackQuery):
    _, _, sid, os_ = c.data.split(":")
    await show(c, f"⚠️ <b>Переустановить на {e(svc.os_label(os_))}?</b>\n\nВсе данные на сервере будут стёрты без возможности восстановления.", kb_rows([[("✅ Переустановить", f"s:ry:{sid}:{os_}")]], back=f"s:v:{sid}"))


@router.callback_query(F.data.startswith("s:ry:"))
async def reinstall_do(c: CallbackQuery):
    _, _, sid, os_ = c.data.split(":")
    await c.answer("🔄 Запускаем…")
    try:
        await svc.server_action(ctx(), c.from_user.id, int(sid), "reinstall", {"os": os_})
    except UserError as ex:
        await c.message.answer(f"❌ {e(ex)}", parse_mode="HTML")
        return
    await show(c, "✅ <b>Переустановка запущена</b>\n\nНовый пароль появится в карточке сервера через 1–3 минуты.", kb_rows([[("🖥 Мои серверы", "s:list")]], back="m:home", back_text="← Меню"))


@router.callback_query(F.data.startswith("s:d:"))
async def delete_confirm(c: CallbackQuery):
    srv = await _own(c, int(c.data.split(":")[2]))
    if not srv:
        return
    async with session() as s:
        cfg = await load_settings(s)
    refund = svc.refund_amount(svc.paid_base(srv), srv.expires_at, cfg["refund_pct"], svc.paid_months(srv))
    await show(
        c,
        f"⚠️ <b>Удалить сервер {e(srv.name)}?</b>\n\nСервер и все данные будут удалены безвозвратно.\n💰 Вернём на баланс: <b>{rub(refund)}</b> за неиспользованные дни.",
        kb_rows([[("❌ Удалить навсегда", f"s:dy:{srv.id}")]], back=f"s:v:{srv.id}", back_text="← Отмена"),
    )


@router.callback_query(F.data.startswith("s:dy:"))
async def delete_do(c: CallbackQuery):
    await c.answer("🔄 Удаляем…")
    try:
        refund = await svc.cancel_server(ctx(), c.from_user.id, int(c.data.split(":")[2]))
    except UserError as ex:
        await c.message.answer(f"❌ {e(ex)}", parse_mode="HTML")
        return
    await show(c, f"✅ <b>Сервер удалён</b>\n\n💰 Возвращено на баланс: <b>{rub(refund)}</b>.", kb_rows([[("🖥 Мои серверы", "s:list")]], back="m:home", back_text="← Меню"))


# ───────────────────────── уведомления ─────────────────────────


def make_notifier(bot: Bot):
    async def notify(tg_id: int, text: str) -> None:
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            log.warning("не удалось отправить сообщение %s", tg_id, exc_info=True)

    async def alert_admin(text: str) -> None:
        for aid in env().admins:
            await notify(aid, e(text))

    return notify, alert_admin



# ───────────────────────── профиль (аналог кабинета прямо в боте) ─────────────────────────


async def _user(tg_id: int):
    async with session() as s:
        return await s.get(User, tg_id), await load_settings(s)


@router.callback_query(F.data == "p:home")
async def profile_home(c: CallbackQuery, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    u, cfg = await _user(c.from_user.id)
    ms = await acc.methods(c.from_user.id)
    name = e(u.first_name or (("@" + u.username) if u.username else "Клиент"))
    lines = [f"👤 <b>Профиль · {e(cfg['service_name'])}</b>", "",
             f"{name}" + (f" · @{e(u.username)}" if u.username else "") + f"\nID <code>{u.tg_id}</code>", "",
             f"💰 Баланс: <b>{rub(u.balance_kop)}</b>",
             f"💳 Способы оплаты: {len(ms) if ms else 'нет сохранённых'}",
             f"🔄 Автоплатёж: {'включён' if u.autopay_enabled else 'выключен'}"]
    if _discount_line(u):
        lines.append(_discount_line(u, ""))
    rows = [[("💳 Способы оплаты и автоплатёж", "p:methods")],
            [("🧾 История операций", "p:hist"), ("🎁 Промокод", "p:promo")]]
    if cfg["ref_enabled"]:
        rows.append([("👥 Пригласить друга", "p:ref")])
    rows.append([(("🔔 Новости: вкл" if u.ads_consent else "🔕 Новости: выкл"), "p:news"), ("📄 Документы", "p:docs")])
    rows.append([("💳 Пополнить баланс", "m:topup")])
    await show(c, "\n".join(lines), kb_rows(rows))


@router.callback_query(F.data == "p:methods")
async def profile_methods(c: CallbackQuery):
    u, cfg = await _user(c.from_user.id)
    ms = await acc.methods(c.from_user.id)
    lines = [f"💳 <b>Способы оплаты · {e(cfg['service_name'])}</b>", ""]
    if ms:
        for m in ms:
            lines.append(f"• {e(m['title'] or 'Карта')}" + (f" · до {e(m['expiry'])}" if m.get("expiry") else ""))
    else:
        lines.append("Сохранённых карт нет." + (" Карту можно сохранить при пополнении баланса." if cfg["autopay_available"] else ""))
    lines += ["", f"🔄 Автоплатёж: <b>{'включён' if u.autopay_enabled else 'выключен'}</b>",
              "<i>Спишем только недостающую сумму для продления сервера с включённым автопродлением и сразу пришлём уведомление.</i>"]
    rows = [[(f"🗑 Удалить {m['title'] or 'карту'}", f"p:md:{m['id']}")] for m in ms]
    rows.append([("🗑 Удалить все способы оплаты", "p:mda")])
    rows.append([(("Выключить автоплатёж" if u.autopay_enabled else "Включить автоплатёж"), "p:ap")])
    base = env().public_url.rstrip("/")
    b = InlineKeyboardBuilder.from_markup(kb_rows(rows, back=None))
    if base.startswith("https://"):
        b.row(InlineKeyboardButton(text="📄 Правила автоплатежей", url=f"{base}/legal/autopay"))
    b.row(InlineKeyboardButton(text="← Профиль", callback_data="p:home"))
    await show(c, "\n".join(lines), b.as_markup())


@router.callback_query(F.data.startswith("p:md:"))
async def method_delete_ask(c: CallbackQuery):
    mid = int(c.data.split(":")[2])
    m = next((x for x in await acc.methods(c.from_user.id) if x["id"] == mid), None)
    if not m:
        return await c.answer("Карта уже удалена", show_alert=True)
    await show(c, f"🗑 <b>Удалить {e(m['title'] or 'карту')}?</b>\n\nКарта больше не будет использоваться для автоплатежей.",
               kb_rows([[("✅ Удалить", f"p:mdy:{mid}")]], back="p:methods", back_text="← Отмена"))


@router.callback_query(F.data.startswith("p:mdy:"))
async def method_delete(c: CallbackQuery):
    n = await svc.deactivate_method(c.from_user.id, int(c.data.split(":")[2]), "user")
    await c.answer("✅ Карта удалена" if n else "Карта уже удалена")
    await profile_methods(c)


@router.callback_query(F.data == "p:mda")
async def method_delete_all_ask(c: CallbackQuery):
    ms = await acc.methods(c.from_user.id)
    text = ("🗑 <b>Удалить все способы оплаты?</b>\n\nВсе сохранённые карты будут отвязаны, автоплатёж выключится." if ms
            else "🗑 <b>Удалить все способы оплаты?</b>\n\nСохранённых способов оплаты сейчас нет. Отвязать всё равно?")
    await show(c, text, kb_rows([[("✅ Удалить все", "p:mday")]], back="p:methods", back_text="← Отмена"))


@router.callback_query(F.data == "p:mday")
async def method_delete_all(c: CallbackQuery):
    n = await svc.deactivate_method(c.from_user.id, None, "user")
    await c.answer(f"✅ Удалено: {n}" if n else "Сохранённых способов нет", show_alert=not n)
    await profile_methods(c)


@router.callback_query(F.data == "p:ap")
async def autopay_toggle(c: CallbackQuery):
    u, _ = await _user(c.from_user.id)
    try:
        on = await acc.set_autopay(c.from_user.id, not u.autopay_enabled)
    except UserError as ex:
        return await c.answer(str(ex)[:190], show_alert=True)
    await c.answer("✅ Автоплатёж включён" if on else "Автоплатёж выключен")
    await profile_methods(c)


@router.callback_query(F.data == "p:hist")
async def profile_history(c: CallbackQuery):
    items = (await acc.history(c.from_user.id, 15))
    lines = ["🧾 <b>История операций</b>", ""]
    if not items:
        lines.append("Операций пока нет.")
    for x in items:
        d = x["created"][:10].split("-")
        sign = "+" if x["amount_rub"] > 0 else "−"
        lines.append(f"{d[2]}.{d[1]} · {sign}{rub(int(round(abs(x['amount_rub']) * 100)))} · {e(x['title'])}" + (f" · {e(x['detail'])}" if x.get("detail") else "")
                     + (f' · <a href="{e(x["receipt_url"])}">чек</a>' if x.get("receipt_url") else ""))
    await show(c, "\n".join(lines), kb_rows([], back="p:home"))


@router.callback_query(F.data == "p:promo")
async def profile_promo(c: CallbackQuery, state: FSMContext):
    await state.set_state(PromoInput.code)
    await show(c, "🎁 <b>Промокод</b>\n\nОтправьте код сообщением. Бонус зачислится на баланс, скидка применится к следующей покупке сервера.",
               kb_rows([], back="p:home"))


@router.message(PromoInput.code)
async def profile_promo_code(m: Message, state: FSMContext):
    await state.clear()
    try:
        r = await acc.apply_promo(m.from_user.id, m.text or "")
    except UserError as ex:
        await m.answer(f"❌ {e(ex)}", parse_mode="HTML", reply_markup=kb_rows([[("🎁 Ввести другой", "p:promo")]], back="p:home"))
        return
    await m.answer(f"✅ {e(r.get('message', 'Промокод применён'))}", parse_mode="HTML", reply_markup=kb_rows([], back="p:home", back_text="← Профиль"))


@router.callback_query(F.data == "p:ref")
async def profile_ref(c: CallbackQuery):
    r = await referral.stats(c.from_user.id, ctx().bot_username)
    terms = f"{r['pct']}% с пополнений друга" + (f" в течение {r['months']} мес." if r["months"] else "") + (f", другу — {rub(r['welcome_rub'] * 100)} после первой оплаты" if r["welcome_rub"] else "")
    text = (f"👥 <b>Пригласите друзей</b>\n\n{terms}. Бонусы — на баланс.\n\n"
            f"Приглашено: <b>{r['invited']}</b> · оплатили: <b>{r['active']}</b>\nЗаработано: <b>{rub(int(round(r['earned_rub'] * 100)))}</b>\n\n"
            f"Ваша ссылка:\n<code>{e(r['bot'] or r['web'])}</code>")
    b = InlineKeyboardBuilder.from_markup(kb_rows([], back="p:home"))
    if r["bot"]:
        from urllib.parse import quote
        b.row(InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url={quote(r['bot'])}&text={quote('Арендую VPS здесь — сервер за пару минут:')}"))
    await show(c, text, b.as_markup())


@router.callback_query(F.data == "p:news")
async def profile_news(c: CallbackQuery):
    u, _ = await _user(c.from_user.id)
    on = await acc.set_ads_consent(c.from_user.id, not u.ads_consent, "bot")
    await c.answer("🔔 Будем присылать новости и акции" if on else "🔕 Вы отписались от новостей и акций")
    await profile_home(c, None)


@router.callback_query(F.data == "p:docs")
async def profile_docs(c: CallbackQuery):
    base = env().public_url.rstrip("/")
    b = InlineKeyboardBuilder()
    if base.startswith("https://"):
        for k in (*REQUIRED, "autopay", "ads"):
            b.row(InlineKeyboardButton(text="📄 " + DOCS[k]["title"], url=f"{base}/legal/{k}"))
    b.row(InlineKeyboardButton(text="← Назад", callback_data="p:home"))
    await show(c, "📄 <b>Документы</b>\n\nОтзыв согласия на обработку данных — в личном кабинете (Профиль → Документы) или через поддержку.", b.as_markup())


# ───────────────────────── поддержка ─────────────────────────


@router.callback_query(F.data == "m:support")
async def support_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    app_url = env().public_url.rstrip("/") + "/app?tickets=1"
    if app_url.startswith("https://"):
        b.row(InlineKeyboardButton(text="💬 Мои обращения", web_app=WebAppInfo(url=app_url)))
    b.row(InlineKeyboardButton(text="← Назад", callback_data="m:home"))
    await show(c, "💬 <b>Поддержка</b>\n\nНапишите вопрос одним сообщением прямо в этот чат — ответим здесь же.\n"
                  "Если вопрос про сервер, укажите его название или IP.", b.as_markup())


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def support_message(m: Message):
    """Любой текст, написанный боту, попадает в поддержку: в открытое обращение или в новое."""
    try:
        t = await tk.latest_open(m.from_user.id)
        if t:
            await tk.client_reply(ctx(), m.from_user.id, t.id, m.text, source="bot")
            tid = t.id
        else:
            tid = (await tk.create(ctx(), m.from_user.id, "", m.text, source="bot")).id
    except UserError as ex:
        await m.answer(f"⚠️ {e(ex)}", parse_mode="HTML")
        return
    await m.answer(f"✅ <b>Передали в поддержку</b> · обращение #{tid}\n\nОтветим в этом чате, обычно в течение дня.", parse_mode="HTML")


@router.message(StateFilter(None), F.photo | F.document | F.video | F.voice)
async def support_media(m: Message):
    await m.answer("⚠️ Пока принимаем обращения только текстом. Опишите проблему словами — при необходимости попросим скриншот.")


def make_member_checker(bot: Bot):
    """Проверка подписки и проверка канала для админки."""
    async def check_member(chat, tg_id: int) -> bool | None:
        try:
            m = await bot.get_chat_member(chat, tg_id)
        except TelegramBadRequest as ex:
            if "user not found" in str(ex).lower() or "participant" in str(ex).lower():
                return False
            log.warning("getChatMember %s: %s", chat, ex)
            return None
        except Exception:
            log.warning("getChatMember %s failed", chat, exc_info=True)
            return None
        st = getattr(m, "status", "")
        st = getattr(st, "value", st)
        if st in ("creator", "administrator", "member"):
            return True
        if st == "restricted":
            return bool(getattr(m, "is_member", False))
        return False

    async def probe_chat(chat) -> dict:
        try:
            ch = await bot.get_chat(chat)
        except Exception as ex:
            return {"ok": False, "error": f"Бот не видит этот канал или группу: {ex}. Добавьте бота администратором."}
        out = {"title": ch.title or "", "link": f"https://t.me/{ch.username}" if getattr(ch, "username", None) else (getattr(ch, "invite_link", None) or "")}
        try:
            me = await bot.get_me()
            mm = await bot.get_chat_member(chat, me.id)
            st = getattr(mm.status, "value", mm.status)
        except Exception as ex:
            return {**out, "ok": False, "error": f"Не удалось проверить права бота: {ex}"}
        if st not in ("administrator", "creator"):
            return {**out, "ok": False, "error": "Бот не администратор — без этого Telegram не даёт проверять подписчиков."}
        if not out["link"]:
            out["note"] = "Канал закрытый: укажите ссылку-приглашение, иначе клиентам некуда будет нажать."
        return {**out, "ok": True}

    return check_member, probe_chat


def make_poster(bot: Bot):
    """Отправка в любой чат (каналы событий). Возвращает None или текст ошибки."""
    from aiogram.types import FSInputFile

    async def post(chat, text: str) -> str | None:
        try:
            await bot.send_message(chat, text, parse_mode="HTML", disable_web_page_preview=True)
            return None
        except Exception as ex:
            return str(ex)

    async def post_file(chat, path: str, caption: str) -> str | None:
        try:
            await bot.send_document(chat, FSInputFile(path), caption=caption[:1000])
            return None
        except Exception as ex:
            return str(ex)

    return post, post_file


def make_kb_notifier(bot: Bot):
    """Сообщение с кнопками-действиями: rows = [[(текст, callback), …], …]."""
    async def notify_kb(tg_id: int, text: str, rows: list) -> None:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows]) if rows else None
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            log.warning("не удалось отправить сообщение %s", tg_id, exc_info=True)
    return notify_kb


def make_transfer_offer(bot: Bot, prefix: str = "tr"):
    async def offer(tg_id: int, text: str, tid: int) -> None:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Принять", callback_data=f"{prefix}:a:{tid}"),
                                                     InlineKeyboardButton(text="❌ Отклонить", callback_data=f"{prefix}:d:{tid}")]])
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            log.warning("transfer offer %s failed", tg_id, exc_info=True)
    return offer


def make_broadcast_sender(bot: Bot):
    """Одно сообщение рассылки. Возвращает ok | blocked | error; при лимите Telegram ждёт и повторяет."""
    import asyncio

    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

    async def send(tg_id: int, text: str, buttons: list) -> str:
        kb = None
        if buttons:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, url=u) if u else InlineKeyboardButton(text=t, callback_data=cb)]
                                                       for t, u, cb in buttons])
        for _ in range(3):
            try:
                await bot.send_message(tg_id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=False)
                return "ok"
            except TelegramRetryAfter as ex:
                await asyncio.sleep(ex.retry_after + 1)
            except TelegramForbiddenError:
                return "blocked"
            except TelegramBadRequest as ex:
                msg = str(ex).lower()
                if "chat not found" in msg or "user is deactivated" in msg:
                    return "blocked"
                log.warning("broadcast %s: %s", tg_id, ex)
                return "error"
            except Exception:
                log.warning("broadcast %s failed", tg_id, exc_info=True)
                return "error"
        return "error"

    return send


# ───────────────────────── новости: отписка ─────────────────────────


@router.callback_query(F.data.in_({"nw:off", "nw:on"}))
async def news_toggle(c: CallbackQuery):
    on = c.data == "nw:on"
    try:
        await acc.set_ads_consent(c.from_user.id, on, "bot")
    except UserError:
        return await c.answer()
    if on:
        await c.answer("✅ Готово: будем присылать новости и акции. Отписаться можно в любой момент.", show_alert=True)
    else:
        await c.answer("🔕 Вы отписались от новостей и акций. Уведомления о серверах и оплате будут приходить как раньше.", show_alert=True)
    try:
        await c.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🔔 Снова получать новости" if not on else "🔕 Отписаться от новостей", callback_data="nw:on" if not on else "nw:off")]]))
    except TelegramBadRequest:
        pass


def make_button_notifier(bot: Bot):
    async def notify_btn(tg_id: int, text: str, button: str, url: str) -> None:
        kb = None
        if url.startswith("https://"):
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button, web_app=WebAppInfo(url=url))]])
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            log.warning("не удалось отправить сообщение %s", tg_id, exc_info=True)

    return notify_btn
