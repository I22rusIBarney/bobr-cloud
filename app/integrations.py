"""Загрузка ключей ЮKassa / «Мой налог» из БД (настраиваются в админке).

Для обратной совместимости, если в БД пусто, берутся значения из .env.
"""
from __future__ import annotations

from .config import env
from .db import session
from .services import Ctx
from .settings import get_secrets


async def load_integrations(ctx: Ctx) -> None:
    async with session() as s:
        sec = await get_secrets(s)
    e = env()
    ctx.yk.configure(sec["yk_shop_id"] or e.yookassa_shop_id, sec["yk_secret"] or e.yookassa_secret_key)
    ctx.nalog.configure(sec["nalog_inn"] or e.nalog_inn, sec["nalog_password"] or e.nalog_password)
