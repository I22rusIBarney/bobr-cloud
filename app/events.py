"""Куда отправлять служебные события: в отдельный канал/группу или в личку администраторам.

Для каждого события в админке задаётся чат (пусто — личка админов) и переключатель.
Если в канал отправить не удалось (бота удалили, нет прав), сообщение уходит админам —
важное не теряется.
"""
from __future__ import annotations

import html
import logging

from .config import env
from .db import session
from .settings import load_settings

log = logging.getLogger(__name__)

EVENTS: dict[str, tuple[str, str]] = {
    "alerts": ("Алерты", "Сбои Astra и ЮKassa, низкий баланс, ошибки чеков"),
    "tickets": ("Обращения", "Новые обращения и ответы клиентов"),
    "sales": ("Покупки", "Новые серверы и увеличение мощности"),
    "payments": ("Пополнения", "Пополнения баланса и автоплатежи"),
    "referrals": ("Рефералы", "Новые приглашённые и бонусы"),
    "backups": ("Бэкапы", "Файлы резервных копий базы"),
    "health": ("Мониторинг", "Сбои и восстановление сервисов, сводка раз в сутки"),
    "security": ("Безопасность", "Входы на сервер по SSH и подбор паролей"),
}


def who(u) -> str:
    """Клиент для служебных сообщений: имя, @username, ID."""
    if u is None:
        return "—"
    name = html.escape(u.first_name or "без имени")
    return f"{name}" + (f" @{html.escape(u.username)}" if u.username else "") + f" · <code>{u.tg_id}</code>"


async def route(event: str) -> tuple[bool, str]:
    async with session() as s:
        cfg = await load_settings(s)
    return bool(cfg.get(f"ev_{event}_on", True)), (cfg.get(f"ev_{event}_chat") or "").strip()


def _ref(chat: str):
    from .subscription import chat_ref
    return chat_ref(chat)


async def emit(ctx, event: str, text: str, admin_url: str | None = None) -> None:
    """text — готовый HTML. admin_url — кнопка «Открыть» (только в личке админов: в каналах мини-приложение не открыть)."""
    try:
        on, chat = await route(event)
    except Exception:
        log.exception("events route")
        on, chat = True, ""
    if not on:
        return
    if chat and getattr(ctx, "post", None):
        err = await ctx.post(_ref(chat), text)
        if err is None:
            return
        log.warning("event %s → %s: %s", event, chat, err)
        text += f"\n\n<i>Не удалось отправить в {html.escape(chat)}: {html.escape(err)[:200]}</i>"
    for aid in env().admins:
        if admin_url and ctx.notify_btn:
            await ctx.notify_btn(aid, text, "Открыть", admin_url)
        else:
            await ctx.notify(aid, text)


async def send_file(ctx, event: str, path: str, caption: str) -> str | None:
    """Файл в чат события (или админам). Возвращает ошибку или None."""
    on, chat = await route(event)
    targets = [_ref(chat)] if chat else list(env().admins)
    errors = []
    for t in targets:
        err = await ctx.post_file(t, path, caption)
        if err:
            errors.append(f"{t}: {err}")
    if errors and chat:  # канал недоступен — отдаём админам
        for aid in env().admins:
            await ctx.post_file(aid, path, caption + f"\n\nНе удалось отправить в {chat}.")
    return "; ".join(errors) or None


async def test(ctx, event: str, chat: str) -> str | None:
    title = EVENTS.get(event, (event, ""))[0]
    text = f"✅ <b>Проверка</b>\n\nСюда будут приходить события «{html.escape(title)}» из {html.escape(ctx.bot_username and '@' + ctx.bot_username or 'бота')}."
    if chat.strip():
        return await ctx.post(_ref(chat), text)
    for aid in env().admins:
        await ctx.notify(aid, text)
    return None


def make_alert(ctx):
    """Замена ctx.alert_admin: системные алерты идут в чат «Алерты»."""
    async def alert(text: str) -> None:
        await emit(ctx, "alerts", html.escape(text))
    return alert
