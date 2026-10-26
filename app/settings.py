"""Настройки, которыми управляет админ через Mini App, и расчёт цен."""
from __future__ import annotations

import json
import math
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .db import Setting

DEFAULTS: dict[str, Any] = {
    "markup_pct": 40.0,          # наценка на цену Astra, %
    "markup_fixed_rub": 0.0,     # фиксированная добавка к цене в месяц, ₽
    "round_to_rub": 10,          # округлять цену вверх до кратного N ₽ (1 = до рубля)
    "min_topup_rub": 100,
    "max_topup_rub": 20000,
    "auto_renew_default": True,
    "receipts_enabled": True,    # чеки самозанятого после пополнения
    "receipt_name": "Услуги хостинга (пополнение баланса)",
    "change_ip_price_rub": 150,  # у Astra смена IP стоит нам 100 ₽
    "refund_pct": 80,            # возврат клиенту при удалении сервера: % от остатка срока
    "low_balance_rub": 1000,     # алерт админу, если баланс у Astra ниже
    "maintenance": False,        # закрыть продажи
    "hidden_presets": [],        # индексы тарифов Astra, скрытые из продажи
    "welcome_text": "🌍 Серверы в Европе\n⚡ Автоматическая выдача за 1–3 минуты\n🛡 Простое управление и удобная оплата",
    # автоплатежи: включайте после того, как ЮKassa подключит рекуррентные платежи магазину
    "autopay_available": False,
    # реквизиты исполнителя и документы
    "service_name": "Bobr Cloud",
    "seller_name": "",
    "seller_inn": "",
    "seller_email": "",
    "seller_status": "плательщик налога на профессиональный доход (самозанятый)",
    "docs_version": 1,
    "docs_date": "",
    # поддержка провайдера, куда передаются технические проблемы (username без @)
    "provider_support": "AstraSupportroBot",
    # покупка сразу на несколько месяцев: скидка клиенту за срок (продление потом — помесячно)
    "periods_enabled": True,
    "period_3m_pct": 5,
    "period_6m_pct": 10,
    "period_1y_pct": 15,
    # обязательная подписка на канал/группу (бот должен быть там администратором)
    "sub_required": False,
    "sub_chat": "",              # @username или -100… для закрытых
    "sub_link": "",              # ссылка-приглашение (для закрытых каналов)
    "sub_title": "",             # название — подставляется при проверке
    # реферальная программа (бонусы только на баланс)
    "ref_enabled": True,
    "ref_pct": 10,               # % от каждого пополнения приглашённого — пригласившему
    "ref_months": 6,             # сколько месяцев после приглашения начисляем (0 — всегда)
    "ref_welcome_rub": 100,      # бонус приглашённому после его первой оплаты
    # куда слать служебные события: чат (@канал или -100…), пусто — личка админов
    **{f"ev_{k}_on": True for k in ("alerts", "tickets", "sales", "payments", "referrals", "backups")},
    **{f"ev_{k}_chat": "" for k in ("alerts", "tickets", "sales", "payments", "referrals", "backups")},
    "backup_hours": 0,           # резервная копия базы в Telegram каждые N часов (0 — выключено)
    # оформление кабинета
    "theme_bg": "none",          # none | aurora | night | ocean | sunset | forest | image
    "theme_bg_image": "",        # адрес своей картинки (заполняется при загрузке)
    "theme_effect": "auto",      # auto (по календарю) | none | snow | newyear | halloween | leaves | hearts | flowers | confetti
    "theme_client_choice": True,  # клиенты могут выбрать свой фон
}

TYPES = {k: type(v) for k, v in DEFAULTS.items()}
THEME_BGS = ("none", "aurora", "night", "ocean", "sunset", "forest", "image")
THEME_EFFECTS = ("auto", "none", "snow", "newyear", "halloween", "leaves", "hearts", "flowers", "confetti")


def coerce(key: str, value: Any) -> Any:
    if key not in DEFAULTS:
        raise KeyError(key)
    t = TYPES[key]
    if t is bool:
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if t is list:
        return [int(x) for x in value]
    if t is int:
        return int(float(value))
    if t is float:
        return float(value)
    return str(value)


async def load_settings(s: AsyncSession) -> dict[str, Any]:
    from sqlalchemy import select

    out = dict(DEFAULTS)
    for row in (await s.execute(select(Setting))).scalars():
        if row.key in DEFAULTS:
            try:
                out[row.key] = coerce(row.key, json.loads(row.value))
            except Exception:
                pass
    return out


async def save_settings(s: AsyncSession, patch: dict[str, Any]) -> dict[str, Any]:
    for k, v in patch.items():
        v = coerce(k, v)
        if k == "markup_pct" and not (-50 <= v <= 1000):
            raise ValueError("markup_pct вне диапазона")
        if k == "theme_bg" and v not in THEME_BGS:
            raise ValueError("неизвестный фон")
        if k == "theme_effect" and v not in THEME_EFFECTS:
            raise ValueError("неизвестный эффект")
        if k in ("period_3m_pct", "period_6m_pct", "period_1y_pct") and not (0 <= v <= 50):
            raise ValueError("Скидка за срок — от 0 до 50%")
        if k == "backup_hours" and v not in (0, 6, 12, 24, 48, 168):
            raise ValueError("Частота бэкапов: 0, 6, 12, 24, 48 или 168 часов")
        if k == "ref_pct" and not (0 <= v <= 50):
            raise ValueError("Реферальный процент — от 0 до 50")
        if k in ("ref_months", "ref_welcome_rub") and v < 0:
            raise ValueError("Значение не может быть отрицательным")
        if k == "round_to_rub" and v < 1:
            raise ValueError("round_to_rub >= 1")
        row = await s.get(Setting, k)
        if row is None:
            s.add(Setting(key=k, value=json.dumps(v)))
        else:
            row.value = json.dumps(v)
    await s.commit()
    return await load_settings(s)


def sell_price_kop(cost_rub: float, cfg: dict[str, Any]) -> int:
    """Цена клиенту в копейках: (себестоимость * (1+наценка) + фикс), округлённая вверх."""
    raw = cost_rub * (1 + cfg["markup_pct"] / 100.0) + cfg["markup_fixed_rub"]
    step = max(1, int(cfg["round_to_rub"]))
    rub = math.ceil(round(raw, 6) / step) * step
    return int(rub * 100)


def no_markup_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Настройки цен для клиента без наценки: себестоимость, округлённая вверх до рубля."""
    return {**cfg, "markup_pct": 0.0, "markup_fixed_rub": 0.0, "round_to_rub": 1}


def user_price_cfg(cfg: dict[str, Any], user: Any) -> dict[str, Any]:
    """Настройки цен для конкретного клиента: без наценки, своя наценка или общая."""
    if user is None:
        return cfg
    if getattr(user, "no_markup", False):
        return {**no_markup_cfg(cfg), "hidden_presets": cfg["hidden_presets"]}
    pct = getattr(user, "markup_pct", None)
    if pct is not None:
        return {**cfg, "markup_pct": float(pct)}
    return cfg


def cost_kop(cost_rub: float) -> int:
    return int(round(cost_rub * 100))


def fmt_rub(kop: int) -> str:
    rub = kop / 100
    return f"{rub:,.0f}".replace(",", " ") + " ₽" if kop % 100 == 0 else f"{rub:,.2f}".replace(",", " ") + " ₽"


# ───────── секреты интеграций (ЮKassa, Мой налог) ─────────
# Хранятся в той же таблице под префиксом "secret:", наружу через load_settings не попадают.

SECRET_KEYS = ("yk_shop_id", "yk_secret", "nalog_inn", "nalog_password")


async def get_secrets(s: AsyncSession) -> dict[str, str]:
    out = {k: "" for k in SECRET_KEYS}
    for k in SECRET_KEYS:
        row = await s.get(Setting, "secret:" + k)
        if row:
            out[k] = row.value
    return out


async def set_secrets(s: AsyncSession, patch: dict[str, str]) -> None:
    for k, v in patch.items():
        if k not in SECRET_KEYS:
            raise KeyError(k)
        row = await s.get(Setting, "secret:" + k)
        if row is None:
            s.add(Setting(key="secret:" + k, value=v))
        else:
            row.value = v
    await s.commit()
