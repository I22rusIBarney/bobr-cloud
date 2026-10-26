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


async def _backup(ctx: svc.Ctx) -> None:
    from . import backup

    await backup.scheduled(ctx)


def start_workers(ctx: svc.Ctx) -> list[asyncio.Task]:
    jobs = [
        ("sync_servers", 45, svc.sync_servers, 5),
        ("poll_payments", 60, svc.poll_pending_payments, 10),
        ("autorenew", 600, svc.autorenew_and_remind, 20),
        ("receipts", 300, svc.process_receipts, 30),
        ("astra_balance", 900, svc.check_astra_balance, 15),
        ("locations", 600, svc.watch_locations, 25),
        ("broadcasts", 60, _resume_broadcasts, 8),
        ("backup", 600, _backup, 120),
        ("health", 30, _health, 40),
        ("security", 30, _security, 15),
    ]
    return [asyncio.create_task(_loop(n, t, f, ctx, d), name=n) for n, t, f, d in jobs]
