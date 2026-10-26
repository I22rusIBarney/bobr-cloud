"""Мониторинг доступности серверов клиентов.

Раз в минуту проверяем каждый работающий сервер снаружи: пробуем открыть TCP-соединение на порты
22, 80 и 443. Сервер считается живым, если хоть один порт ответил — принял соединение или отказал
(отказ означает, что машина в сети, просто порт закрыт). «Не отвечает» — только когда все порты
молчат до таймаута. Сообщаем после трёх неудачных проверок подряд (~3 минуты), предварительно
сверившись с Astra: если сервер выключен самим клиентом, это не сбой. После включения,
перезагрузки и переустановки мониторинг на несколько минут затихает.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import timedelta

from sqlalchemy import select

from .db import Server, ServerShare, session, utcnow
from .settings import load_settings
from . import serverlog

log = logging.getLogger(__name__)

PORTS = (22, 80, 443)
TIMEOUT = 4.0
FAILS_TO_ALERT = 3
PARALLEL = 25


async def _port(ip: str, port: int) -> bool:
    """True — хост ответил (соединение или отказ), False — тишина/недоступен."""
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), TIMEOUT)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True
    except ConnectionRefusedError:
        return True
    except (asyncio.TimeoutError, OSError):
        return False


async def probe(ip: str) -> bool:
    res = await asyncio.gather(*(_port(ip, p) for p in PORTS))
    return any(res)


def _dur(minutes: int) -> str:
    if minutes < 60:
        return f"{max(1, minutes)} мин"
    h, m = divmod(minutes, 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


async def _recipients(server_id: int, owner: int) -> list[int]:
    async with session() as s:
        rows = (await s.execute(select(ServerShare.member_tg).where(ServerShare.server_id == server_id, ServerShare.owner_tg == owner,
                                                                    ServerShare.status == "active"))).scalars().all()
    return [owner, *rows]


async def _send(ctx, tg_id: int, text: str, rows: list) -> None:
    fn = getattr(ctx, "notify_kb", None)
    if fn:
        await fn(tg_id, text, rows)
    else:
        await ctx.notify(tg_id, text)


async def tick(ctx, probe_fn=None) -> None:
    async with session() as s:
        cfg = await load_settings(s)
    if not cfg.get("monitor_enabled", True):
        return
    probe_fn = probe_fn or probe
    now = utcnow()
    async with session() as s:
        rows = (await s.execute(select(Server).where(Server.status == "active", Server.monitor.is_(True), Server.ip.is_not(None)))).scalars().all()
    rows = [r for r in rows if not (r.mon_quiet_until and r.mon_quiet_until > now)]
    sem = asyncio.Semaphore(PARALLEL)

    async def one(srv: Server) -> tuple[Server, bool]:
        async with sem:
            try:
                return srv, await probe_fn(srv.ip)
            except Exception:
                log.warning("probe %s failed", srv.ip, exc_info=True)
                return srv, True  # ошибка проверки — не повод будить клиента

    for srv, up in await asyncio.gather(*(one(r) for r in rows)):
        if up:
            if srv.mon_down_at:
                await _recovered(ctx, srv)
            elif srv.mon_fail:
                await _set(srv.id, mon_fail=0)
            continue
        fails = srv.mon_fail + 1
        if fails < FAILS_TO_ALERT or srv.mon_down_at:
            await _set(srv.id, mon_fail=fails)
            continue
        # сервер выключен владельцем — это не сбой
        try:
            r = await ctx.astra.server(srv.astra_id)
            if str(r.get("power_status", "")).lower() in ("stopped", "off", "shutoff", "poweroff"):
                await _set(srv.id, mon_fail=0, mon_quiet_until=now + timedelta(minutes=30))
                continue
        except Exception:
            pass
        await _down(ctx, srv)


async def _set(sid: int, **vals) -> None:
    async with session() as s:
        row = await s.get(Server, sid)
        if row:
            for k, v in vals.items():
                setattr(row, k, v)
            await s.commit()


async def _down(ctx, srv: Server) -> None:
    await _set(srv.id, mon_fail=FAILS_TO_ALERT, mon_down_at=utcnow())
    await serverlog.add(srv.id, 0, "monitor_down")
    text = (f"🔴 <b>Сервер {html.escape(srv.name)} не отвечает</b>\n\nIP <code>{html.escape(srv.ip or '')}</code> — не отвечает уже несколько минут.\n\n"
            "Попробуйте перезагрузить его. Если не поможет — напишите в поддержку, разберёмся.")
    rows = [[("🔄 Перезагрузить", f"s:a:{srv.id}:reboot"), ("💬 В поддержку", "m:support")]]
    for tg in await _recipients(srv.id, srv.tg_id):
        own = tg == srv.tg_id  # выключить слежение может только владелец
        await _send(ctx, tg, text, rows + ([[("🔕 Не следить за этим сервером", f"mon:off:{srv.id}")]] if own else []))


async def _recovered(ctx, srv: Server) -> None:
    minutes = int((utcnow() - srv.mon_down_at).total_seconds() // 60)
    await _set(srv.id, mon_fail=0, mon_down_at=None)
    await serverlog.add(srv.id, 0, "monitor_up", f"не отвечал {_dur(minutes)}")
    for tg in await _recipients(srv.id, srv.tg_id):
        await ctx.notify(tg, f"🟢 <b>Сервер {html.escape(srv.name)} снова отвечает</b>\n\nНе отвечал {_dur(minutes)}.")


async def set_monitor(tg_id: int, server_id: int, on: bool) -> bool:
    from .services import UserError

    async with session() as s:
        srv = await s.get(Server, server_id)
        if srv is None or srv.tg_id != tg_id or srv.status == "deleted":
            raise UserError("Сервер не найден.")
        srv.monitor = bool(on)
        if not on:
            srv.mon_fail, srv.mon_down_at = 0, None
        await s.commit()
    await serverlog.add(server_id, tg_id, "monitor_on" if on else "monitor_off")
    return bool(on)
