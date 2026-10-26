"""Telegram-бот (aiogram 3): меню клиента, покупка, пополнение, управление серверами."""
from __future__ import annotations

import html
import logging
from datetime import timezone

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
from .legal import DOCS, REQUIRED
from .config import env
from .db import Payment, Server, User, change_balance, get_or_create_user, session
from .services import Ctx, UserError
from .settings import fmt_rub, load_settings

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
    return (
        f"🦫 <b>{e(cfg['service_name'])}</b>\n"
        f"<i>Облачная инфраструктура для ваших проектов.</i>\n\n"
        f"{e(body)}\n\n"
        f"💰 Баланс: <b>{rub(user.balance_kop)}</b>"
    )


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
    b.row(InlineKeyboardButton(text="💬 Поддержка", callback_data="m:support"))
    if admin_btn:  # только администраторам, последней строкой
        b.row(admin_btn)
    return b.as_markup()


# ───────────────────────── старт / меню ─────────────────────────


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
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
    rows = [[(f"{l['flag']} {l['name']}", f"b:l:{l['code']}")] for l in locs]
    await show(c, "🌍 <b>Выберите расположение сервера</b>\n\nЦена зависит от локации. Выдача — за 1–3 минуты.", kb_rows(rows))


@router.callback_query(F.data.startswith("b:l:"))
async def buy_tariffs(c: CallbackQuery):
    loc = c.data.split(":")[2]
    offs = await svc.offers(ctx(), loc, c.from_user.id)
    name = loc_name(await ctx().catalog(), loc)
    if not offs:
        await show(c, f"⚠️ <b>{svc.flag(loc)} {e(name)}</b>\n\nВ этой локации сейчас нет доступных тарифов.", kb_rows([], back="m:buy"))
        return
    rows = [[(f"{o['cpu']} vCPU · {o['ram']} ГБ · {o['disk']} ГБ — {rub(o['price_kop'])}", f"b:p:{loc}:{o['idx']}")] for o in offs]
    await show(c, f"🖥 <b>Конфигурация · {svc.flag(loc)} {e(name)}</b>\n\nПроцессор · память · диск NVMe.\nЦены — за 30 дней.", kb_rows(rows, back="m:buy"))


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
    await show(c, f"💳 <b>Пополнение баланса</b>\n\n💰 Сейчас: <b>{rub(user.balance_kop)}</b>\n\nВыберите сумму. Оплата картой или через СБП — в ЮKassa.", kb_rows(rows))


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
    await _start_payment(m, int(txt))


@router.callback_query(F.data.regexp(r"^t:\d+$"))
async def topup_preset(c: CallbackQuery):
    await c.answer()
    await _start_payment(c, int(c.data.split(":")[1]))


async def _start_payment(target: Message | CallbackQuery, amount: int) -> None:
    tg_id = target.from_user.id
    try:
        pay, url = await svc.create_topup(ctx(), tg_id, amount)
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
        f"💳 <b>Счёт на {rub(amount * 100)}</b>\n\nНажмите «Оплатить» — откроется страница ЮKassa.\nБаланс пополнится автоматически сразу после оплаты.",
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
    return f"<b>{srv.expires_at.astimezone(timezone.utc):%d.%m.%Y}</b> · осталось {max(0, int(d))} дн." if d >= 0 else "<b>срок истёк</b>"


@router.callback_query(F.data == "s:list")
async def servers_list(c: CallbackQuery):
    from sqlalchemy import select

    async with session() as s:
        rows = (await s.execute(select(Server).where(Server.tg_id == c.from_user.id, Server.status != "deleted").order_by(Server.id))).scalars().all()
    if not rows:
        await show(c, "🖥 <b>Мои серверы</b>\n\nСерверов пока нет. Соберите первый — он будет готов через 1–3 минуты.", kb_rows([[("🛒 Купить сервер", "m:buy")]]))
        return
    kb = [[(f"{STATUS_ICON.get(r.status, '•')} {svc.flag(r.location)} {r.name} · {r.ip or 'создаётся'}", f"s:v:{r.id}")] for r in rows]
    active = sum(1 for r in rows if r.status == "active")
    await show(c, f"🖥 <b>Мои серверы</b>\n\nВсего: <b>{len(rows)}</b> · активных: <b>{active}</b>\nВыберите сервер, чтобы открыть управление.", kb_rows(kb))


def card_text(srv: Server) -> str:
    return (
        f"🖥 <b>{e(srv.name)}</b>\n"
        f"{STATUS.get(srv.status, e(srv.status))} · {svc.flag(srv.location)} {e(srv.location.upper())}\n"
        f"{srv.cpu} vCPU · {srv.ram} ГБ RAM · {srv.disk} ГБ · {e(svc.os_label(srv.os))}\n\n"
        f"🛡 <b>Доступ</b>\n"
        f"IP: <code>{e(srv.ip or '—')}</code>\nЛогин: <code>root</code>\nПароль: <code>{e(srv.root_password or '—')}</code>\n\n"
        f"💳 <b>Оплата</b>\n"
        f"Оплачен до: {_left(srv)}\n"
        f"Продление: {rub(srv.price_kop)} за 30 дней\n"
        f"Автопродление: {'✅ включено' if srv.auto_renew else '⚠️ выключено'}"
    )


def card_kb(srv: Server) -> InlineKeyboardMarkup:
    i = srv.id
    rows = [
        [("🔄 Продлить на 30 дней", f"s:rn:{i}"), (("Автопродление: вкл" if srv.auto_renew else "Автопродление: выкл"), f"s:ar:{i}")],
        [("Включить", f"s:a:{i}:start"), ("Выключить", f"s:a:{i}:stop"), ("Перезагрузить", f"s:a:{i}:reboot")],
        [("🖥 Консоль VNC", f"s:a:{i}:vnc"), ("🌍 Сменить IP", f"s:c:{i}:change-ip")],
        [("⚙️ Переустановить ОС", f"s:ri:{i}"), ("🛡 Сбросить пароль", f"s:c:{i}:password")],
        [("❌ Удалить сервер", f"s:d:{i}")],
    ]
    return kb_rows(rows, back="s:list", back_text="← Мои серверы")


async def _own(c: CallbackQuery, sid: int) -> Server | None:
    async with session() as s:
        srv = await s.get(Server, sid)
    if not srv or srv.tg_id != c.from_user.id or srv.status == "deleted":
        await c.answer("❌ Сервер не найден", show_alert=True)
        return None
    return srv


@router.callback_query(F.data.startswith("s:v:"))
async def server_card(c: CallbackQuery):
    srv = await _own(c, int(c.data.split(":")[2]))
    if srv:
        await show(c, card_text(srv), card_kb(srv))


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
    await show(c, card_text(srv), card_kb(srv))


@router.callback_query(F.data.startswith("s:rn:"))
async def renew_now(c: CallbackQuery):
    srv = await _own(c, int(c.data.split(":")[2]))
    if not srv:
        return
    await c.answer("🔄 Продлеваем…")
    try:
        srv = await svc.renew_server(ctx(), srv.id)
    except UserError as ex:
        await c.message.answer(f"❌ {e(ex)}", parse_mode="HTML")
        return
    await show(c, card_text(srv), card_kb(srv))


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
        url = next((resp.get(k) for k in ("url", "link", "vnc_url", "console_url") if isinstance(resp, dict) and resp.get(k)), None)
        await c.message.answer(f"🖥 <b>Консоль VNC</b>\n\n{e(url)}\n\n<i>Ссылка действует ограниченное время.</i>" if url else f"⚠️ Ответ провайдера: <code>{e(str(resp)[:500])}</code>", parse_mode="HTML")
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
