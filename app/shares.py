"""Общий доступ к серверу.

Владелец приглашает другого клиента бота по @username или Telegram ID. Тот принимает приглашение,
и сервер появляется в его списке с пометкой «Общий доступ». Участник видит IP и пароль, может
включать, выключать и перезагружать сервер, открывать VNC и продлевать сервер со своего баланса.
Переустановка, сброс пароля, смена IP, мощность, переименование, автопродление, удаление, передача
и управление доступом — только у владельца. Приглашение живёт 24 часа. Владелец может закрыть
доступ, участник — выйти сам. При передаче сервера другому владельцу доступ у всех закрывается.
"""
from __future__ import annotations

import html
from datetime import timedelta

from sqlalchemy import select, update

from .db import Server, ServerShare, User, session, utcnow
from .services import UserError, flag
from .settings import load_settings
from . import events, serverlog

TTL = timedelta(hours=24)
MAX_MEMBERS = 5
MEMBER_ACTIONS = {"start", "stop", "reboot", "vnc"}
LIVE = ("active", "suspended")


def _who(u: User | None) -> str:
    if u is None:
        return "клиент"
    return html.escape(u.first_name or "") + (f" (@{html.escape(u.username)})" if u.username else f" (ID {u.tg_id})")


def _user(u: User | None) -> dict | None:
    return {"tg_id": u.tg_id, "username": u.username, "first_name": u.first_name} if u else None


async def role(s, tg_id: int, srv: Server | None) -> str | None:
    """owner | member | None — кем приходится клиент серверу."""
    if srv is None or srv.status == "deleted":
        return None
    if srv.tg_id == tg_id:
        return "owner"
    sh = (await s.execute(select(ServerShare.id).where(ServerShare.server_id == srv.id, ServerShare.member_tg == tg_id,
                                                       ServerShare.owner_tg == srv.tg_id, ServerShare.status == "active"))).first()
    return "member" if sh else None


async def invite(ctx, owner_tg: int, server_id: int, query: str) -> dict:
    from .transfers import find_recipient

    async with session() as s:
        cfg = await load_settings(s)
    if not cfg.get("shares_enabled", True):
        raise UserError("Общий доступ к серверам сейчас отключён.")
    to = await find_recipient(query)
    if to.tg_id == owner_tg:
        raise UserError("Это вы — сервер и так ваш.")
    async with session() as s:
        srv = await s.get(Server, server_id)
        if srv is None or srv.tg_id != owner_tg or srv.status == "deleted":
            raise UserError("Сервер не найден.")
        if srv.status not in LIVE:
            raise UserError("Поделиться можно только созданным сервером.")
        rows = (await s.execute(select(ServerShare).where(ServerShare.server_id == server_id, ServerShare.owner_tg == owner_tg,
                                                          ServerShare.status.in_(["pending", "active"])))).scalars().all()
        now = utcnow()
        for r in rows:  # просроченные приглашения закрываем
            if r.status == "pending" and r.created_at <= now - TTL:
                r.status, r.finished_at = "expired", now
        rows = [r for r in rows if r.status in ("pending", "active")]
        same = next((r for r in rows if r.member_tg == to.tg_id), None)
        if same:
            raise UserError("У этого клиента уже есть доступ." if same.status == "active" else "Приглашение этому клиенту уже отправлено.")
        if len(rows) >= MAX_MEMBERS:
            raise UserError(f"Доступ можно дать не больше чем {MAX_MEMBERS} клиентам. Закройте доступ кому-нибудь, чтобы пригласить ещё.")
        sh = ServerShare(server_id=server_id, owner_tg=owner_tg, member_tg=to.tg_id)
        s.add(sh)
        owner = await s.get(User, owner_tg)
        await s.commit()
        await s.refresh(sh)
        name, fl, spec = srv.name, flag(srv.location), f"{srv.cpu} vCPU · {srv.ram} ГБ · {srv.disk} ГБ"
    text = (f"👥 <b>Вам открывают доступ к серверу</b>\n\n👤 Владелец: {_who(owner)}\n🖥 {fl} {html.escape(name)} · {spec}\n\n"
            "Вы увидите IP и пароль, сможете включать, выключать и перезагружать сервер, открывать VNC и продлевать его со своего баланса. "
            "Сервер остаётся у владельца. Приглашение действует 24 часа.")
    await serverlog.add(server_id, owner_tg, "share_invite", to.first_name or (f"@{to.username}" if to.username else str(to.tg_id)))
    send = getattr(ctx, "send_share_offer", None)
    if send:
        await send(to.tg_id, text, sh.id)
    else:
        await ctx.notify(to.tg_id, text)
    return {"id": sh.id, "to": _user(to)}


async def _finish(sid: int, actor: int, by: str, new_status: str, allowed: tuple[str, ...]) -> ServerShare:
    async with session() as s:
        sh = await s.get(ServerShare, sid)
        if sh is None or getattr(sh, by) != actor:
            raise UserError("Приглашение не найдено.")
        if sh.status not in allowed:
            raise UserError({"active": "Доступ уже открыт.", "declined": "Приглашение уже отклонено.", "cancelled": "Владелец отменил приглашение.",
                             "revoked": "Владелец закрыл доступ.", "left": "Вы уже вышли из доступа.", "expired": "Приглашение истекло."}.get(sh.status, "Уже обработано."))
        if sh.status == "pending" and sh.created_at <= utcnow() - TTL:
            sh.status, sh.finished_at = "expired", utcnow()
            await s.commit()
            raise UserError("Приглашение истекло — попросите владельца отправить его снова.")
        srv = await s.get(Server, sh.server_id)
        if srv is None or srv.status == "deleted" or srv.tg_id != sh.owner_tg:
            sh.status, sh.finished_at = "cancelled", utcnow()
            await s.commit()
            raise UserError("Сервер больше недоступен.")
        sh.status = new_status
        if new_status == "active":
            sh.accepted_at = utcnow()
        else:
            sh.finished_at = utcnow()
        await s.commit()
        return sh


async def _names(sh: ServerShare) -> tuple[str, User | None, User | None]:
    async with session() as s:
        srv = await s.get(Server, sh.server_id)
        return (srv.name if srv else ""), await s.get(User, sh.owner_tg), await s.get(User, sh.member_tg)


async def accept(ctx, share_id: int, member_tg: int) -> dict:
    sh = await _finish(share_id, member_tg, "member_tg", "active", ("pending",))
    name, owner, member = await _names(sh)
    await serverlog.add(sh.server_id, member_tg, "share_join")
    await ctx.notify(sh.owner_tg, f"👥 {_who(member)} принял(а) доступ к серверу <b>{html.escape(name)}</b>.")
    await events.emit(ctx, "sales", f"👥 <b>Общий доступ</b>\n\n🖥 {html.escape(name)}\n👤 {events.who(owner)} → {events.who(member)}")
    return {"server_id": sh.server_id, "name": name}


async def decline(ctx, share_id: int, member_tg: int) -> None:
    sh = await _finish(share_id, member_tg, "member_tg", "declined", ("pending",))
    name, _, member = await _names(sh)
    await ctx.notify(sh.owner_tg, f"❌ {_who(member)} отклонил(а) доступ к серверу {html.escape(name)}.")


async def remove(ctx, share_id: int, owner_tg: int) -> None:
    """Владелец отменяет приглашение или закрывает доступ."""
    async with session() as s:
        sh = await s.get(ServerShare, share_id)
        st = sh.status if sh and sh.owner_tg == owner_tg else None
    sh = await _finish(share_id, owner_tg, "owner_tg", "cancelled" if st == "pending" else "revoked", ("pending", "active"))
    name, _, member = await _names(sh)
    if sh.status == "revoked":
        await serverlog.add(sh.server_id, owner_tg, "share_remove", (member.first_name or "") if member else "")
    await ctx.notify(sh.member_tg, (f"ℹ️ Владелец отменил приглашение к серверу {html.escape(name)}." if sh.status == "cancelled"
                                    else f"🔒 Владелец закрыл вам доступ к серверу <b>{html.escape(name)}</b>. Сервер больше не отображается у вас."))


async def leave(ctx, server_id: int, member_tg: int) -> None:
    async with session() as s:
        sh = (await s.execute(select(ServerShare).where(ServerShare.server_id == server_id, ServerShare.member_tg == member_tg,
                                                        ServerShare.status == "active"))).scalars().first()
        if sh is None:
            raise UserError("У вас нет доступа к этому серверу.")
        sh.status, sh.finished_at = "left", utcnow()
        await s.commit()
    name, _, member = await _names(sh)
    await serverlog.add(server_id, member_tg, "share_leave")
    await ctx.notify(sh.owner_tg, f"👋 {_who(member)} вышел(а) из доступа к серверу {html.escape(name)}.")


async def revoke_all(ctx, server_id: int, reason: str) -> None:
    """Закрывает доступ всем (передача сервера) и сообщает участникам."""
    async with session() as s:
        rows = (await s.execute(select(ServerShare).where(ServerShare.server_id == server_id, ServerShare.status.in_(["pending", "active"])))).scalars().all()
        srv = await s.get(Server, server_id)
        notify = [r.member_tg for r in rows if r.status == "active"]
        await s.execute(update(ServerShare).where(ServerShare.server_id == server_id, ServerShare.status.in_(["pending", "active"]))
                        .values(status="revoked", finished_at=utcnow()))
        await s.commit()
    for tg in notify:
        await ctx.notify(tg, f"🔒 Доступ к серверу <b>{html.escape(srv.name if srv else '')}</b> закрыт: {reason}.")


async def members(server_id: int, owner_tg: int) -> list[dict]:
    """Кто имеет доступ или приглашён — для владельца."""
    now = utcnow()
    async with session() as s:
        rows = (await s.execute(select(ServerShare).where(ServerShare.server_id == server_id, ServerShare.owner_tg == owner_tg,
                                                          ServerShare.status.in_(["pending", "active"])).order_by(ServerShare.id))).scalars().all()
        out = []
        for r in rows:
            if r.status == "pending" and r.created_at <= now - TTL:
                continue
            out.append({"id": r.id, "status": r.status, "user": _user(await s.get(User, r.member_tg)),
                        "until": (r.created_at + TTL).isoformat() if r.status == "pending" else None})
    return out


async def shared_servers(member_tg: int) -> list[tuple[Server, User | None]]:
    """Серверы, к которым у клиента есть доступ (не его собственные)."""
    async with session() as s:
        rows = (await s.execute(select(Server, ServerShare).join(ServerShare, ServerShare.server_id == Server.id).where(
            ServerShare.member_tg == member_tg, ServerShare.status == "active", Server.status != "deleted",
            Server.tg_id == ServerShare.owner_tg).order_by(Server.id))).all()
        return [(srv, await s.get(User, srv.tg_id)) for srv, _ in rows]


async def incoming(member_tg: int) -> list[dict]:
    """Активные приглашения клиенту — для кабинета."""
    cutoff = utcnow() - TTL
    async with session() as s:
        rows = (await s.execute(select(ServerShare).where(ServerShare.member_tg == member_tg, ServerShare.status == "pending",
                                                          ServerShare.created_at > cutoff))).scalars().all()
        out = []
        for r in rows:
            srv = await s.get(Server, r.server_id)
            if srv is None or srv.status == "deleted" or srv.tg_id != r.owner_tg:
                continue
            out.append({"id": r.id, "server_id": srv.id, "server": srv.name, "flag": flag(srv.location), "cpu": srv.cpu, "ram": srv.ram, "disk": srv.disk,
                        "user": _user(await s.get(User, r.owner_tg)), "until": (r.created_at + TTL).isoformat()})
    return out
