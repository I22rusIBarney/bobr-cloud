"""События безопасности сервера: входы по SSH и блокировки fail2ban.

bcloud harden ставит на сервер маленький хук: он дописывает события в data/host-events.jsonl
(папка data общая у сервера и контейнера бота). Бот читает файл каждые 30 секунд:
о каждом входе по SSH сообщает сразу, о блокировках — сводкой, а при массовом подборе
пароля — отдельным предупреждением.
"""
from __future__ import annotations

import html
import json
import logging
import os
import time
from datetime import datetime, timezone

from .db import Setting, session
from . import events

log = logging.getLogger(__name__)

ATTACK_BANS, ATTACK_WINDOW = 20, 600   # 20+ блокировок за 10 минут — похоже на атаку
_bans: list[float] = []
_logins: list[dict] = []
_attack_alert_ts = 0.0


def _dir() -> str:
    from .health import _data_dir
    return _data_dir()


def events_file() -> str:
    return os.path.join(_dir(), "host-events.jsonl")


def hardened() -> dict | None:
    try:
        with open(os.path.join(_dir(), ".hardened")) as f:
            return json.load(f)
    except Exception:
        return None


def _read_and_clear() -> list[dict]:
    path = events_file()
    if not os.path.exists(path):
        return []
    with open(path, "r+", encoding="utf-8", errors="replace") as f:
        data = f.read()
        f.seek(0)
        f.truncate()
    out = []
    for line in data.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


async def _known_ips() -> list[str]:
    async with session() as s:
        row = await s.get(Setting, "sec_known_ips")
    try:
        return json.loads(row.value) if row else []
    except ValueError:
        return []


async def _remember_ip(ip: str) -> None:
    ips = [x for x in await _known_ips() if x != ip][-49:] + [ip]
    async with session() as s:
        row = await s.get(Setting, "sec_known_ips")
        if row is None:
            s.add(Setting(key="sec_known_ips", value=json.dumps(ips)))
        else:
            row.value = json.dumps(ips)
        await s.commit()


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m %H:%M UTC")


async def tick(ctx) -> None:
    global _attack_alert_ts
    try:
        evs = _read_and_clear()
    except Exception:
        log.exception("host events")
        return
    now = time.time()
    for ev in evs:
        ts = float(ev.get("ts") or now)
        if ev.get("type") == "login":
            user, ip = str(ev.get("user", "?"))[:32], str(ev.get("ip", "?"))[:64]
            known = ip in await _known_ips()
            _logins.append({"user": user, "ip": ip, "ts": ts, "new": not known})
            del _logins[:-20]
            await events.emit(ctx, "security",
                              f"🔐 <b>Вход на сервер по SSH</b>" + ("" if known else " · <b>новый IP</b>") +
                              f"\n\n👤 {html.escape(user)}\n🌐 <code>{html.escape(ip)}</code>\n🕐 {_when(ts)}"
                              + ("" if known else "\n\nЕсли это были не вы — срочно смените пароль и ключи SSH."))
            if not known:
                await _remember_ip(ip)
        elif ev.get("type") == "ban":
            _bans.append(ts)
    cutoff = now - 24 * 3600
    _bans[:] = [t for t in _bans if t > cutoff]
    recent = sum(1 for t in _bans if t > now - ATTACK_WINDOW)
    if recent >= ATTACK_BANS and now - _attack_alert_ts > 3600:
        _attack_alert_ts = now
        await events.emit(ctx, "security", f"⚠️ <b>Подбор паролей к серверу</b>\n\nЗа 10 минут fail2ban заблокировал {recent} IP. "
                          "Защита работает, но если атака не стихает — проверьте, что вход по паролю отключён (bcloud security).")


async def daily_line() -> str:
    h = hardened()
    if not h:
        return "🛡 Защита сервера не настроена — выполните на сервере bcloud harden"
    now = time.time()
    bans = sum(1 for t in _bans if t > now - 24 * 3600)
    logins = sum(1 for x in _logins if x["ts"] > now - 24 * 3600)
    return f"🛡 За сутки: заблокировано IP — {bans}, входов по SSH — {logins}"


def info() -> dict:
    now = time.time()
    return {"hardened": hardened(), "bans_24h": sum(1 for t in _bans if t > now - 24 * 3600),
            "logins": list(reversed(_logins))[:10]}
