"""Журнал действий по серверу: покупка, питание, продление, переустановка, доступ, сбои связи.

Видят все, у кого есть доступ к серверу (владелец и участники общего доступа).
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select

from .db import ServerLog, User, session

log = logging.getLogger(__name__)

LABEL = {
    "purchase": "Сервер куплен",
    "start": "Включение",
    "stop": "Выключение",
    "reboot": "Перезагрузка",
    "vnc": "Открыта консоль VNC",
    "reinstall": "Переустановка системы",
    "password": "Сброс пароля",
    "change-ip": "Смена IP",
    "renew": "Продление на 30 дней",
    "autorenew": "Автопродление на 30 дней",
    "autorenew_on": "Автопродление включено",
    "autorenew_off": "Автопродление выключено",
    "rename": "Переименование",
    "upgrade": "Увеличение мощности",
    "share_invite": "Приглашение в общий доступ",
    "share_join": "Открыт общий доступ",
    "share_remove": "Доступ закрыт",
    "share_leave": "Выход из общего доступа",
    "transfer": "Сервер передан",
    "monitor_down": "Сервер перестал отвечать",
    "monitor_up": "Сервер снова отвечает",
    "monitor_on": "Мониторинг включён",
    "monitor_off": "Мониторинг выключен",
    "delete": "Сервер удалён",
    "renew_free": "Продление на 30 дней за счёт сервиса",
    "reprice": "Новая цена продления",
}
ICON = {"purchase": "🛒", "start": "▶️", "stop": "⏹", "reboot": "🔄", "vnc": "🖥", "reinstall": "⚙️", "password": "🛡", "change-ip": "🌍",
        "renew": "💳", "autorenew": "💳", "rename": "✏️", "upgrade": "⚡", "transfer": "🔁", "monitor_down": "🔴", "monitor_up": "🟢", "delete": "❌",
        "autorenew_on": "🔄", "autorenew_off": "🔄", "share_invite": "👥", "share_join": "👥", "share_remove": "🔒", "share_leave": "🚪",
        "monitor_on": "🔔", "monitor_off": "🔕", "renew_free": "🎁", "reprice": "💲"}


async def add(server_id: int, actor: int, action: str, detail: str = "") -> None:
    """Пишет событие. Ошибка журнала никогда не ломает основное действие."""
    try:
        async with session() as s:
            s.add(ServerLog(server_id=server_id, actor=int(actor or 0), action=action[:24], detail=(detail or "")[:200]))
            await s.commit()
    except Exception:
        log.warning("server log %s %s failed", server_id, action, exc_info=True)


async def recent(server_id: int, owner_tg: int, limit: int = 40) -> list[dict]:
    async with session() as s:
        # после передачи новому владельцу видна история только с момента передачи
        since = (await s.execute(select(func.max(ServerLog.id)).where(ServerLog.server_id == server_id, ServerLog.action == "transfer"))).scalar_one() or 0
        rows = (await s.execute(select(ServerLog).where(ServerLog.server_id == server_id, ServerLog.id >= since)
                                .order_by(ServerLog.id.desc()).limit(limit))).scalars().all()
        names: dict[int, User | None] = {}
        for r in rows:
            if r.actor and r.actor not in names:
                names[r.actor] = await s.get(User, r.actor)
    from .config import env
    admins = set(env().admins)
    out = []
    for r in rows:
        u = names.get(r.actor)
        who = "Система" if not r.actor else (u.first_name or (f"@{u.username}" if u and u.username else f"ID {r.actor}")) if u else f"ID {r.actor}"
        role = "system" if not r.actor else "owner" if r.actor == owner_tg else "other"
        if r.actor and r.actor != owner_tg and r.actor in admins:
            who, role = "Поддержка", "support"  # клиенту не показываем, кто именно из админов
        out.append({"action": r.action, "title": LABEL.get(r.action, r.action), "icon": ICON.get(r.action, "•"), "detail": r.detail,
                    "who": who, "role": role, "at": r.created_at.isoformat()})
    return out
