"""Действия админа с серверами клиентов и сторож маржи.

Сторож маржи: Astra может поднять цену продления уже купленных серверов, а клиент продлевает
по цене на момент покупки. Себестоимость обновляется из ответов Astra; если цена клиента стала
ниже себестоимости, админ получает алерт и пересчитывает цену одной кнопкой — клиенту приходит
уведомление, новая цена действует со следующего продления.
"""
from __future__ import annotations

import html
import logging

from sqlalchemy import select, update

from .db import Server, User, session
from .services import UserError, cost_kop, flag, fmt_rub, sell_price_kop, user_price_cfg
from .settings import load_settings
from . import events, serverlog

log = logging.getLogger(__name__)
LIVE = ("active", "suspended")


def _who(u: User | None) -> str:
    return events.who(u)


async def loss_servers() -> list[dict]:
    """Серверы, у которых цена продления клиенту ниже себестоимости у Astra."""
    async with session() as s:
        rows = (await s.execute(select(Server).where(Server.status.in_(LIVE), Server.cost_kop > 0, Server.price_kop < Server.cost_kop)
                                .order_by(Server.id))).scalars().all()
        out = []
        for x in rows:
            u = await s.get(User, x.tg_id)
            out.append({"id": x.id, "astra_id": x.astra_id, "name": x.name, "flag": flag(x.location), "tg_id": x.tg_id,
                        "client": (u.first_name or (f"@{u.username}" if u.username else str(u.tg_id))) if u else str(x.tg_id),
                        "price_rub": x.price_kop / 100, "cost_rub": x.cost_kop / 100, "loss_rub": (x.cost_kop - x.price_kop) / 100,
                        "new_price_rub": (await _new_price(x)) / 100})
    return out


async def _new_price(srv: Server) -> int:
    async with session() as s:
        cfg = await load_settings(s)
        u = await s.get(User, srv.tg_id)
    return sell_price_kop(srv.cost_kop / 100, user_price_cfg(cfg, u))


async def margin_check(ctx) -> int:
    """Воркер: один алерт на каждое новое повышение себестоимости, из-за которого сервер стал убыточным."""
    async with session() as s:
        rows = (await s.execute(select(Server).where(Server.status.in_(LIVE), Server.cost_kop > 0, Server.price_kop < Server.cost_kop,
                                                     Server.margin_alert_cost != Server.cost_kop))).scalars().all()
        if not rows:
            return 0
        ids = [x.id for x in rows]
        lines = [f"• {flag(x.location)} {html.escape(x.name)} (#{x.astra_id}): клиент {fmt_rub(x.price_kop)}, Astra {fmt_rub(x.cost_kop)}" for x in rows[:15]]
        for x in rows:
            x.margin_alert_cost = x.cost_kop
        await s.commit()
    more = f"\n…и ещё {len(ids) - 15}" if len(ids) > 15 else ""
    from .config import env
    await events.emit(ctx, "alerts", "📉 <b>Продление в минус</b>\n\nAstra подняла цену продления — эти серверы клиенты продлевают дешевле, чем платите вы:\n\n"
                      + "\n".join(lines) + more + "\n\nПересчитайте цену: Админка → «Продление в минус».",
                      admin_url=env().public_url.rstrip("/") + "/admin")
    return len(ids)


async def reprice(ctx, server_id: int, admin_id: int) -> dict:
    async with session() as s:
        srv = await s.get(Server, server_id)
        if srv is None or srv.status not in LIVE:
            raise UserError("Сервер не найден.")
    new = await _new_price(srv)
    old = srv.price_kop
    if new == old:
        return {"price_rub": old / 100, "changed": False}
    async with session() as s:
        row = await s.get(Server, server_id)
        row.price_kop = new
        await s.commit()
    await serverlog.add(server_id, admin_id, "reprice", f"{fmt_rub(old)} → {fmt_rub(new)}")
    when = f" ({srv.expires_at:%d.%m.%Y})" if srv.expires_at else ""
    await ctx.notify(srv.tg_id, f"ℹ️ <b>Стоимость продления сервера {html.escape(srv.name)}</b>\n\n"
                                f"Поставщик изменил цены, поэтому со следующего продления{when} сервер будет стоить "
                                f"<b>{fmt_rub(new)}</b> в месяц вместо {fmt_rub(old)}. Оплаченный срок не меняется.")
    return {"price_rub": new / 100, "changed": True}


async def reprice_all(ctx, admin_id: int) -> int:
    n = 0
    for x in await loss_servers():
        if (await reprice(ctx, x["id"], admin_id))["changed"]:
            n += 1
    return n


# ───────────── действия с сервером клиента ─────────────

async def _get(server_id: int) -> Server:
    async with session() as s:
        srv = await s.get(Server, server_id)
    if srv is None or srv.status == "deleted":
        raise UserError("Сервер не найден.")
    return srv


async def renew_free(ctx, server_id: int, admin_id: int) -> dict:
    """Продление на 30 дней за счёт сервиса (компенсация): Astra продлевает, с клиента не списываем."""
    from .services import renew_server
    srv = await _get(server_id)
    srv = await renew_server(ctx, server_id, free=True, actor=admin_id)
    await ctx.notify(srv.tg_id, f"🎁 <b>Сервер {html.escape(srv.name)} продлён на 30 дней бесплатно</b>\n\nСпасибо, что вы с нами. Новая дата окончания — "
                                f"{srv.expires_at:%d.%m.%Y}." if srv.expires_at else "🎁 Сервер продлён на 30 дней бесплатно.")
    return {"expires_at": srv.expires_at.isoformat() if srv.expires_at else None}


async def move(ctx, server_id: int, to_query: str, admin_id: int) -> dict:
    """Перенос сервера другому клиенту без подтверждения (например, по просьбе в поддержке)."""
    from .transfers import find_recipient
    from .shares import revoke_all
    srv = await _get(server_id)
    to = await find_recipient(to_query)
    if to.tg_id == srv.tg_id:
        raise UserError("Сервер уже у этого клиента.")
    async with session() as s:
        await s.execute(update(Server).where(Server.id == server_id).values(tg_id=to.tg_id, notified_stage=0))
        await s.commit()
        a = await s.get(User, srv.tg_id)
    await revoke_all(ctx, server_id, "сервер передан другому владельцу")
    await serverlog.add(server_id, admin_id, "transfer", f"админ: → {to.first_name or to.tg_id}")
    await ctx.notify(srv.tg_id, f"🔁 Сервер <b>{html.escape(srv.name)}</b> передан другому клиенту по решению поддержки.")
    await ctx.notify(to.tg_id, f"🔁 <b>Вам передан сервер {html.escape(srv.name)}</b>\n\nОн появился в «Мои серверы». Рекомендуем сменить пароль root.")
    await events.emit(ctx, "sales", f"🔁 <b>Перенос сервера (админ)</b>\n\n🖥 {html.escape(srv.name)}\n👤 {_who(a)} → {_who(to)}")
    return {"tg_id": to.tg_id}


async def remove(ctx, server_id: int, refund: bool, admin_id: int) -> dict:
    from .services import cancel_server
    srv = await _get(server_id)
    kop = await cancel_server(ctx, srv.tg_id, server_id, refund=refund, actor=admin_id)
    await ctx.notify(srv.tg_id, f"❌ Сервер <b>{html.escape(srv.name)}</b> удалён поддержкой."
                                + (f" На баланс вернули {fmt_rub(kop)}." if kop else ""))
    return {"refund_rub": kop / 100}


async def action(ctx, server_id: int, act: str, admin_id: int) -> dict:
    from .services import server_action
    if act not in ("start", "stop", "reboot", "vnc"):
        raise UserError("Неизвестное действие.")
    srv = await _get(server_id)
    resp = await server_action(ctx, srv.tg_id, server_id, act, actor=admin_id)
    return {"ok": True, "link": resp.get("link") if isinstance(resp, dict) else None}
