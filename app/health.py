"""Мониторинг: регулярные проверки сервисов, от которых зависит бот.

Сообщение приходит, когда что-то сломалось и когда починилось, плюс сводка раз в сутки
(в чат события «Мониторинг»). Проверки, которые не настроены (например, «Мой налог»),
пропускаются и не считаются сбоем.
"""
from __future__ import annotations

import asyncio
import html
import logging
import shutil
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from .config import env
from .db import Setting, session
from .settings import load_settings
from . import events

log = logging.getLogger(__name__)

TIMEOUT = 15
NALOG_EVERY = 1800  # «Мой налог» не любит частые входы — проверяем раз в 30 минут
DISK_MIN_FREE_PCT, DISK_MIN_FREE_GB = 10, 1.0
MEM_MIN_AVAIL_PCT = 5
CONFIRM_DELAY = 20  # перепроверка перед сообщением о сбое

_state: dict[str, dict] = {}   # name → последний результат
_last_run = 0.0
_nalog_cache: tuple[float, dict] | None = None
_lock = asyncio.Lock()


def _res(ok: bool | None, ms: float | None = None, detail: str = "") -> dict:
    return {"ok": ok, "ms": None if ms is None else int(ms), "detail": detail}


async def _timed(coro) -> tuple[float, object]:
    t = time.perf_counter()
    r = await asyncio.wait_for(coro, TIMEOUT)
    return (time.perf_counter() - t) * 1000, r


async def _check_db(ctx) -> dict:
    async def q():
        async with session() as s:
            await s.execute(text("SELECT 1"))
    ms, _ = await _timed(q())
    return _res(True, ms)


async def _check_telegram(ctx) -> dict:
    if not getattr(ctx, "tg_ping", None):
        return _res(None, detail="не запущен")
    ms, _ = await _timed(ctx.tg_ping())
    return _res(True, ms)


async def _check_astra(ctx) -> dict:
    ms, b = await _timed(ctx.astra.balance())
    async with session() as s:
        cfg = await load_settings(s)
    bal = float((b or {}).get("balance_rub", 0))
    if bal < cfg["low_balance_rub"]:
        return _res(False, ms, f"баланс {bal:g} ₽ ниже порога")
    return _res(True, ms, f"баланс {bal:g} ₽")


async def _check_yookassa(ctx) -> dict:
    if not ctx.yk.configured:
        return _res(None, detail="не настроена")
    ms, (st, _) = await _timed(ctx.yk.check())
    return _res(st == 200, ms, "" if st == 200 else f"ответ {st} — проверьте ключи")


async def _check_nalog(ctx) -> dict:
    global _nalog_cache
    if not ctx.nalog.enabled:
        return _res(None, detail="не настроен")
    if _nalog_cache and time.time() - _nalog_cache[0] < NALOG_EVERY:
        return _nalog_cache[1]
    ms, _ = await _timed(ctx.nalog.check())
    r = _res(True, ms)
    _nalog_cache = (time.time(), r)
    return r


async def _check_https(ctx) -> dict:
    url = env().public_url.rstrip("/")
    if not url.startswith("https://"):
        return _res(None, detail="нет HTTPS-адреса")
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as c:
        ms, r = await _timed(c.get(url + "/paid"))
    return _res(r.status_code == 200, ms, "" if r.status_code == 200 else f"ответ {r.status_code}")


def _data_dir() -> str:
    url = env().database_url
    if url.startswith("sqlite") and ":///" in url:
        import os
        return os.path.dirname(url.split(":///", 1)[1]) or "."
    return "."


async def _check_disk(ctx) -> dict:
    u = shutil.disk_usage(_data_dir())
    free_pct, free_gb = u.free / u.total * 100, u.free / 1024 ** 3
    ok = free_pct >= DISK_MIN_FREE_PCT and free_gb >= DISK_MIN_FREE_GB
    return _res(ok, None, f"свободно {free_gb:.1f} ГБ ({free_pct:.0f}%)")


async def _check_memory(ctx) -> dict:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        avail = info["MemAvailable"] / info["MemTotal"] * 100
    except Exception:
        return _res(None, detail="нет данных")
    return _res(avail >= MEM_MIN_AVAIL_PCT, None, f"свободно {info['MemAvailable'] // 1024} МБ ({avail:.0f}%)")


CHECKS = {
    "astra": ("Astra API", _check_astra),
    "yookassa": ("ЮKassa", _check_yookassa),
    "moynalog": ("Мой налог", _check_nalog),
    "telegram": ("Telegram Bot API", _check_telegram),
    "database": ("База данных", _check_db),
    "https": ("HTTPS-сайт бота", _check_https),
    "disk": ("Диск", _check_disk),
    "memory": ("Память", _check_memory),
}


async def run_checks(ctx) -> dict[str, dict]:
    async def one(key, fn):
        try:
            return key, await fn(ctx)
        except asyncio.TimeoutError:
            return key, _res(False, None, f"нет ответа {TIMEOUT} с")
        except Exception as ex:
            return key, _res(False, None, (str(ex) or ex.__class__.__name__)[:200])
    return dict(await asyncio.gather(*(one(k, fn) for k, (_, fn) in CHECKS.items())))


def _line(key: str, r: dict, bold: bool = False) -> str:
    name = CHECKS[key][0]
    mark = "✅" if r["ok"] else "❌" if r["ok"] is False else "⚪️"
    ms = f" ({r['ms']}ms)" if r.get("ms") is not None else ""
    det = f" — {html.escape(r['detail'])}" if r.get("detail") else ""
    n = f"<b>{name}</b>" if bold else name
    return f"{mark} {n}{ms}{det}"


def summary_text(res: dict[str, dict], issues: list[tuple[str, str]] | None = None, extra: str = "") -> str:
    active = {k: r for k, r in res.items() if r["ok"] is not None}
    up = sum(1 for r in active.values() if r["ok"])
    head = "✅" if up == len(active) else "⚠️"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    out = [f"{head} <b>Healthcheck</b> — {up}/{len(active)} up", f"🕐 <i>{now} UTC</i>"]
    if issues:
        out += ["", "<b>Изменения:</b>"]
        for key, what in issues:
            r = res[key]
            if what == "down":
                out.append(_line(key, r, bold=True))
            else:
                out.append(f"✅ <b>{CHECKS[key][0]}</b> — восстановлено" + (f" ({r['ms']}ms)" if r.get("ms") is not None else ""))
    out.append("")
    out += [_line(k, r) for k, r in res.items()]
    if extra:
        out += ["", extra]
    return "\n".join(out)


async def tick(ctx) -> None:
    """Воркер: проверки по расписанию, сообщения о сбоях и восстановлении, сводка раз в сутки."""
    global _last_run
    async with session() as s:
        cfg = await load_settings(s)
    if not cfg.get("health_enabled", True):
        return
    every = max(1, int(cfg.get("health_interval_min", 5))) * 60
    if time.time() - _last_run < every - 5:
        return
    async with _lock:
        _last_run = time.time()
        res = await run_checks(ctx)
        issues = []
        for k, r in res.items():
            prev = _state.get(k)
            if r["ok"] is False and (prev is None or prev["ok"] is not False):
                # первый сбой после запуска тоже сообщаем; один таймаут — повод перепроверить, а не будить
                issues.append((k, "down"))
            elif r["ok"] is True and prev is not None and prev["ok"] is False:
                issues.append((k, "up"))
        # подтверждаем падение повторной проверкой, чтобы не шуметь из-за разовых сбоев сети
        confirm = [k for k, w in issues if w == "down"]
        if confirm:
            await asyncio.sleep(CONFIRM_DELAY)
            again = await run_checks(ctx)
            for k in confirm:
                if again[k]["ok"] is not False:
                    issues.remove((k, "down"))
                    res[k] = again[k]
        _state.clear()
        _state.update(res)
    if issues:
        await events.emit(ctx, "health", summary_text(res, issues))
    await _daily(ctx, res, cfg)


async def _daily(ctx, res: dict, cfg: dict) -> None:
    if not cfg.get("health_daily", True):
        return
    async with session() as s:
        row = await s.get(Setting, "health_daily_ts")
        last = int(row.value) if row and row.value.isdigit() else 0
        if time.time() - last < 24 * 3600 - 120:
            return
        val = str(int(time.time()))
        if row is None:
            s.add(Setting(key="health_daily_ts", value=val))
        else:
            row.value = val
        await s.commit()
    from . import security
    await events.emit(ctx, "health", summary_text(res, extra=await security.daily_line()))


def current() -> dict:
    return {"checks": [{"key": k, "title": CHECKS[k][0], **r} for k, r in _state.items()], "checked_at": int(_last_run) or None}


async def check_now(ctx) -> dict:
    global _last_run
    async with _lock:
        res = await run_checks(ctx)
        _state.clear()
        _state.update(res)
        _last_run = time.time()
    return current()
