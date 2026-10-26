"""Реферальная программа: ссылка-приглашение, бонусы на баланс за оплаты друзей.

Пригласивший получает ref_pct% от каждого пополнения приглашённого (через ЮKassa) в течение ref_months
месяцев; приглашённый — ref_welcome_rub ₽ после своей первой оплаты. Бонусы зачисляются только на баланс
и тратятся на услуги, вывести их нельзя.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import timedelta

from sqlalchemy import func, select, update

from .config import env
from .db import Payment, Tx, User, change_balance, session, utcnow
from .settings import fmt_rub, load_settings

log = logging.getLogger(__name__)

PREFIX = "ref_"
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def code_for(tg_id: int) -> str:
    n, out = int(tg_id), ""
    while True:
        n, r = divmod(n, 36)
        out = _B36[r] + out
        if n == 0:
            return out


def parse(arg: str | None) -> int | None:
    """'ref_abc' или 'abc' → tg_id пригласившего; мусор → None."""
    a = (arg or "").strip().lower()
    if a.startswith(PREFIX):
        a = a[len(PREFIX):]
    if not re.fullmatch(r"[0-9a-z]{1,13}", a):
        return None
    try:
        return int(a, 36)
    except ValueError:
        return None


def links(tg_id: int, bot_username: str | None) -> dict:
    code = code_for(tg_id)
    bot = f"https://t.me/{bot_username}?start={PREFIX}{code}" if bot_username else ""
    web = env().public_url.rstrip("/") + "/r/" + code
    return {"code": code, "bot": bot, "web": web}


async def attach(new_tg_id: int, start_arg: str | None) -> int | None:
    """Привязывает только что зарегистрированного пользователя к пригласившему. Возвращает id пригласившего."""
    ref = parse(start_arg)
    if not ref or ref == new_tg_id:
        return None
    async with session() as s:
        cfg = await load_settings(s)
        if not cfg["ref_enabled"]:
            return None
        inviter = await s.get(User, ref)
        if inviter is None or inviter.banned:
            return None
        res = await s.execute(update(User).where(User.tg_id == new_tg_id, User.referrer_id.is_(None))
                              .values(referrer_id=ref, referred_at=utcnow()))
        await s.commit()
    return ref if res.rowcount == 1 else None


async def reward(ctx, tg_id: int, paid_kop: int, pay_id: int) -> None:
    """Вызывается после зачисления оплаты через ЮKassa. Ошибки не мешают зачислению."""
    try:
        await _reward(ctx, tg_id, paid_kop, pay_id)
    except Exception:
        log.exception("referral reward %s", pay_id)


async def _reward(ctx, tg_id: int, paid_kop: int, pay_id: int) -> None:
    async with session() as s:
        cfg = await load_settings(s)
        u = await s.get(User, tg_id)
        if not cfg["ref_enabled"] or u is None or not u.referrer_id:
            return
        ref_id, referred_at = u.referrer_id, u.referred_at
        welcome = int(cfg["ref_welcome_rub"]) * 100
        got_welcome = False
        if welcome > 0:
            res = await s.execute(update(User).where(User.tg_id == tg_id, User.ref_welcome_paid.is_(False)).values(ref_welcome_paid=True))
            await s.commit()
            if res.rowcount == 1:
                got_welcome = await change_balance(s, tg_id, welcome, "ref_welcome", str(pay_id))
        months = int(cfg["ref_months"])
        in_window = months == 0 or (referred_at is not None and utcnow() - referred_at <= timedelta(days=30 * months))
        bonus = paid_kop * int(cfg["ref_pct"]) // 100
        inviter = await s.get(User, ref_id)
        paid_bonus = False
        if in_window and bonus > 0 and inviter is not None and not inviter.banned:
            paid_bonus = await change_balance(s, ref_id, bonus, "ref_bonus", f"{tg_id}:{pay_id}")
    if got_welcome:
        await ctx.notify(tg_id, f"💰 <b>Бонус за приглашение</b>\n\nЗачислили <b>{fmt_rub(welcome)}</b> на баланс.")
    if paid_bonus:
        await ctx.notify(ref_id, f"💰 <b>Бонус за друга</b>\n\nВаш друг пополнил баланс — начислили <b>{fmt_rub(bonus)}</b>.")
    if paid_bonus or got_welcome:
        from . import events
        async with session() as s:
            inv, fr = await s.get(User, ref_id), await s.get(User, tg_id)
        await events.emit(ctx, "referrals", "💰 <b>Реферальные бонусы</b>\n\n"
                          + (f"Пригласившему {events.who(inv)}: <b>{fmt_rub(bonus)}</b>\n" if paid_bonus else "")
                          + (f"Приглашённому {events.who(fr)}: <b>{fmt_rub(welcome)}</b> (первая оплата)" if got_welcome else ""))


async def stats(tg_id: int, bot_username: str | None) -> dict:
    async with session() as s:
        cfg = await load_settings(s)
        invited = (await s.execute(select(func.count()).select_from(User).where(User.referrer_id == tg_id))).scalar_one()
        active = (await s.execute(
            select(func.count(func.distinct(Payment.tg_id))).where(
                Payment.status == "succeeded", Payment.tg_id.in_(select(User.tg_id).where(User.referrer_id == tg_id)))
        )).scalar_one()
        earned = (await s.execute(select(func.coalesce(func.sum(Tx.amount_kop), 0)).where(Tx.tg_id == tg_id, Tx.kind == "ref_bonus"))).scalar_one()
    return {
        "enabled": cfg["ref_enabled"], "pct": cfg["ref_pct"], "months": cfg["ref_months"], "welcome_rub": cfg["ref_welcome_rub"],
        "invited": invited, "active": active, "earned_rub": earned / 100, **links(tg_id, bot_username),
    }


async def admin_info(tg_id: int) -> dict:
    """Для карточки клиента в админке."""
    async with session() as s:
        u = await s.get(User, tg_id)
        inviter = await s.get(User, u.referrer_id) if u and u.referrer_id else None
        invited = (await s.execute(select(User).where(User.referrer_id == tg_id).order_by(User.created_at.desc()).limit(50))).scalars().all()
        earned = (await s.execute(select(func.coalesce(func.sum(Tx.amount_kop), 0)).where(Tx.tg_id == tg_id, Tx.kind == "ref_bonus"))).scalar_one()
    who = lambda x: {"tg_id": x.tg_id, "username": x.username, "first_name": x.first_name}
    return {"referrer": who(inviter) if inviter else None, "invited": [who(x) for x in invited], "earned_rub": earned / 100}


def joined_text(new_user) -> str:
    name = new_user.first_name or (("@" + new_user.username) if new_user.username else "Новый пользователь")
    return f"✅ <b>По вашей ссылке присоединился {html.escape(name)}</b>\n\nБонус начислим, когда друг пополнит баланс."
