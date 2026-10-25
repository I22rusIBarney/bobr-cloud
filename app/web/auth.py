"""Проверка подписи Telegram Mini App (initData)."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, bot_token: str, max_age_s: int = 86400, now: float | None = None) -> dict | None:
    """Возвращает объект user из initData или None, если подпись неверна/устарела.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got_hash = pairs.pop("hash", None)
    if not got_hash:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got_hash):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        if (now if now is not None else time.time()) - auth_date > max_age_s:
            return None
        return json.loads(pairs["user"])
    except (KeyError, ValueError):
        return None
