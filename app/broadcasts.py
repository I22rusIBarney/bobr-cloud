"""Массовые рассылки клиентам из админки.

Отправка идёт в фоне ~20 сообщений в секунду (лимит Telegram — около 30), прогресс сохраняется:
после перезапуска рассылка продолжится с того же места. Кто заблокировал бота — помечается и
пропускается в следующих рассылках, пока снова не напишет боту. Рассылки типа «Новости»
не уходят тем, кто отписался, и содержат кнопку «Отписаться».
"""
from __future__ import annotations

import asyncio
import logging
import re
from html.parser import HTMLParser

from sqlalchemy import func, select, update

from .db import Broadcast, Server, User, session, utcnow

log = logging.getLogger(__name__)

AUDIENCES = {"all": "Все клиенты", "active": "С активными серверами", "inactive": "Без активных серверов"}
KINDS = ("news", "service")
RATE = 20          # сообщений в секунду
CHUNK = 25         # как часто сохранять прогресс и проверять отмену
_running: set[int] = set()

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "tg-spoiler", "blockquote"}


class BroadcastError(Exception):
    pass


class _Check(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.err: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.err = self.err or f"тег <{tag}> не поддерживается Telegram"
        if tag == "a" and not any(k == "href" and v for k, v in attrs):
            self.err = self.err or "у ссылки <a> нет href"
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.err = self.err or f"лишний или неверно закрытый тег </{tag}>"
        else:
            self.stack.pop()


def validate(text: str, btn_text: str = "", btn_url: str = "") -> str:
    """Проверяет разметку Telegram HTML и кнопку. Возвращает очищенный текст."""
    text = (text or "").strip()
    if not text:
        raise BroadcastError("Напишите текст рассылки.")
    if len(text) > 3500:
        raise BroadcastError("Слишком длинный текст: максимум 3500 символов.")
    if re.search(r"<(?![/a-zA-Z])", text):
        raise BroadcastError("Знак «<» в тексте нужно писать как &lt; — иначе Telegram примет его за тег.")
    p = _Check()
    p.feed(text)
    p.close()
    if p.err or p.stack:
        raise BroadcastError("Ошибка разметки: " + (p.err or f"не закрыт тег <{p.stack[-1]}>"))
    if bool(btn_text.strip()) != bool(btn_url.strip()):
        raise BroadcastError("Для кнопки нужны и текст, и ссылка.")
    if btn_url and not re.match(r"^(https://|tg://)", btn_url.strip()):
        raise BroadcastError("Ссылка кнопки должна начинаться с https:// или tg://")
    if len(btn_text) > 64:
        raise BroadcastError("Текст кнопки — до 64 символов.")
    return text


def _audience_query(audience: str, kind: str):
    active_ids = select(Server.tg_id).where(Server.status.in_(["active", "suspended", "provisioning"]))
    q = select(User.tg_id).where(User.banned.is_(False), User.bot_blocked.is_(False), User.consent_version > 0)
    if kind == "news":
        q = q.where(User.news_off.is_(False))
    if audience == "active":
        q = q.where(User.tg_id.in_(active_ids))
    elif audience == "inactive":
        q = q.where(User.tg_id.not_in(active_ids))
    return q


async def audience_count(audience: str, kind: str) -> int:
    if audience not in AUDIENCES or kind not in KINDS:
        raise BroadcastError("Неизвестная аудитория.")
    async with session() as s:
        return (await s.execute(select(func.count()).select_from(_audience_query(audience, kind).subquery()))).scalar_one()


def buttons(b: Broadcast | dict) -> list:
    """Кнопки сообщения: [(text, url|None, callback|None)]."""
    get = (lambda k: b.get(k)) if isinstance(b, dict) else (lambda k: getattr(b, k))
    out = []
    if get("btn_text") and get("btn_url"):
        out.append((get("btn_text"), get("btn_url"), None))
    if get("kind") == "news":
        out.append(("🔕 Отписаться от новостей", None, "nw:off"))
    return out


async def create(ctx, admin_id: int, text: str, audience: str, kind: str, btn_text: str = "", btn_url: str = "") -> Broadcast:
    text = validate(text, btn_text, btn_url)
    if audience not in AUDIENCES or kind not in KINDS:
        raise BroadcastError("Неизвестная аудитория.")
    async with session() as s:
        if (await s.execute(select(func.count()).select_from(Broadcast).where(Broadcast.status == "sending"))).scalar_one():
            raise BroadcastError("Уже идёт другая рассылка — дождитесь окончания или остановите её.")
    total = await audience_count(audience, kind)
    if total == 0:
        raise BroadcastError("В этой аудитории никого нет.")
    async with session() as s:
        b = Broadcast(text=text, btn_text=btn_text.strip(), btn_url=btn_url.strip(), audience=audience, kind=kind,
                      status="sending", total=total, created_by=admin_id)
        s.add(b)
        await s.commit()
        await s.refresh(b)
    start(ctx, b.id)
    return b


async def send_test(ctx, admin_id: int, text: str, kind: str, btn_text: str = "", btn_url: str = "") -> str:
    text = validate(text, btn_text, btn_url)
    if ctx.send_broadcast is None:
        raise BroadcastError("Бот ещё не запущен.")
    res = await ctx.send_broadcast(admin_id, text, buttons({"btn_text": btn_text, "btn_url": btn_url, "kind": kind}))
    if res != "ok":
        raise BroadcastError("Telegram не принял сообщение — проверьте разметку и ссылку кнопки." if res == "error" else "Не удалось отправить: напишите боту /start.")
    return res


def start(ctx, bid: int) -> None:
    if bid in _running:
        return
    _running.add(bid)
    asyncio.create_task(_run(ctx, bid), name=f"broadcast-{bid}")


async def cancel(bid: int) -> None:
    async with session() as s:
        await s.execute(update(Broadcast).where(Broadcast.id == bid, Broadcast.status == "sending").values(status="cancelled", finished_at=utcnow()))
        await s.commit()


async def resume(ctx) -> None:
    """Воркер: продолжает рассылки, прерванные перезапуском."""
    async with session() as s:
        ids = (await s.execute(select(Broadcast.id).where(Broadcast.status == "sending"))).scalars().all()
    for bid in ids:
        start(ctx, bid)


async def _run(ctx, bid: int) -> None:
    try:
        while True:
            async with session() as s:
                b = await s.get(Broadcast, bid)
                if b is None or b.status != "sending":
                    return
                ids = (await s.execute(_audience_query(b.audience, b.kind).where(User.tg_id > b.cursor).order_by(User.tg_id).limit(CHUNK))).scalars().all()
                text, kb = b.text, buttons(b)
            if not ids:
                async with session() as s:
                    await s.execute(update(Broadcast).where(Broadcast.id == bid, Broadcast.status == "sending").values(status="done", finished_at=utcnow()))
                    await s.commit()
                await _report(ctx, bid)
                return
            sent = failed = blocked = 0
            gone: list[int] = []
            for tg_id in ids:
                res = await ctx.send_broadcast(tg_id, text, kb)
                if res == "ok":
                    sent += 1
                elif res == "blocked":
                    blocked += 1
                    gone.append(tg_id)
                else:
                    failed += 1
                await asyncio.sleep(1 / RATE)
            async with session() as s:
                if gone:
                    await s.execute(update(User).where(User.tg_id.in_(gone)).values(bot_blocked=True))
                await s.execute(update(Broadcast).where(Broadcast.id == bid).values(
                    sent=Broadcast.sent + sent, failed=Broadcast.failed + failed, blocked=Broadcast.blocked + blocked, cursor=ids[-1]))
                await s.commit()
    except Exception:
        log.exception("broadcast %s", bid)
    finally:
        _running.discard(bid)


async def _report(ctx, bid: int) -> None:
    async with session() as s:
        b = await s.get(Broadcast, bid)
    if b and b.created_by:
        await ctx.notify(b.created_by, f"📣 <b>Рассылка #{b.id} завершена</b>\n\n✅ Доставлено: <b>{b.sent}</b> из {b.total}"
                         + (f"\n🚫 Заблокировали бота: {b.blocked}" if b.blocked else "") + (f"\n⚠️ Ошибки: {b.failed}" if b.failed else ""))


def as_dict(b: Broadcast) -> dict:
    return {"id": b.id, "text": b.text, "btn_text": b.btn_text, "btn_url": b.btn_url, "audience": b.audience, "kind": b.kind,
            "status": b.status, "total": b.total, "sent": b.sent, "failed": b.failed, "blocked": b.blocked,
            "created": b.created_at.isoformat(), "finished": b.finished_at.isoformat() if b.finished_at else None}


async def history(limit: int = 30) -> list[dict]:
    async with session() as s:
        rows = (await s.execute(select(Broadcast).order_by(Broadcast.id.desc()).limit(limit))).scalars().all()
    return [as_dict(b) for b in rows]


async def get(bid: int) -> dict | None:
    async with session() as s:
        b = await s.get(Broadcast, bid)
    return as_dict(b) if b else None


async def news_unsubscribed_count() -> int:
    async with session() as s:
        return (await s.execute(select(func.count()).select_from(User).where(User.news_off.is_(True)))).scalar_one()
