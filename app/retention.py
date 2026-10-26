"""Удержание: брошенная оплата, возврат ушедших клиентов, срок персональных скидок.

Брошенная оплата. Клиент начал пополнение, но не оплатил. Через час бот напоминает (служебное
сообщение о его же действии). Через сутки, если у клиента нет работающих серверов и он дал
согласие на рекламу, — персональная скидка на покупку сервера на несколько дней (не чаще раза в 30 дней).

Возврат ушедших. Серверов больше нет, последний закончился 2+ недели назад (но не больше 90 дней) —
с согласия на рекламу приходит персональная скидка на новый сервер, не чаще раза в 90 дней.

Скидка не суммируется с уже выданной по промокоду и сгорает по истечении срока.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import func, select, update

from .db import Payment, Server, User, session, utcnow
from .settings import fmt_rub, load_settings

log = logging.getLogger(__name__)

REMIND_AFTER = timedelta(hours=1)
PROMO_AFTER = timedelta(hours=24)
LOOKBACK = timedelta(hours=72)
ABANDON_PROMO_EVERY = timedelta(days=30)
WINBACK_EVERY = timedelta(days=90)
WINBACK_MAX_AGE = timedelta(days=90)
LIVE = ("active", "suspended", "provisioning", "expired")
_last_winback = 0.0


async def _send(ctx, tg_id: int, text: str, rows: list) -> None:
    fn = getattr(ctx, "notify_kb", None)
    if fn:
        await fn(tg_id, text, rows)
    else:
        await ctx.notify(tg_id, text)


async def _has_live(s, tg_id: int) -> bool:
    return bool((await s.execute(select(func.count()).select_from(Server).where(Server.tg_id == tg_id, Server.status.in_(LIVE)))).scalar_one())


def _grant(u: User, pct: int, days: int, label: str) -> None:
    u.promo_discount_pct, u.promo_code, u.promo_until = int(pct), label, utcnow() + timedelta(days=days)


async def abandoned(ctx) -> None:
    async with session() as s:
        cfg = await load_settings(s)
    if not cfg.get("abandon_enabled", True):
        return
    now = utcnow()
    async with session() as s:
        rows = (await s.execute(select(Payment).where(Payment.autopay.is_(False), Payment.status != "succeeded", Payment.remind_stage < 2,
                                                      Payment.created_at > now - LOOKBACK).order_by(Payment.created_at.desc()))).scalars().all()
    seen: set[int] = set()
    for p in rows:
        if p.tg_id in seen:  # напоминаем только о последней попытке
            await _stage(p.id, 2)
            continue
        seen.add(p.tg_id)
        age = now - p.created_at
        async with session() as s:
            paid_after = (await s.execute(select(func.count()).select_from(Payment).where(
                Payment.tg_id == p.tg_id, Payment.status == "succeeded", Payment.created_at > p.created_at))).scalar_one()
            u = await s.get(User, p.tg_id)
        if paid_after or u is None or u.banned or u.bot_blocked:
            await _stage(p.id, 2)
            continue
        if p.remind_stage == 0 and age >= timedelta(hours=6):  # старые попытки (например, до обновления) — без напоминания
            await _stage(p.id, 2)
        elif p.remind_stage == 0 and age >= REMIND_AFTER:
            await _stage(p.id, 1)
            amount = p.amount_kop // 100
            await _send(ctx, p.tg_id, f"💳 <b>Оплата не завершена</b>\n\nВы начали пополнение на {fmt_rub(p.amount_kop)}, но платёж не прошёл. "
                                      "Если что-то пошло не так — попробуйте ещё раз или напишите в поддержку.",
                        [[(f"💳 Пополнить на {fmt_rub(p.amount_kop)}", f"t:{amount}")], [("💬 Поддержка", "m:support")]])
        elif p.remind_stage == 1 and age >= PROMO_AFTER:
            await _stage(p.id, 2)
            pct, days = int(cfg.get("abandon_promo_pct", 10)), int(cfg.get("abandon_promo_days", 3))
            async with session() as s:
                u = await s.get(User, p.tg_id)
                ok = (pct > 0 and u.ads_consent and not u.promo_discount_pct and not await _has_live(s, u.tg_id)
                      and not (u.abandon_promo_at and u.abandon_promo_at > now - ABANDON_PROMO_EVERY))
                if ok:
                    _grant(u, pct, days, "Персональная")
                    u.abandon_promo_at = now
                    await s.commit()
            if ok:
                await _send(ctx, p.tg_id, f"🎁 <b>Скидка {pct}% на сервер — для вас</b>\n\nВы не завершили оплату вчера. Скидка на первый месяц нового сервера "
                                          f"уже применена к вашему аккаунту и действует {days} {_days(days)}.",
                            [[("🛒 Собрать сервер", "m:buy")]])


async def _stage(pid: int, st: int) -> None:
    async with session() as s:
        await s.execute(update(Payment).where(Payment.id == pid).values(remind_stage=st))
        await s.commit()


def _days(n: int) -> str:
    n = abs(n) % 100
    return "дней" if 11 <= n <= 14 else {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(n % 10, "дней")


async def winback(ctx, force: bool = False) -> int:
    """Раз в 6 часов: персональная скидка тем, кто ушёл 2+ недели назад."""
    global _last_winback
    if not force and time.time() - _last_winback < 6 * 3600:
        return 0
    _last_winback = time.time()
    async with session() as s:
        cfg = await load_settings(s)
    pct, after, days = int(cfg.get("winback_promo_pct", 15)), int(cfg.get("winback_after_days", 14)), int(cfg.get("winback_promo_days", 7))
    if not cfg.get("winback_enabled", True) or pct <= 0:
        return 0
    now = utcnow()
    end = func.max(func.coalesce(Server.deleted_at, Server.expires_at))
    async with session() as s:
        live = select(Server.tg_id).where(Server.status.in_(LIVE))
        cand = (await s.execute(select(Server.tg_id, end).where(Server.status == "deleted", Server.tg_id.not_in(live)).group_by(Server.tg_id))).all()
    sent = 0
    for tg_id, last_end in cand:
        if last_end is None:
            continue
        if last_end.tzinfo is None:
            from datetime import timezone
            last_end = last_end.replace(tzinfo=timezone.utc)
        if not (now - WINBACK_MAX_AGE <= last_end <= now - timedelta(days=after)):
            continue
        async with session() as s:
            u = await s.get(User, tg_id)
            if (u is None or u.banned or u.bot_blocked or not u.ads_consent or not u.consent_version or u.promo_discount_pct
                    or (u.winback_at and u.winback_at > now - WINBACK_EVERY)):
                continue
            _grant(u, pct, days, "Персональная")
            u.winback_at = now
            await s.commit()
        await _send(ctx, tg_id, f"🦫 <b>Скучаем!</b>\n\nДарим скидку {pct}% на первый месяц нового сервера — она уже применена к вашему аккаунту "
                                f"и действует {days} {_days(days)}. Соберите сервер за пару минут.",
                    [[("🛒 Собрать сервер", "m:buy")], [("🔕 Не присылать предложения", "nw:off")]])
        sent += 1
    return sent


async def expire_discounts() -> None:
    async with session() as s:
        await s.execute(update(User).where(User.promo_until.is_not(None), User.promo_until < utcnow())
                        .values(promo_discount_pct=0, promo_code=None, promo_until=None))
        await s.commit()


async def tick(ctx) -> None:
    await expire_discounts()
    await abandoned(ctx)
    await winback(ctx)
