"""Фоновые задачи: синхронизация серверов, автопродление, платежи, чеки, баланс Astra."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from . import services as svc

log = logging.getLogger(__name__)


async def _loop(name: str, every_s: int, fn: Callable[[svc.Ctx], Awaitable[None]], ctx: svc.Ctx, first_delay: int = 5) -> None:
    await asyncio.sleep(first_delay)
    while True:
        try:
            await fn(ctx)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker %s failed", name)
        await asyncio.sleep(every_s)


async def _resume_broadcasts(ctx: svc.Ctx) -> None:
    from . import broadcasts

    await broadcasts.resume(ctx)


async def _health(ctx: svc.Ctx) -> None:
    from . import health

    await health.tick(ctx)


async def _security(ctx: svc.Ctx) -> None:
    from . import security

    await security.tick(ctx)


async def _locations(ctx: svc.Ctx) -> None:
    from . import locwatch

    await svc.watch_locations(ctx)
    await locwatch.notify(ctx)          # подписавшимся «сообщить, когда появится» — до общей рассылки
    await svc.announce_locations(ctx)


async def _margin(ctx: svc.Ctx) -> None:
    from . import admin_servers

    await admin_servers.margin_check(ctx)


async def _monitor(ctx: svc.Ctx) -> None:
    from . import monitor

    await monitor.tick(ctx)


async def _retention(ctx: svc.Ctx) -> None:
    from . import retention

    await retention.tick(ctx)


async def _backup(ctx: svc.Ctx) -> None:
    from . import backup

    await backup.scheduled(ctx)


def start_workers(ctx: svc.Ctx) -> list[asyncio.Task]:
    jobs = [
        ("sync_servers", 45, svc.sync_servers, 5),
        ("refund_undelivered", 6 * 3600, svc.refund_undelivered, 90),
        ("poll_payments", 60, svc.poll_pending_payments, 10),
        ("autorenew", 600, svc.autorenew_and_remind, 20),
        ("receipts", 300, svc.process_receipts, 30),
        ("astra_balance", 900, svc.check_astra_balance, 15),
        ("locations", 300, _locations, 25),
        ("broadcasts", 60, _resume_broadcasts, 8),
        ("backup", 600, _backup, 120),
        ("health", 30, _health, 40),
        ("security", 30, _security, 15),
        ("monitor", 60, _monitor, 90),
        ("margin", 1800, _margin, 240),
        ("retention", 600, _retention, 180),
    ]
    return [asyncio.create_task(_loop(n, t, f, ctx, d), name=n) for n, t, f, d in jobs]
