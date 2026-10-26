"""Обязательная подписка на канал или группу перед использованием сервиса.

Проверка — через Bot API getChatMember: бот должен быть администратором канала/группы.
Если проверить нельзя (бот не админ, канал удалён), доступ не блокируем и предупреждаем админа:
лучше пропустить клиента, чем запереть всех.
"""
from __future__ import annotations

import logging
import time

from .config import env
from .db import session
from .settings import load_settings

log = logging.getLogger(__name__)

TTL_OK = 600  # подписанных перепроверяем не чаще раза в 10 минут
_ok_until: dict[int, float] = {}


def chat_ref(raw: str) -> str | int:
    """'@name', 'https://t.me/name', 'name' → '@name'; '-100…' → int."""
    v = (raw or "").strip()
    for p in ("https://t.me/", "http://t.me/", "t.me/"):
        if v.startswith(p):
            v = v[len(p):]
    v = v.split("?")[0].strip("/")
    if v.lstrip("-").isdigit():
        return int(v)
    return "@" + v.lstrip("@") if v else ""


def public_link(cfg: dict) -> str:
    if cfg.get("sub_link"):
        return cfg["sub_link"].strip()
    ref = chat_ref(cfg.get("sub_chat", ""))
    return f"https://t.me/{ref[1:]}" if isinstance(ref, str) and ref.startswith("@") else ""


def forget(tg_id: int) -> None:
    _ok_until.pop(tg_id, None)


async def required(ctx, tg_id: int) -> dict | None:
    """None — можно пользоваться; dict(link, title) — нужно подписаться."""
    async with session() as s:
        cfg = await load_settings(s)
    if not cfg.get("sub_required") or not cfg.get("sub_chat") or tg_id in env().admins:
        return None
    if _ok_until.get(tg_id, 0) > time.time():
        return None
    check = getattr(ctx, "check_member", None)
    if check is None:
        return None
    res = await check(chat_ref(cfg["sub_chat"]), tg_id)
    if res is None:  # проверить не удалось — пропускаем
        await ctx.alert_once("sub_check", "⚠️ Не удаётся проверить подписку на канал. Убедитесь, что бот — администратор канала/группы "
                             f"{cfg['sub_chat']}. Пока проверка не работает, клиенты проходят без подписки.")
        return None
    if res:
        _ok_until[tg_id] = time.time() + TTL_OK
        return None
    return {"link": public_link(cfg), "title": cfg.get("sub_title") or "наш канал"}
