"""Обращения в поддержку: переписка клиента с администратором и передача технических проблем провайдеру."""
from __future__ import annotations

import html
from urllib.parse import quote

from sqlalchemy import func, select

from .config import env
from .db import Server, Ticket, TicketMessage, User, session, utcnow
from .services import Ctx, UserError, flag, os_label
from .settings import fmt_rub, load_settings

STATUS = {"open": "Ждёт ответа", "answered": "Ответили", "provider": "У провайдера", "closed": "Решено"}
MAX_TEXT = 4000


def _clean(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise UserError("Напишите сообщение.")
    if len(text) > MAX_TEXT:
        raise UserError(f"Сообщение длиннее {MAX_TEXT} символов. Разбейте его на несколько.")
    return text


def _ticket_dict(t: Ticket, last: TicketMessage | None = None, for_admin: bool = False) -> dict:
    d = {"id": t.id, "subject": t.subject, "status": t.status, "status_text": STATUS.get(t.status, t.status),
         "server_id": t.server_id, "created": t.created_at.isoformat(), "updated": t.updated_at.isoformat(),
         "unread": t.unread_admin if for_admin else t.unread_client}
    if last is not None:
        d["last"] = {"author": last.author, "text": last.text[:140], "created": last.created_at.isoformat()}
    if for_admin:
        d["tg_id"] = t.tg_id
    return d


def _msg_dict(m: TicketMessage) -> dict:
    return {"id": m.id, "author": m.author, "text": m.text, "source": m.source, "created": m.created_at.isoformat()}


async def _last_messages(s, ids: list[int], include_notes: bool) -> dict[int, TicketMessage]:
    if not ids:
        return {}
    q = select(TicketMessage).where(TicketMessage.ticket_id.in_(ids))
    if not include_notes:
        q = q.where(TicketMessage.author.in_(["client", "admin", "system"]))
    out: dict[int, TicketMessage] = {}
    for m in (await s.execute(q.order_by(TicketMessage.id))).scalars():
        out[m.ticket_id] = m
    return out


def _admin_link(tid: int) -> str:
    return f"{env().public_url.rstrip('/')}/admin?ticket={tid}"


def _client_link(tid: int) -> str:
    return f"{env().public_url.rstrip('/')}/app?ticket={tid}"


async def _ping_admins(ctx: Ctx, text: str, tid: int) -> None:
    if ctx.notify_btn:
        for aid in env().admins:
            await ctx.notify_btn(aid, text, "Открыть обращение", _admin_link(tid))
    else:
        await ctx.alert_admin(text)


# ───────────────────────── клиент ─────────────────────────


async def create(ctx: Ctx, tg_id: int, subject: str, text: str, server_id: int | None = None, source: str = "app") -> Ticket:
    text = _clean(text)
    subject = " ".join((subject or "").split())[:120] or (text.split("\n")[0][:60] + ("…" if len(text) > 60 else ""))
    async with session() as s:
        if server_id is not None:
            srv = await s.get(Server, server_id)
            if srv is None or srv.tg_id != tg_id:
                raise UserError("Сервер не найден.")
        # защита от спама: не больше 5 открытых обращений
        n_open = (await s.execute(select(func.count()).select_from(Ticket).where(Ticket.tg_id == tg_id, Ticket.status != "closed"))).scalar_one()
        if n_open >= 5:
            raise UserError("У вас уже 5 открытых обращений. Дождитесь ответа или закройте решённые.")
        t = Ticket(tg_id=tg_id, subject=subject, server_id=server_id, status="open", unread_admin=True)
        s.add(t)
        await s.flush()
        s.add(TicketMessage(ticket_id=t.id, author="client", text=text, source=source))
        u = await s.get(User, tg_id)
        await s.commit()
    who = html.escape(u.first_name or (("@" + u.username) if u and u.username else str(tg_id))) if u else str(tg_id)
    await _ping_admins(ctx, f"🆕 Обращение #{t.id} от {who}\n<b>{html.escape(t.subject)}</b>\n\n{html.escape(text[:600])}", t.id)
    return t


async def list_client(tg_id: int) -> list[dict]:
    async with session() as s:
        rows = (await s.execute(select(Ticket).where(Ticket.tg_id == tg_id).order_by(Ticket.updated_at.desc()).limit(50))).scalars().all()
        last = await _last_messages(s, [t.id for t in rows], include_notes=False)
    return [_ticket_dict(t, last.get(t.id)) for t in rows]


async def get_client(tg_id: int, tid: int) -> dict:
    async with session() as s:
        t = await s.get(Ticket, tid)
        if t is None or t.tg_id != tg_id:
            raise UserError("Обращение не найдено.")
        msgs = (await s.execute(select(TicketMessage).where(TicketMessage.ticket_id == tid, TicketMessage.author != "note").order_by(TicketMessage.id))).scalars().all()
        if t.unread_client:
            t.unread_client = False
            await s.commit()
        srv = await s.get(Server, t.server_id) if t.server_id else None
    d = _ticket_dict(t)
    d["messages"] = [_msg_dict(m) for m in msgs]
    d["server"] = {"id": srv.id, "name": srv.name, "flag": flag(srv.location)} if srv else None
    return d


async def client_reply(ctx: Ctx, tg_id: int, tid: int, text: str, source: str = "app") -> None:
    text = _clean(text)
    async with session() as s:
        t = await s.get(Ticket, tid)
        if t is None or t.tg_id != tg_id:
            raise UserError("Обращение не найдено.")
        reopened = t.status == "closed"
        s.add(TicketMessage(ticket_id=tid, author="client", text=text, source=source))
        if t.status != "provider":
            t.status = "open"
        t.unread_admin, t.updated_at = True, utcnow()
        await s.commit()
        subject = t.subject
    head = "🔁 Клиент снова открыл" if reopened else "💬 Новое сообщение в"
    await _ping_admins(ctx, f"{head} обращении #{tid} «{html.escape(subject)}»\n\n{html.escape(text[:600])}", tid)


async def client_close(tg_id: int, tid: int) -> None:
    async with session() as s:
        t = await s.get(Ticket, tid)
        if t is None or t.tg_id != tg_id:
            raise UserError("Обращение не найдено.")
        t.status, t.updated_at = "closed", utcnow()
        s.add(TicketMessage(ticket_id=tid, author="system", text="Клиент закрыл обращение"))
        await s.commit()


async def latest_open(tg_id: int) -> Ticket | None:
    """Последнее незакрытое обращение — туда попадают сообщения, написанные боту в чат."""
    async with session() as s:
        return (await s.execute(select(Ticket).where(Ticket.tg_id == tg_id, Ticket.status != "closed").order_by(Ticket.updated_at.desc()))).scalars().first()


async def unread_for_client(tg_id: int) -> int:
    async with session() as s:
        return (await s.execute(select(func.count()).select_from(Ticket).where(Ticket.tg_id == tg_id, Ticket.unread_client.is_(True)))).scalar_one()


# ───────────────────────── администратор ─────────────────────────


async def list_admin(status: str = "") -> dict:
    async with session() as s:
        q = select(Ticket)
        if status == "active":
            q = q.where(Ticket.status.in_(["open", "provider"]))
        elif status:
            q = q.where(Ticket.status == status)
        rows = (await s.execute(q.order_by(Ticket.unread_admin.desc(), Ticket.updated_at.desc()).limit(100))).scalars().all()
        last = await _last_messages(s, [t.id for t in rows], include_notes=True)
        users = {u.tg_id: u for u in (await s.execute(select(User).where(User.tg_id.in_({t.tg_id for t in rows})))).scalars()} if rows else {}
        counts = dict((await s.execute(select(Ticket.status, func.count()).group_by(Ticket.status))).all())
        unread = (await s.execute(select(func.count()).select_from(Ticket).where(Ticket.unread_admin.is_(True)))).scalar_one()
    items = []
    for t in rows:
        d = _ticket_dict(t, last.get(t.id), for_admin=True)
        u = users.get(t.tg_id)
        d["client"] = {"tg_id": t.tg_id, "username": u.username if u else None, "first_name": u.first_name if u else None}
        items.append(d)
    return {"items": items, "counts": counts, "unread": unread}


async def get_admin(tid: int) -> dict:
    async with session() as s:
        t = await s.get(Ticket, tid)
        if t is None:
            raise UserError("Обращение не найдено.")
        msgs = (await s.execute(select(TicketMessage).where(TicketMessage.ticket_id == tid).order_by(TicketMessage.id))).scalars().all()
        u = await s.get(User, t.tg_id)
        servers = (await s.execute(select(Server).where(Server.tg_id == t.tg_id, Server.status != "deleted").order_by(Server.id))).scalars().all()
        n_tickets = (await s.execute(select(func.count()).select_from(Ticket).where(Ticket.tg_id == t.tg_id))).scalar_one()
        if t.unread_admin:
            t.unread_admin = False
            await s.commit()
        cfg = await load_settings(s)
    d = _ticket_dict(t, for_admin=True)
    d["messages"] = [_msg_dict(m) for m in msgs]
    d["provider_support"] = cfg["provider_support"]
    d["client"] = {
        "tg_id": t.tg_id, "username": u.username if u else None, "first_name": u.first_name if u else None,
        "balance_rub": (u.balance_kop / 100) if u else 0, "banned": bool(u and u.banned), "tickets": n_tickets,
        "since": u.created_at.isoformat() if u else None,
    }
    d["servers"] = [{"id": x.id, "astra_id": x.astra_id, "name": x.name, "status": x.status, "location": x.location, "flag": flag(x.location),
                     "ip": x.ip, "cpu": x.cpu, "ram": x.ram, "disk": x.disk, "os": os_label(x.os),
                     "expires_at": x.expires_at.isoformat() if x.expires_at else None} for x in servers]
    return d


async def admin_reply(ctx: Ctx, tid: int, text: str, internal: bool = False) -> None:
    text = _clean(text)
    async with session() as s:
        t = await s.get(Ticket, tid)
        if t is None:
            raise UserError("Обращение не найдено.")
        s.add(TicketMessage(ticket_id=tid, author="note" if internal else "admin", text=text))
        if not internal:
            t.status, t.unread_client = "answered", True
        t.updated_at = utcnow()
        await s.commit()
        tg_id, subject = t.tg_id, t.subject
        cfg = await load_settings(s)
    if internal:
        return
    msg = f"💬 <b>Ответ поддержки</b> · обращение #{tid}\n\n{html.escape(text)}\n\n<i>Ответить можно прямо здесь — просто напишите сообщение.</i>"
    if ctx.notify_btn:
        await ctx.notify_btn(tg_id, msg, "Открыть обращение", _client_link(tid))
    else:
        await ctx.notify(tg_id, msg)


async def set_status(ctx: Ctx, tid: int, status: str) -> None:
    if status not in STATUS:
        raise UserError("Неизвестный статус.")
    async with session() as s:
        t = await s.get(Ticket, tid)
        if t is None:
            raise UserError("Обращение не найдено.")
        t.status, t.updated_at = status, utcnow()
        if status == "closed":
            s.add(TicketMessage(ticket_id=tid, author="system", text="Обращение отмечено как решённое"))
            t.unread_client = True
        await s.commit()
        tg_id = t.tg_id
    if status == "closed":
        await ctx.notify(tg_id, f"✅ <b>Обращение #{tid} решено</b>\n\nЕсли вопрос остался — просто напишите сюда, продолжим.")


def escalation_text(ticket: dict, server: dict | None, client_msgs: list[str]) -> str:
    """Технический запрос провайдеру: только данные сервера и суть проблемы, без данных клиента."""
    lines = ["Здравствуйте! Пишу как реселлер, нужна помощь по серверу."]
    if server:
        lines += [
            "",
            f"ID сервера: {server['astra_id']}",
            f"IP: {server['ip'] or '—'}",
            f"Локация: {server['location'].upper()}",
            f"Конфигурация: {server['cpu']} vCPU, {server['ram']} ГБ RAM, {server['disk']} ГБ, {server['os']}",
            f"Статус у нас: {server['status']}",
        ]
    problem = "\n".join(m.strip() for m in client_msgs if m.strip())[-1500:]
    lines += ["", "Проблема:", problem or ticket["subject"]]
    return "\n".join(lines)


async def escalate(tid: int, server_id: int | None = None) -> dict:
    d = await get_admin(tid)
    servers = d["servers"]
    sid = server_id or d.get("server_id")
    server = next((x for x in servers if x["id"] == sid), None)
    if server is None and len(servers) == 1:
        server = servers[0]
    if server is None and servers:
        raise UserError("Выберите сервер, по которому вопрос.")
    client_msgs = [m["text"] for m in d["messages"] if m["author"] == "client"][-3:]
    text = escalation_text(d, server, client_msgs)
    support = (d["provider_support"] or "").lstrip("@")
    async with session() as s:
        t = await s.get(Ticket, tid)
        t.status, t.escalated_at, t.updated_at = "provider", utcnow(), utcnow()
        if server and not t.server_id:
            t.server_id = server["id"]
        s.add(TicketMessage(ticket_id=tid, author="note", text="Передано провайдеру (@" + support + "):\n\n" + text))
        await s.commit()
    return {"text": text, "support": support, "url": f"https://t.me/{support}?text={quote(text)}" if support else None}
