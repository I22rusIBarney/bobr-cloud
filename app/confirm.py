"""Подтверждение опасных действий в кабинете кодом из бота.

Удаление сервера, переустановка и сброс пароля стирают данные. В кабинете перед ними клиент
запрашивает код: бот присылает 6 цифр, код действует 10 минут, даётся 5 попыток. В самом боте
код не нужен — там действие и так подтверждается в чате.
"""
from __future__ import annotations

import html
import secrets
import time

from .services import UserError

TTL = 600
ATTEMPTS = 5
RESEND_AFTER = 30
DANGEROUS = {"delete": "удаление сервера", "reinstall": "переустановку системы", "password": "сброс пароля"}

_codes: dict[tuple[int, int, str], dict] = {}


def _gc() -> None:
    now = time.time()
    for k in [k for k, v in _codes.items() if v["exp"] < now]:
        _codes.pop(k, None)


async def request(ctx, tg_id: int, server_id: int, server_name: str, action: str) -> dict:
    if action not in DANGEROUS:
        raise UserError("Для этого действия код не нужен.")
    _gc()
    key = (tg_id, server_id, action)
    old = _codes.get(key)
    if old and time.time() - old["sent"] < RESEND_AFTER:
        return {"sent": True, "ttl": int(old["exp"] - time.time())}
    code = f"{secrets.randbelow(10**6):06d}"
    _codes[key] = {"code": code, "exp": time.time() + TTL, "left": ATTEMPTS, "sent": time.time()}
    await ctx.notify(tg_id, f"🔐 <b>Код подтверждения: <code>{code}</code></b>\n\nНа {DANGEROUS[action]} <b>{html.escape(server_name)}</b> в личном кабинете. "
                            "Код действует 10 минут.\n\n<i>Если это не вы — никому не сообщайте код и ничего не делайте.</i>")
    return {"sent": True, "ttl": TTL}


def check(tg_id: int, server_id: int, action: str, code: str | None) -> None:
    _gc()
    key = (tg_id, server_id, action)
    c = _codes.get(key)
    if not code:
        raise UserError("Нужен код подтверждения из бота.")
    if c is None:
        raise UserError("Код истёк — запросите новый.")
    if not secrets.compare_digest(str(code).strip(), c["code"]):
        c["left"] -= 1
        if c["left"] <= 0:
            _codes.pop(key, None)
            raise UserError("Слишком много неверных попыток — запросите новый код.")
        raise UserError(f"Неверный код. Осталось попыток: {c['left']}.")
    _codes.pop(key, None)
