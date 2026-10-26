"""Передача сервера другому клиенту бота.

Владелец указывает @username или Telegram ID получателя. Получатель должен быть клиентом
(принял документы, не заблокирован). Запрос живёт 24 часа: получатель принимает или отклоняет,
владелец может отменить. После принятия сервер со всем оплаченным сроком принадлежит получателю,
продление идёт с его баланса, возврат при удалении — ему.
"""
from __future__ import annotations

import html
import re
from datetime import timedelta

from sqlalchemy import func, select, update

from .db import Server, ServerTransfer, User, session, utcnow
from .services import UserError, flag
from .settings import load_settings
from . import events

TTL = timedelta(hours=24)


def _who(u: User) -> str:
    return html.escape(u.first_name or "") + (f" (@{html.escape(u.username)})" if u.username else f" (ID {u.tg_id})")


async def find_recipient(query: str) -> User:
    q = (query or "").strip()
    q = re.sub(r"^(https?://)?t\.me/", "", q).lstrip("@").strip()
    if not q:
        raise UserError("Укажите @username или Telegram ID получателя.")
    async with session() as s:
        if q.isdigit():
            u = await s.get(User, int(q))
        else:
            if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", q):
                raise UserError("Похоже, это не username и не ID. Пример: @ivan_petrov или 123456789.")
            u = (await s.execute(select(User).where(func.lower(User.username) == q.lower()))).scalars().first()
    if u is None or not u.consent_version:
        raise UserError("Такого клиента нет в боте. Попросите друга открыть бота и принять документы — потом повторите.")
    if u.banned:
        raise UserError("Этому пользователю передать сервер нельзя.")
    return u


async def create(ctx, from_tg: int, server_id: int, query: str) -> dict:
    async with session() as s:
        cfg = await load_settings(s)
    if not cfg.get("transfers_enabled", True):
        raise UserError("Передача серверов сейчас отключена.")
    to = await find_recipient(query)
    if to.tg_id == from_tg:
        raise UserError("Нельзя передать сервер самому себе.")
    async with session() as s:
        srv = await s.get(Server, server_id)
        if srv is None or srv.tg_id != from_tg or srv.status == "deleted":
            raise UserError("Сервер не найден.")
        if srv.status not in ("active", "suspended"):
            raise UserError("Передать можно только созданный сервер.")
        busy = (await s.execute(select(ServerTransfer).where(ServerTransfer.server_id == server_id, ServerTransfer.status == "pending"))).scalars().first()
        if busy and busy.created_at > utcnow() - TTL:
            raise UserError("Этот сервер уже ждёт подтверждения от получателя. Отмените запрос, чтобы отправить новый.")
        if busy:
            busy.status, busy.finished_at = "expired", utcnow()
        t = ServerTransfer(server_id=server_id, from_tg=from_tg, to_tg=to.tg_id)
        s.add(t)
        sender = await s.get(User, from_tg)
        await s.commit()
        await s.refresh(t)
        info = {"id": t.id, "server": srv.name, "flag": flag(srv.location), "cpu": srv.cpu, "ram": srv.ram, "disk": srv.disk,
                "expires_at": srv.expires_at, "price_kop": srv.price_kop, "auto_renew": srv.auto_renew}
    days = max(0, int((info["expires_at"] - utcnow()).total_seconds() // 86400)) if info["expires_at"] else 0
    from .settings import fmt_rub
    text = (f"🔁 <b>Вам передают сервер</b>\n\n👤 От: {_who(sender)}\n🖥 {info['flag']} {html.escape(info['server'])} · {info['cpu']} vCPU · {info['ram']} ГБ · {info['disk']} ГБ\n"
            f"📅 Оплачен ещё {days} дн., дальше продление {fmt_rub(info['price_kop'])} в месяц — с вашего баланса.\n\n"
            "Примите, если ждёте этот сервер. После принятия смените пароль root. Запрос действует 24 часа.")
    if getattr(ctx, "send_transfer_offer", None):
        await ctx.send_transfer_offer(to.tg_id, text, t.id)
    else:
        await ctx.notify(to.tg_id, text)
    return {"id": t.id, "to": {"tg_id": to.tg_id, "username": to.username, "first_name": to.first_name}}


async def _finish(tid: int, actor: int, new_status: str, by: str) -> ServerTransfer:
    async with session() as s:
        t = await s.get(ServerTransfer, tid)
        if t is None or getattr(t, by) != actor:
            raise UserError("Запрос не найден.")
        if t.status != "pending":
            raise UserError({"accepted": "Сервер уже передан.", "declined": "Запрос уже отклонён.", "cancelled": "Владелец отменил передачу.",
                             "expired": "Запрос истёк."}.get(t.status, "Запрос уже обработан."))
        if t.created_at < utcnow() - TTL:
            t.status, t.finished_at = "expired", utcnow()
            await s.commit()
            raise UserError("Запрос истёк — попросите владельца отправить его снова.")
        t.status, t.finished_at = new_status, utcnow()
        await s.commit()
        return t


async def accept(ctx, tid: int, to_tg: int) -> dict:
    t = await _finish(tid, to_tg, "accepted", "to_tg")
    async with session() as s:
        res = await s.execute(update(Server).where(Server.id == t.server_id, Server.tg_id == t.from_tg, Server.status.in_(["active", "suspended"]))
                              .values(tg_id=to_tg, notified_stage=0))
        if res.rowcount != 1:
            row = await s.get(ServerTransfer, tid)
            row.status = "cancelled"
            await s.commit()
            raise UserError("Сервер больше недоступен для передачи (удалён или истёк).")
        srv = await s.get(Server, t.server_id)
        a, b = await s.get(User, t.from_tg), await s.get(User, to_tg)
        await s.commit()
        name = srv.name
    from .shares import revoke_all
    await revoke_all(ctx, t.server_id, "сервер передан другому владельцу")
    await ctx.notify(t.from_tg, f"✅ <b>Сервер {html.escape(name)} передан</b>\n\nПолучатель: {_who(b)}. Сервер больше не отображается у вас.")
    await events.emit(ctx, "sales", f"🔁 <b>Передача сервера</b>\n\n🖥 {html.escape(name)}\n👤 {events.who(a)} → {events.who(b)}")
    return {"server_id": t.server_id, "name": name}


async def decline(ctx, tid: int, to_tg: int) -> None:
    t = await _finish(tid, to_tg, "declined", "to_tg")
    async with session() as s:
        srv = await s.get(Server, t.server_id)
    await ctx.notify(t.from_tg, f"❌ Получатель отклонил передачу сервера {html.escape(srv.name if srv else '')}. Сервер остаётся у вас.")


async def cancel(ctx, tid: int, from_tg: int) -> None:
    t = await _finish(tid, from_tg, "cancelled", "from_tg")
    async with session() as s:
        srv = await s.get(Server, t.server_id)
    await ctx.notify(t.to_tg, f"ℹ️ Владелец отменил передачу сервера {html.escape(srv.name if srv else '')}.")


async def pending_for(tg_id: int) -> dict:
    """Входящие и исходящие активные запросы клиента — для кабинета."""
    cutoff = utcnow() - TTL
    async with session() as s:
        rows = (await s.execute(select(ServerTransfer).where(ServerTransfer.status == "pending", ServerTransfer.created_at > cutoff,
                                                             (ServerTransfer.to_tg == tg_id) | (ServerTransfer.from_tg == tg_id)))).scalars().all()
        out = {"incoming": [], "outgoing": []}
        for t in rows:
            srv = await s.get(Server, t.server_id)
            other = await s.get(User, t.from_tg if t.to_tg == tg_id else t.to_tg)
            if srv is None or srv.status == "deleted":
                continue
            item = {"id": t.id, "server_id": srv.id, "server": srv.name, "flag": flag(srv.location), "cpu": srv.cpu, "ram": srv.ram, "disk": srv.disk,
                    "expires_at": srv.expires_at.isoformat() if srv.expires_at else None, "price_rub": srv.price_kop / 100,
                    "user": {"tg_id": other.tg_id, "username": other.username, "first_name": other.first_name} if other else None,
                    "until": (t.created_at + TTL).isoformat()}
            out["incoming" if t.to_tg == tg_id else "outgoing"].append(item)
    return out
