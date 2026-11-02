"""Журнал действий админов.

Всё, что меняет данные из админки, записывается само: ASGI-прослойка смотрит на успешные изменяющие запросы
к /api/admin/… (см. ROUTES) и пишет, кто, когда и что сделал. Пароли, ключи и токены в подробности не попадают.
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import or_, select

from .config import env
from .db import Audit, session
from .web.auth import validate_init_data

log = logging.getLogger(__name__)

# (метод, путь после /api/admin/) → (код действия, как назвать). Не пишем: проверки, тесты, чтение.
ROUTES: list[tuple[str, str, str, str]] = [
    ("POST", r"settings", "settings", "Изменил настройки"),
    ("PUT", r"theme/background", "theme", "Загрузил фон кабинета"),
    ("DELETE", r"theme/background", "theme", "Убрал фон кабинета"),
    ("POST", r"integrations/(?P<name>yookassa|platega|nalog)", "integration", "Подключил {name}"),
    ("DELETE", r"integrations/(?P<name>yookassa|platega|nalog)", "integration", "Отключил {name}"),
    ("POST", r"users/(?P<tg>\d+)/balance", "user.balance", "Изменил баланс клиента"),
    ("POST", r"users/(?P<tg>\d+)/ban", "user.ban", "Заблокировал или разблокировал клиента"),
    ("POST", r"users/(?P<tg>\d+)/autopay", "user.autopay", "Изменил автоплатёж клиента"),
    ("POST", r"users/(?P<tg>\d+)/markup", "user.markup", "Изменил наценку клиента"),
    ("DELETE", r"users/(?P<tg>\d+)/methods/(?P<mid>\d+)", "user.method", "Удалил способ оплаты клиента"),
    ("POST", r"servers/(?P<sid>\d+)/(?P<act>[a-z_-]+)", "server", "Действие с сервером: {act}"),
    ("POST", r"broadcasts", "broadcast", "Запустил рассылку"),
    ("POST", r"broadcasts/(?P<bid>\d+)/cancel", "broadcast", "Остановил рассылку"),
    ("POST", r"tickets/(?P<tid>\d+)/messages", "ticket", "Ответил в обращении"),
    ("POST", r"tickets/(?P<tid>\d+)/status", "ticket", "Изменил статус обращения"),
    ("POST", r"tickets/(?P<tid>\d+)/escalate", "ticket", "Передал обращение провайдеру"),
    ("POST", r"promos", "promo", "Создал промокод"),
    ("PATCH", r"promos/(?P<pid>\d+)", "promo", "Включил или выключил промокод"),
    ("DELETE", r"promos/(?P<pid>\d+)", "promo", "Удалил промокод"),
    ("POST", r"docs/publish", "legal", "Опубликовал документы"),
    ("POST", r"margin/reprice-all", "margin", "Пересчитал цены продлений"),
    ("POST", r"payments/(?P<pid>\d+)/retry-receipt", "receipt", "Повторил чек"),
    ("POST", r"backup", "backup", "Сделал резервную копию"),
    ("POST", r"backup/key", "backup.key", "Сменил ключ резервных копий"),
    ("PUT", r"backup/restore", "backup.restore", "Восстановил базу из копии"),
    ("POST", r"update", "update.cfg", "Изменил автообновление"),
    ("POST", r"update/now", "update", "Обновил бота"),
    ("POST", r"update/night", "update", "Запланировал обновление на ночь"),
]
_ROUTES = [(m, re.compile(p + r"/?$"), a, lbl) for m, p, a, lbl in ROUTES]

SECRET = re.compile(r"pass|secret|token|key|cookie|init|cred|shop_id|merchant|inn", re.I)
SHOW = {
    "delta_rub": "сумма ₽", "banned": "заблокирован", "enabled": "включено", "mode": "режим", "pct": "%", "no_markup": "без наценки",
    "to": "куда", "refund": "с возвратом", "charge": "за счёт клиента", "code": "код", "kind": "вид", "value": "значение",
    "max_uses": "использований", "days": "дней", "active": "активен", "status": "статус", "audience": "кому", "text": "текст",
    "internal": "внутренняя заметка", "server_id": "сервер", "on": "включено", "hour": "час",
}
_NAMES = {"yookassa": "ЮKassa", "platega": "Platega", "nalog": "«Мой налог»"}


def match(method: str, path: str) -> tuple[str, str, dict] | None:
    for m, rx, action, label in _ROUTES:
        if m == method:
            g = rx.fullmatch(path)
            if g:
                d = g.groupdict()
                return action, label.format(**{k: _NAMES.get(v, v) for k, v in d.items()}), d
    return None


def _short(v) -> str:
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, (list, tuple)):
        return ", ".join(_short(x) for x in list(v)[:6]) + (f" и ещё {len(v) - 6}" if len(v) > 6 else "")
    if isinstance(v, dict):
        return "…"
    return str(v).replace("\n", " ")[:80]


def describe(body) -> str:
    """Понятные поля тела запроса без секретов. У настроек — какие ключи и на что поменяли."""
    if not isinstance(body, dict):
        return ""
    parts = []
    if isinstance(body.get("values"), dict):  # настройки
        for k, v in list(body["values"].items())[:12]:
            parts.append(f"{k}: " + ("•••" if SECRET.search(k) else _short(v)))
    for k, v in body.items():
        if k == "values" or SECRET.search(k) or v in (None, "", [], {}) or k not in SHOW:
            continue
        parts.append(f"{SHOW[k]}: {_short(v)}")
    return "; ".join(parts)[:500]


def who_of(user: dict) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip()
    return name or (("@" + user["username"]) if user.get("username") else str(user.get("id", "")))


async def log_action(uid: int, who: str, action: str, label: str, detail: str = "") -> None:
    """Записать действие. Журнал — не главное: его ошибка не должна ломать само действие."""
    try:
        async with session() as s:
            s.add(Audit(uid=uid, who=who[:80], action=action[:40], label=label[:120], detail=detail[:2000]))
            await s.commit()
    except Exception:  # noqa: BLE001
        log.exception("audit")


async def from_request(method: str, path: str, raw: bytes, init_data: str) -> None:
    hit = match(method, path)
    if not hit:
        return
    action, label, g = hit
    user = validate_init_data(init_data, env().bot_token) or {}
    try:
        body = json.loads(raw or b"null")
    except (ValueError, UnicodeDecodeError):
        body = None
    extra = [f"{what} {g[k]}" for k, what in (("tg", "клиент"), ("sid", "сервер"), ("tid", "обращение"), ("pid", "№"),
                                               ("bid", "рассылка"), ("mid", "способ")) if g.get(k)]
    detail = "; ".join(x for x in [", ".join(extra), describe(body)] if x)
    await log_action(int(user.get("id") or 0), who_of(user) if user else "", action, label, detail)


class AuditMiddleware:
    """ASGI: тело запроса копится по ходу чтения (обработчик получает его как обычно), запись — после ответа."""
    LIMIT = 64 * 1024

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS") or not scope["path"].startswith("/api/admin/"):
            return await self.app(scope, receive, send)
        path = scope["path"][len("/api/admin/"):]
        if not match(scope["method"], path):
            return await self.app(scope, receive, send)
        chunks: list[bytes] = []
        size = [0]
        code = [500]

        async def rcv():
            m = await receive()
            if m.get("type") == "http.request" and size[0] < self.LIMIT:
                b = m.get("body", b"")
                chunks.append(b)
                size[0] += len(b)
            return m

        async def snd(m):
            if m.get("type") == "http.response.start":
                code[0] = m.get("status", 500)
            await send(m)

        await self.app(scope, rcv, snd)
        if code[0] < 400:
            headers = {k.decode().lower(): v.decode(errors="replace") for k, v in scope.get("headers", [])}
            ctype = headers.get("content-type", "")
            raw = b"".join(chunks) if (size[0] < self.LIMIT and "json" in ctype) else b""
            await from_request(scope["method"], path, raw, headers.get("x-init-data", ""))


async def view(q: str = "", before: int | None = None, limit: int = 50) -> list[dict]:
    async with session() as s:
        st = select(Audit).order_by(Audit.id.desc()).limit(limit)
        if before:
            st = st.where(Audit.id < before)
        if q:
            like = f"%{q.strip()}%"
            st = st.where(or_(Audit.label.ilike(like), Audit.detail.ilike(like), Audit.who.ilike(like), Audit.action.ilike(like)))
        rows = (await s.execute(st)).scalars().all()
    return [{"id": r.id, "ts": r.created_at.isoformat() if r.created_at else "", "uid": r.uid, "who": r.who or ("Бот" if not r.uid else str(r.uid)),
             "action": r.action, "label": r.label, "detail": r.detail} for r in rows]
