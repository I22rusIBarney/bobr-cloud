"""«Сообщить, когда появится» для закрытых локаций.

Локация без мест (Astra закрыла заказы) или ещё не открытая (есть в каталоге, available=false)
показывается клиенту серой. Клиент может подписаться — когда локация откроется и продержится
в продаже хотя бы 10 минут (защита от «мигания» доступности), бот напишет ему один раз
и снимет подписку. Это ответ на просьбу клиента, а не реклама, поэтому согласие на рекламу не нужно.
"""
from __future__ import annotations

import html
import time

from sqlalchemy import delete, select

from .db import LocationWatch, User, session, utcnow
from .settings import load_settings

STABLE_S = 600  # сколько локация должна быть в продаже, прежде чем уведомлять


async def _get(key: str, default):
    from .services import _setting_json
    return await _setting_json(key, default)


async def _put(key: str, value) -> None:
    from .services import _setting_put
    await _setting_put(key, value)


async def track(now_on: dict[str, bool]) -> None:
    """Вызывается после проверки каталога: какие локации когда-либо были в продаже и с какого момента открыты."""
    ever = set(await _get("ever_on_locations", []))
    since = dict(await _get("loc_open_since", {}))
    t = int(time.time())
    for code, on in now_on.items():
        if on:
            ever.add(code)
            since.setdefault(code, t)
        else:
            since.pop(code, None)
    for code in list(since):
        if code not in now_on:
            since.pop(code, None)
    await _put("ever_on_locations", sorted(ever))
    await _put("loc_open_since", since)


async def closed_locations(ctx, tg_id: int | None = None) -> list[dict]:
    """Локации из каталога, где сейчас купить нельзя: soldout — были в продаже, soon — ещё не открывались."""
    from .services import clean_name, flag, offers
    cat = await ctx.catalog()
    ever = set(await _get("ever_on_locations", []))
    mine = await watching(tg_id) if tg_id else set()
    out = []
    for loc in cat.get("locations", []) or []:
        code = loc.get("code")
        if not code:
            continue
        if loc.get("available", True) and await offers(ctx, code):
            continue
        out.append({"code": code, "name": clean_name(loc.get("name", "")) or code.upper(), "flag": flag(code),
                    "status": "soldout" if code in ever else "soon", "description": loc.get("description") or "",
                    "watching": code in mine})
    return out


async def watching(tg_id: int) -> set[str]:
    async with session() as s:
        return set((await s.execute(select(LocationWatch.code).where(LocationWatch.tg_id == tg_id))).scalars().all())


async def set_watch(tg_id: int, code: str, on: bool) -> bool:
    code = (code or "").lower()[:8]
    async with session() as s:
        row = (await s.execute(select(LocationWatch).where(LocationWatch.tg_id == tg_id, LocationWatch.code == code))).scalar_one_or_none()
        if on and row is None:
            s.add(LocationWatch(tg_id=tg_id, code=code))
        elif not on and row is not None:
            await s.delete(row)
        await s.commit()
    return on


async def notify(ctx) -> int:
    """Пишет подписавшимся, когда их локация открылась и стабильно в продаже. Возвращает число сообщений."""
    from .services import clean_name, flag, offers, fmt_rub
    since = dict(await _get("loc_open_since", {}))
    now = int(time.time())
    ready = [c for c, t in since.items() if now - int(t) >= STABLE_S]
    if not ready:
        return 0
    async with session() as s:
        cfg = await load_settings(s)
        rows = (await s.execute(select(LocationWatch).where(LocationWatch.code.in_(ready)))).scalars().all()
    if not rows:
        return 0
    # новую страну объявит рассылка «Новости»: её получатели снимаются с подписки при отправке рассылки,
    # а пока рассылка в очереди — им не пишем, чтобы не продублировать
    pend = set(await _get("loc_announce_pending", [])) if cfg.get("loc_announce", True) else set()
    cat = await ctx.catalog()
    sent = 0
    for code in ready:
        subs = [r for r in rows if r.code == code]
        if not subs:
            continue
        offs = await offers(ctx, code)
        if not offs:
            continue
        loc = next((x for x in cat.get("locations", []) if x.get("code") == code), {})
        name = f"{flag(code)} {html.escape(clean_name(loc.get('name', '')) or code.upper())}"
        price = fmt_rub(min(o["price_kop"] for o in offs))
        text = (f"🔔 <b>{name} — можно заказывать</b>\n\nВы просили сообщить, когда появятся места. Серверы от <b>{price}</b> в месяц — успейте, пока есть места."
                + (f"\n📍 {html.escape(loc['description'])}" if loc.get("description") else ""))
        done: list[int] = []
        for r in subs:
            async with session() as s:
                u = await s.get(User, r.tg_id)
            if u is not None and code in pend and u.ads_consent and u.consent_version:
                continue  # получит рассылку о новой стране; подписку снимет она
            done.append(r.id)
            if not (u is None or u.banned or u.bot_blocked):
                fn = getattr(ctx, "notify_kb", None)
                if fn:
                    await fn(r.tg_id, text, [[("🛒 Собрать сервер", f"b:l:{code}")]])
                else:
                    await ctx.notify(r.tg_id, text)
                sent += 1
        async with session() as s:
            await s.execute(delete(LocationWatch).where(LocationWatch.id.in_(done)))
            await s.commit()
    return sent


async def tick(ctx) -> None:
    await notify(ctx)
