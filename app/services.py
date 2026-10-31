"""Бизнес-логика: покупка, продление, платежи, чеки, синхронизация с Astra."""
from __future__ import annotations

import asyncio
import html
import re
import math
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select, update

from .astra import Astra, AstraError, AstraUncertain, parse_dt
from .config import env
from .db import Consent, Payment, PaymentMethod, Server, Tx, User, change_balance, session, utcnow
from .nalog import Nalog, NalogError
from .settings import cost_kop, fmt_rub, load_settings, no_markup_cfg, sell_price_kop, user_price_cfg
from .yookassa import YooKassa, YooKassaError

log = logging.getLogger(__name__)

Notify = Callable[[int, str], Awaitable[None]]


class UserError(Exception):
    """Ошибка, текст которой можно показать клиенту."""


@dataclass
class Ctx:
    astra: Astra
    yk: YooKassa
    nalog: Nalog
    notify: Notify
    alert_admin: Callable[[str], Awaitable[None]]
    bot_username: str = ""
    platega: Any = None  # второй платёжный провайдер (Platega), настраивается в админке
    # сообщение с кнопкой, открывающей мини-приложение: (tg_id, text, button_text, url)
    notify_btn: Callable[[int, str, str, str], Awaitable[None]] | None = None
    # проверка подписки: (chat, tg_id) → True/False, None — проверить нельзя
    check_member: Callable[[Any, int], Awaitable[bool | None]] | None = None
    # проверка канала для админки: chat → {ok, title, link, error}
    probe_chat: Callable[[Any], Awaitable[dict]] | None = None
    # отправка в произвольный чат (канал событий): → None или текст ошибки
    post: Callable[[Any, str], Awaitable[str | None]] | None = None
    tg_ping: Callable[[], Awaitable[Any]] | None = None
    send_transfer_offer: Callable[[int, str, int], Awaitable[None]] | None = None
    send_share_offer: Callable[[int, str, int], Awaitable[None]] | None = None
    notify_kb: Callable[[int, str, list], Awaitable[None]] | None = None
    post_file: Callable[[Any, str, str], Awaitable[str | None]] | None = None
    # рассылка одного сообщения: (tg_id, text, buttons) → ok | blocked | error
    send_broadcast: Callable[[int, str, list], Awaitable[str]] | None = None
    _catalog: tuple[float, dict] | None = None
    _alert_ts: dict[str, float] = field(default_factory=dict)

    async def catalog(self, fresh: bool = False) -> dict:
        if not fresh and self._catalog and time.time() - self._catalog[0] < 60:
            return self._catalog[1]
        data = _normalize_catalog(await self.astra.catalog())
        self._catalog = (time.time(), data)
        return data

    async def alert_once(self, key: str, text: str, every_s: int = 6 * 3600) -> None:
        if time.time() - self._alert_ts.get(key, 0) >= every_s:
            self._alert_ts[key] = time.time()
            await self.alert_admin(text)


# ───────────────────────── отображение: флаги и ОС ─────────────────────────

_RI = range(0x1F1E6, 0x1F1FF + 1)  # regional indicator symbols


def flag(code: str) -> str:
    """Флаг из двухбуквенного кода страны: nl → 🇳🇱. Для не-ISO кодов — 🌍."""
    c = (code or "").strip().upper()
    if len(c) != 2 or not c.isalpha():
        return "🌍"
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in c)


def clean_name(name: str) -> str:
    """Убирает эмодзи-флаги из названия локации Astra («🇳🇱 Нидерланды» → «Нидерланды»)."""
    out = "".join(ch for ch in (name or "") if ord(ch) not in _RI)
    return " ".join(out.split())


_OS = {
    "ubuntu": "Ubuntu", "debian": "Debian", "centos": "CentOS", "almalinux": "AlmaLinux", "alma": "AlmaLinux",
    "rockylinux": "Rocky Linux", "rocky": "Rocky Linux", "fedora": "Fedora", "windows": "Windows", "win": "Windows Server", "winserver": "Windows Server", "alpine": "Alpine",
    "arch": "Arch Linux", "archlinux": "Arch Linux", "opensuse": "openSUSE", "freebsd": "FreeBSD", "oraclelinux": "Oracle Linux",
}


def os_label(code: str) -> str:
    """ubuntu2404 → Ubuntu 24.04, debian12 → Debian 12; неизвестное — как есть, с заглавной."""
    import re

    m = re.fullmatch(r"([a-z]+)[-_]?([\d.]*)", (code or "").lower())
    if not m:
        return code
    name, ver = m.group(1), m.group(2)
    title = _OS.get(name, name.capitalize())
    if name == "ubuntu" and len(ver) == 4 and ver.isdigit():
        ver = f"{ver[:2]}.{ver[2:]}"
    return f"{title} {ver}".strip()


def _normalize_catalog(data: dict) -> dict:
    for loc in data.get("locations", []) or []:
        loc["name"] = clean_name(loc.get("name", "")) or str(loc.get("code", "")).upper()
        loc["flag"] = flag(loc.get("code", ""))
    data["os_labels"] = {o: os_label(o) for o in data.get("os", []) or []}
    return data


# ───────────────────────── каталог ─────────────────────────


def spec_key(cpu, ram, disk) -> str:
    n = lambda x: str(int(x)) if float(x) == int(float(x)) else str(x)
    return f"{n(cpu)}-{n(ram)}-{n(disk)}"


def manual_speed(cfg: dict, cpu, ram, disk) -> str:
    """Ограничение скорости тарифа из админки («200 Мбит/с») или пустая строка."""
    try:
        return (cfg.get("preset_speeds") or {}).get(spec_key(cpu, ram, disk), "")
    except (TypeError, ValueError):
        return ""


def channel_speed(channel: str) -> str:
    """Канал локации от Astra в коротком виде: «до 25 Гбит/с · shared, не гарантирован» → «25 Гбит/с»."""
    c = re.split(r"\s*[·,;(|]\s*", (channel or "").strip())[0]
    return re.sub(r"^до\s+", "", c, flags=re.I).strip()[:30]


def speed_of(cfg: dict, cpu, ram, disk, channel: str = "") -> str:
    """Скорость для клиента: ручная из админки, иначе канал локации от Astra."""
    return manual_speed(cfg, cpu, ram, disk) or channel_speed(channel)


def loc_channel(cat: dict, code: str) -> str:
    return next((l.get("channel") or "" for l in cat.get("locations", []) if l.get("code") == code), "")


def os_requirement(cfg: dict, os_: str) -> dict | None:
    """Минимум для ОС из настроек: {"cpu", "ram", "disk"} или None."""
    v = (cfg.get("os_requirements") or {}).get(os_ or "")
    try:
        c, r, d = (int(x) for x in str(v).split("-"))
    except (TypeError, ValueError):
        return None
    return {"cpu": c, "ram": r, "disk": d}


def os_fits(cfg: dict, os_: str, cpu: int, ram: int, disk: int) -> bool:
    req = os_requirement(cfg, os_)
    return req is None or (cpu >= req["cpu"] and ram >= req["ram"] and disk >= req["disk"])


def os_req_text(os_: str, req: dict) -> str:
    return (f"Для {os_label(os_)} нужно от {req['cpu']} vCPU, {req['ram']} ГБ памяти и {req['disk']} ГБ диска.")


def configurator(cat: dict) -> dict | None:
    """Диапазоны своей конфигурации из каталога Astra: {cpu|ram|disk: {min, max, step}} или None."""
    c = cat.get("configurator")
    if not isinstance(c, dict):
        return None
    out = {}
    for k in ("cpu", "ram", "disk"):
        r = c.get(k) or {}
        try:
            mn, mx, st = int(r["min"]), int(r["max"]), max(1, int(r.get("step", 1)))
        except (KeyError, TypeError, ValueError):
            return None
        if mn <= 0 or mx < mn:
            return None
        out[k] = {"min": mn, "max": mx, "step": st, "max_is_allowed": bool(r.get("max_is_allowed", True))}
    return out


def fits(conf: dict, cpu: int, ram: int, disk: int) -> bool:
    for k, v in (("cpu", cpu), ("ram", ram), ("disk", disk)):
        r = conf[k]
        if not (r["min"] <= v <= r["max"]):
            return False
        if (v - r["min"]) % r["step"] and not (v == r["max"] and r.get("max_is_allowed")):
            return False
    return True


def port_label(port) -> str:
    """«25gbps» → «25 Гбит/с», «200mbps» → «200 Мбит/с»."""
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*([gm])(?:bit|bps|b)?(?:/s|ps)?\s*", str(port or ""), re.I)
    if not m:
        return ""
    return f"{m.group(1).replace('.', ',')} {'Гбит/с' if m.group(2).lower() == 'g' else 'Мбит/с'}"


_quote_cache: dict[tuple, tuple[float, dict]] = {}
QUOTE_TTL = 600


async def astra_quote(ctx: Ctx, location: str, cpu: int, ram: int, disk: int, fresh: bool = False) -> dict:
    """Цена своей конфигурации у Astra (за месяц). Кэш 10 минут — ползунки в кабинете дёргают часто."""
    key = (location, cpu, ram, disk)
    hit = _quote_cache.get(key)
    if not fresh and hit and time.time() - hit[0] < QUOTE_TTL:
        return hit[1]
    try:
        q = await ctx.astra.quote(location=location, cpu=cpu, ram=ram, disk=disk, period="1m")
    except AstraError as ex:
        hint = (ex.payload or {}).get("hint") if isinstance(ex.payload, dict) else None
        raise UserError("Такую конфигурацию сейчас собрать нельзя" + (f" ({hint})" if hint else "") + ".") from ex
    except Exception as ex:
        raise UserError("Не удалось рассчитать цену. Попробуйте через минуту.") from ex
    if len(_quote_cache) > 2000:
        _quote_cache.clear()
    _quote_cache[key] = (time.time(), q)
    return q


def quote_cost_rub(q: dict) -> float:
    """Себестоимость месяца: берём большее из цены покупки и продления — чтобы не уйти в минус."""
    vals = [float(q[k]) for k in ("price_rub", "renew_month_rub") if isinstance(q.get(k), (int, float))]
    if not vals:
        raise UserError("Провайдер не сообщил цену этой конфигурации.")
    return max(vals)


async def custom_offer(ctx: Ctx, location: str, cpu: int, ram: int, disk: int, tg_id: int | None = None, fresh: bool = False) -> dict:
    """Своя конфигурация: цена клиенту в месяц и скорость канала."""
    cat = await ctx.catalog()
    conf = configurator(cat)
    async with session() as s:
        cfg = await load_settings(s)
        u = await s.get(User, tg_id) if tg_id else None
    if conf is None or not cfg.get("custom_enabled", True):
        raise UserError("Своя конфигурация сейчас недоступна — выберите готовый тариф.")
    loc = next((x for x in cat.get("locations", []) if x.get("code") == location), None)
    if not loc or not loc.get("available", True):
        raise UserError("Эта локация сейчас недоступна.")
    if not fits(conf, cpu, ram, disk):
        raise UserError("Такой конфигурации нет: проверьте процессор, память и диск.")
    q = await astra_quote(ctx, location, cpu, ram, disk, fresh=fresh)
    cost = quote_cost_rub(q)
    pcfg = user_price_cfg(cfg, u)
    spec = q.get("spec") or {}
    speed = manual_speed(cfg, cpu, ram, disk) or port_label(spec.get("port")) or channel_speed(loc.get("channel", ""))
    return {"idx": None, "cpu": cpu, "ram": ram, "disk": disk, "cost_rub": cost, "price_kop": sell_price_kop(cost, pcfg),
            "speed": speed, "speed_limited": bool(manual_speed(cfg, cpu, ram, disk))}


async def offers(ctx: Ctx, location: str, tg_id: int | None = None) -> list[dict]:
    """Тарифы для локации с ценой клиенту (с учётом персонального «без наценки»)."""
    cat = await ctx.catalog()
    async with session() as s:
        cfg = await load_settings(s)
        u = await s.get(User, tg_id) if tg_id else None
    cfg = user_price_cfg(cfg, u)
    channel = loc_channel(cat, location)
    out = []
    for p in cat.get("presets", []):
        if p["idx"] in cfg["hidden_presets"]:
            continue
        cost = p.get("price_month_rub", {}).get(location)
        if cost is None:
            continue
        cost = max(cost, (p.get("renew_month_rub") or {}).get(location) or cost)  # цена продления у Astra может быть выше
        out.append(
            {
                "idx": p["idx"],
                "cpu": p["cpu"],
                "ram": p["ram"],
                "disk": p["disk"],
                "cost_rub": cost,
                "price_kop": sell_price_kop(cost, cfg),
                "speed": speed_of(cfg, p["cpu"], p["ram"], p["disk"], channel),
                "speed_limited": bool(manual_speed(cfg, p["cpu"], p["ram"], p["disk"])),
            }
        )
    return out


async def locations(ctx: Ctx) -> list[dict]:
    cat = await ctx.catalog()
    return [loc for loc in cat.get("locations", []) if loc.get("available", True)]


# ───────────────────────── покупка ─────────────────────────


def check_password(pw: str) -> str:
    """Пароль root от клиента: 8–64 печатных символа ASCII без пробелов."""
    import re

    pw = pw or ""
    if not re.fullmatch(r"[\x21-\x7e]{8,64}", pw):
        raise UserError("Пароль: от 8 до 64 символов, латиница, цифры и знаки, без пробелов и кириллицы.")
    return pw


# ───────────────────────── сроки аренды ─────────────────────────

PERIOD_MONTHS = {"1m": 1, "3m": 3, "6m": 6, "1y": 12}
_PERIOD_KEY = {"3m": "period_3m_pct", "6m": "period_6m_pct", "1y": "period_1y_pct"}


def period_options(cfg: dict, cat: dict) -> list[dict]:
    """Сроки, доступные клиенту: из каталога Astra, со скидкой из админки. 1 месяц есть всегда."""
    have = [p for p in (cat.get("periods") or ["1m"]) if p in PERIOD_MONTHS]
    out = [{"code": "1m", "months": 1, "pct": 0}]
    if cfg.get("periods_enabled", True):
        for code in ("3m", "6m", "1y"):
            if code in have:
                out.append({"code": code, "months": PERIOD_MONTHS[code], "pct": int(cfg.get(_PERIOD_KEY[code], 0))})
    return out


def period_price(month_kop: int, months: int, pct: int) -> int:
    """Цена за срок: месяц × N со скидкой за срок, вверх до рубля."""
    if months <= 1:
        return month_kop
    return math.ceil(month_kop * months * (100 - max(0, pct)) / 100 / 100) * 100


def apply_discount(price_kop: int, pct: int) -> int:
    """Цена со скидкой, округлённая вверх до рубля."""
    if pct <= 0:
        return price_kop
    return max(0, math.ceil(price_kop * (100 - pct) / 100 / 100) * 100)


async def buy_server(ctx: Ctx, tg_id: int, idx: int | None, location: str, os_: str, password: str | None = None, period: str = "1m",
                     spec: dict | None = None) -> Server:
    """Покупка: готовый тариф (idx) или своя конфигурация (idx=None, spec={cpu, ram, disk})."""
    if password:
        password = check_password(password)
    async with session() as s:
        cfg = await load_settings(s)
        user = await s.get(User, tg_id)
        if user is None or user.banned:
            raise UserError("Доступ ограничен.")
        if cfg["maintenance"]:
            raise UserError("Продажи временно приостановлены. Попробуйте позже.")
        if idx is not None and idx in cfg["hidden_presets"]:
            raise UserError("Этот тариф недоступен.")

        cat = await ctx.catalog(fresh=True)
        loc = next((x for x in cat.get("locations", []) if x["code"] == location), None)
        if idx is None:  # своя конфигурация: цена — свежий расчёт Astra
            if not spec:
                raise UserError("Не указана конфигурация.")
            c = await custom_offer(ctx, location, int(spec["cpu"]), int(spec["ram"]), int(spec["disk"]), tg_id, fresh=True)
            preset = {"cpu": c["cpu"], "ram": c["ram"], "disk": c["disk"]}
            cost_rub = renew_rub = c["cost_rub"]
        else:
            preset = next((p for p in cat.get("presets", []) if p["idx"] == idx), None)
            if not preset or not loc or not loc.get("available", True):
                raise UserError("Тариф или локация недоступны.")
            cost_rub = preset["price_month_rub"].get(location)
            if cost_rub is None:
                raise UserError("В этой локации тариф недоступен.")
            renew_rub = (preset.get("renew_month_rub") or {}).get(location) or cost_rub
            cost_rub = max(cost_rub, renew_rub)  # продаём не дешевле, чем потом продлевать
        if not loc or not loc.get("available", True):
            raise UserError("Эта локация сейчас недоступна.")
        if os_ not in cat.get("os", []):
            raise UserError("Эта ОС недоступна.")
        if not os_fits(cfg, os_, preset["cpu"], preset["ram"], preset["disk"]):
            raise UserError(os_req_text(os_, os_requirement(cfg, os_)) + " Выберите конфигурацию мощнее или другую систему.")

        opt = next((o for o in period_options(cfg, cat) if o["code"] == period), None)
        if opt is None:
            raise UserError("Этот срок аренды недоступен.")
        months = opt["months"]
        price = sell_price_kop(cost_rub, user_price_cfg(cfg, user))  # цена месяца = цена продления
        discount_pct = max(0, min(100, int(user.promo_discount_pct or 0)))
        if user.promo_until and user.promo_until < utcnow():
            discount_pct = 0
        # промокод — скидка на первый месяц, скидка за срок — на весь срок
        charge = max(0, period_price(price, months, opt["pct"]) - (price - apply_discount(price, discount_pct)))
        ref = f"{location}:{idx}" if idx is not None else f"{location}:{preset['cpu']}-{preset['ram']}-{preset['disk']}"
        if not await change_balance(s, tg_id, -charge, "purchase", ref):
            raise UserError("Недостаточно средств на балансе. Пополните баланс и повторите.")

    name = f"vps-{secrets.token_hex(3)}"
    try:
        if idx is not None:
            resp = await ctx.astra.create(idx=idx, location=location, os=os_, name=name, period=period, password=password or None)
        else:
            resp = await ctx.astra.create(cpu=preset["cpu"], ram=preset["ram"], disk=preset["disk"], location=location, os=os_,
                                          name=name, period=period, password=password or None)
    except AstraError as ex:
        await _refund(tg_id, charge, "refund_create", name)
        if ex.status == 402:
            await ctx.alert_once("astra_balance", "⚠️ Баланс Astra закончился — покупки не проходят. Пополните через @astravpsbot.")
            raise UserError("Сервис временно не может выдать сервер. Деньги возвращены на баланс.") from ex
        if ex.status == 409:
            raise UserError("В этой локации сейчас нет свободных мощностей. Деньги возвращены на баланс.") from ex
        log.exception("astra create failed")
        await ctx.alert_admin(f"❌ Astra create error {ex.status}: {ex.payload}")
        raise UserError("Не удалось создать сервер. Деньги возвращены на баланс.") from ex
    except AstraUncertain as ex:
        await ctx.alert_admin(f"⚠️ Неизвестный итог создания сервера (таймаут) для user {tg_id}, name={name}. Деньги списаны, проверьте в Astra и при необходимости верните вручную.")
        raise UserError("Ответ от провайдера не получен. Мы проверим заказ вручную в ближайшее время — если сервера не будет, деньги вернём.") from ex

    spec = resp.get("spec", {})
    charged = resp.get("charged_rub")
    srv = Server(
        astra_id=int(resp["id"]),
        tg_id=tg_id,
        name=name,
        location=location,
        cpu=spec.get("cpu", preset["cpu"]),
        ram=spec.get("ram", preset["ram"]),
        disk=spec.get("disk", preset["disk"]),
        os=spec.get("os", os_),
        price_kop=price,
        period_paid_kop=charge,
        period_months=float(months),
        # себестоимость месяца для маржи и продления — цена продления у Astra
        cost_kop=cost_kop(max(renew_rub, charged / months)) if charged is not None else cost_kop(renew_rub),
        status="provisioning",
        expires_at=utcnow() + timedelta(days=int(resp.get("expires_in_days", 30 * months))),
        auto_renew=cfg["auto_renew_default"],
    )
    async with session() as s:
        s.add(srv)
        u = await s.get(User, tg_id)
        if discount_pct:
            u.promo_discount_pct, u.promo_code, u.promo_until = 0, None, None
        await s.commit()
    from . import serverlog
    await serverlog.add(srv.id, tg_id, "purchase", f"{months} мес" if months > 1 else "")
    from . import events
    term = {1: "1 мес", 3: "3 мес", 6: "6 мес", 12: "1 год"}.get(months, f"{months} мес")
    await events.emit(ctx, "sales", f"🛒 <b>Покупка · {fmt_rub(charge)}</b>\n\n👤 {events.who(u)}\n🖥 {flag(location)} {srv.cpu} vCPU · {srv.ram} ГБ · {srv.disk} ГБ · {html.escape(os_label(srv.os))}\n"
                      f"🔄 {term}" + (f" · промокод −{discount_pct}%" if discount_pct else "") + f"\n💰 Маржа в месяц: {fmt_rub(price - srv.cost_kop)}")
    return srv


async def _refund(tg_id: int, kop: int, kind: str, ref: str) -> None:
    async with session() as s:
        await change_balance(s, tg_id, kop, kind, ref)


# ───────────────────────── продление ─────────────────────────


async def renew_server(ctx: Ctx, server_id: int, payer: int | None = None, free: bool = False, actor: int | None = None) -> Server:
    """Продление на 30 дней. payer — кто платит (по умолчанию владелец; участник общего доступа — сам).
    free — за счёт сервиса (админ продлевает в качестве компенсации, с клиента не списываем)."""
    async with session() as s:
        srv = await s.get(Server, server_id)
        if srv is None or srv.status in ("deleted", "provisioning"):
            raise UserError("Сервер недоступен для продления.")
        price = srv.price_kop
        explicit_payer = payer
        payer = payer or srv.tg_id
        if free:
            price = 0
        elif not await change_balance(s, payer, -price, "renew", str(srv.astra_id)):
            raise UserError("Недостаточно средств для продления.")
    try:
        await ctx.astra.action(srv.astra_id, "renew")
    except AstraError as ex:
        if price:
            await _refund(payer, price, "refund_renew", str(srv.astra_id))
        if ex.status == 402:
            await ctx.alert_once("astra_balance", "⚠️ Баланс Astra закончился — продления не проходят.")
        else:
            await ctx.alert_admin(f"❌ Ошибка продления #{srv.astra_id}: {ex.status} {ex.payload}")
        raise UserError("Не удалось продлить сервер, деньги возвращены на баланс.") from ex
    except AstraUncertain as ex:
        await ctx.alert_admin(f"⚠️ Неизвестный итог продления #{srv.astra_id} (таймаут). Деньги клиента списаны, проверьте вручную.")
        raise UserError("Ответ от провайдера не получен, проверим продление вручную.") from ex
    # сначала сдвигаем срок сами: если запрос состояния ниже не пройдёт, не продлим повторно
    async with session() as s:
        row = await s.get(Server, server_id)
        now = utcnow()
        base = row.expires_at if row.expires_at and row.expires_at > now else now
        # остаток уже оплаченного срока (важно при покупке на несколько месяцев) + новый месяц
        months_old = float(row.period_months or 1.0)
        left = min(max(0.0, (base - now).total_seconds() / 86400 / 30.0), months_old)
        row.period_paid_kop = int(round(paid_base(row) * left)) + price
        row.period_months = round(left + 1.0, 4)
        row.expires_at = base + timedelta(days=30)
        row.notified_stage = 0
        row.renew_try_at = now
        await s.commit()
    from . import serverlog
    if free:
        await serverlog.add(server_id, actor or 0, "renew_free")
    else:
        await serverlog.add(server_id, payer if explicit_payer else 0, "renew" if explicit_payer else "autorenew")
    await refresh_server(ctx, srv.astra_id)  # точная дата от Astra, если доступна
    async with session() as s:
        srv = await s.get(Server, server_id)
        await s.commit()
    return srv


async def refresh_server(ctx: Ctx, astra_id: int) -> None:
    try:
        r = await ctx.astra.server(astra_id)
    except Exception:
        log.warning("refresh_server %s failed", astra_id, exc_info=True)
        return
    async with session() as s:
        srv = (await s.execute(select(Server).where(Server.astra_id == astra_id))).scalar_one_or_none()
        if srv:
            _apply_remote(srv, r)
            await s.commit()


def _apply_remote(srv: Server, r: dict) -> None:
    if r.get("status"):
        srv.status = r["status"]
    if r.get("ip"):
        srv.ip = r["ip"]
    if r.get("root_password"):
        srv.root_password = r["root_password"]
    exp = parse_dt(r.get("expires_at"))
    if exp:
        srv.expires_at = exp
    spec = r.get("spec") or {}
    if spec.get("os"):
        srv.os = spec["os"]
    for k in ("cpu", "ram", "disk"):
        if isinstance(spec.get(k), (int, float)) and spec[k] > 0:
            setattr(srv, k, int(spec[k]))
    if r.get("name") and isinstance(r["name"], str):
        srv.name = r["name"][:60]
    # Astra отдаёт цену продления сервера — по ней сторож маржи видит повышения
    for k in ("renew_month_rub", "price_month_rub"):
        v = r.get(k)
        if isinstance(v, (int, float)) and v > 0:
            srv.cost_kop = cost_kop(v)
            break


# ───────────────────────── действия над сервером ─────────────────────────


async def get_user_server(s, tg_id: int, server_id: int) -> Server:
    srv = await s.get(Server, server_id)
    if srv is None or srv.tg_id != tg_id or srv.status == "deleted":
        raise UserError("Сервер не найден.")
    return srv


_LINK_KEYS = ("url", "link", "vnc_url", "console_url", "vnc", "console", "href", "novnc_url", "web_url")


def vnc_link(resp) -> str | None:
    """Ссылка на консоль из ответа Astra: ищем по известным полям, затем любую http(s)-ссылку в любом месте ответа."""

    def walk(x, depth=0):
        if depth > 5:
            return None
        if isinstance(x, str):
            m = re.search(r"https?://\S+", x)
            return m.group(0).rstrip('",)') if m else None
        if isinstance(x, dict):
            for k in _LINK_KEYS:  # сначала явные поля
                if k in x:
                    r = walk(x[k], depth + 1)
                    if r:
                        return r
            for v in x.values():
                r = walk(v, depth + 1)
                if r:
                    return r
        if isinstance(x, (list, tuple)):
            for v in x:
                r = walk(v, depth + 1)
                if r:
                    return r
        return None

    found = walk(resp)
    if found:
        return _brand_vnc(found)
    # запасной вариант: адрес без схемы или только токен консоли (формат ссылок Astra: https://vnc.astravps.co/<токен>)
    def strings(x, key="", depth=0):
        if depth > 5:
            return
        if isinstance(x, str):
            yield key, x
        elif isinstance(x, dict):
            for k, v in x.items():
                yield from strings(v, str(k).lower(), depth + 1)
        elif isinstance(x, (list, tuple)):
            for v in x:
                yield from strings(v, key, depth + 1)
    for key, val in strings(resp):
        v = val.strip()
        m = re.match(r"^(?://)?(vnc\.[a-z0-9.-]+/\S+)$", v, re.I)
        if m:
            return _brand_vnc("https://" + m.group(1))
        if key in ("token", "vnc_token", "console_token", "key", "session", "hash", "id", "code") and re.fullmatch(r"[A-Za-z0-9_-]{20,128}", v):
            return _brand_vnc(VNC_BASE + v)
    return None


VNC_BASE = "https://vnc.astravps.co/"
VNC_HOST = "vnc.astravps.co"


def _brand_vnc(url: str) -> str:
    """Если настроен свой домен консоли (bcloud vnc), показываем его вместо адреса Astra — Caddy проксирует."""
    from .config import env

    d = (env().vnc_domain or "").strip().lower().removeprefix("https://").strip("/")
    if not d:
        return url
    return re.sub(r"^https?://" + re.escape(VNC_HOST) + r"(?=[/:?#]|$)", "https://" + d, url, flags=re.I)


IP_FAILED = {"failed", "error", "rejected", "canceled", "cancelled", "declined"}
IP_DONE = {"done", "success", "succeeded", "completed", "ok", "finished"}


def _ts(v) -> datetime | None:
    if isinstance(v, (int, float)) and v > 0:
        return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=timezone.utc)
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def ip_status_correlated(r: dict) -> bool | None:
    """Astra сама говорит, относится ли последняя операция к нашему запросу (request_correlated)."""
    v = r.get("request_correlated") if isinstance(r, dict) else None
    return v if isinstance(v, bool) else None


def parse_ip_status(r: dict) -> tuple[str | None, str, datetime | None]:
    """Из ответа change-ip/status: текущий IP (current_ip), статус последней операции, её время."""
    r = r if isinstance(r, dict) else {}
    ip = r.get("ip") or r.get("current_ip") or (r.get("server") or {}).get("ip")
    op = r.get("last_operation") or r.get("operation") or r.get("last") or r.get("last_change") or {}
    if not isinstance(op, dict):
        op = {}
    status = str(op.get("status") or r.get("status") or "").lower()
    when = None
    for k in ("created_at", "requested_at", "started_at", "at", "time", "updated_at", "finished_at"):
        when = _ts(op.get(k))
        if when:
            break
    return (str(ip) if ip else None), status, when


async def verify_change_ip(ctx: Ctx, server_id: int, astra_id: int, old_ip: str | None, started: datetime, charge: int, tg_id: int,
                           tries: int = 10, delay: float = 60.0) -> str:
    """Проверка смены IP после таймаута: done — IP сменился, refunded — операция отклонена (деньги вернули),
    unknown — за 10 минут ясности нет (решает админ, автоматически не возвращаем и не повторяем)."""
    from . import serverlog

    for _ in range(tries):
        await asyncio.sleep(delay)
        try:
            raw = await ctx.astra.change_ip_status(astra_id)
            ip, status, when = parse_ip_status(raw)
        except Exception:
            log.warning("change-ip status %s failed", astra_id, exc_info=True)
            continue
        corr = ip_status_correlated(raw)
        # старая операция не подтверждает новую: верим признаку Astra, без него — времени операции
        fresh_op = corr if corr is not None else (when is not None and when >= started - timedelta(minutes=2))
        if ip and old_ip and ip != old_ip:
            async with session() as s:
                row = await s.get(Server, server_id)
                if row:
                    row.ip = ip
                    await s.commit()
            await refresh_server(ctx, astra_id)
            await serverlog.add(server_id, tg_id, "change-ip", f"новый IP {ip}")
            await ctx.notify(tg_id, f"✅ <b>IP-адрес сменён</b>\n\nНовый IP: <code>{html.escape(ip)}</code>")
            return "done"
        if fresh_op and status in IP_FAILED:
            if charge:
                await _refund(tg_id, charge, "refund_change_ip", str(astra_id))
            await ctx.notify(tg_id, "❌ <b>IP-адрес не сменился</b>\n\nПровайдер отклонил операцию"
                                    + (f", {fmt_rub(charge)} вернули на баланс." if charge else ".") + " Можно попробовать ещё раз.")
            await ctx.alert_admin(f"ℹ️ Смена IP #{astra_id} после таймаута: отклонена, клиенту {tg_id} возвращено {fmt_rub(charge)}.")
            return "refunded"
    await ctx.alert_admin(f"⚠️ Смена IP #{astra_id} (клиент {tg_id}) не подтверждена за {tries} мин после таймаута. "
                          f"Списано {fmt_rub(charge)}, IP был {old_ip}. Проверьте вручную — автоматически не возвращаем и не повторяем.")
    await ctx.notify(tg_id, "⏳ Смена IP ещё не подтверждена провайдером — мы проверим вручную и напишем. Повторно запускать не нужно.")
    return "unknown"


def change_ip_price_kop(cfg: dict, user: User | None) -> int:
    """Смена IP: клиентам без наценки — по себестоимости Astra, остальным — цена из настроек."""
    if user is not None and user.no_markup:
        return int(cfg.get("change_ip_cost_rub", 100)) * 100
    return int(cfg["change_ip_price_rub"]) * 100


async def get_server_access(s, tg_id: int, server_id: int) -> tuple[Server, str]:
    """Сервер и роль клиента: owner — свой, member — общий доступ."""
    from .shares import role

    srv = await s.get(Server, server_id)
    r = await role(s, tg_id, srv)
    if r is None:
        raise UserError("Сервер не найден.")
    return srv, r


async def server_action(ctx: Ctx, tg_id: int, server_id: int, action: str, body: dict | None = None, actor: int | None = None,
                        free: bool = False) -> dict:
    """start | stop | reboot | reinstall | password | vnc | change-ip. actor — кто нажал (админ от имени владельца).
    free — смена IP за счёт сервиса, без списания с клиента (только из админки)."""
    from .shares import MEMBER_ACTIONS

    async with session() as s:
        srv, who = await get_server_access(s, tg_id, server_id)
        if who == "member" and action not in MEMBER_ACTIONS:
            raise UserError("Это действие доступно только владельцу сервера.")
        cfg = await load_settings(s)
        if srv.status not in ("active", "suspended"):
            raise UserError("Сервер сейчас недоступен для этого действия.")
        if action == "reinstall" and body and not os_fits(cfg, body.get("os", ""), srv.cpu, srv.ram, srv.disk):
            raise UserError(os_req_text(body["os"], os_requirement(cfg, body["os"])) + " Сначала увеличьте мощность сервера.")
        charge = 0
        if action == "change-ip" and not free:
            payer = await s.get(User, tg_id)
            charge = change_ip_price_kop(cfg, payer)
            if not await change_balance(s, tg_id, -charge, "change_ip", str(srv.astra_id)):
                raise UserError(f"Для смены IP нужно {fmt_rub(charge)} на балансе.")
    try:
        resp = await ctx.astra.action(srv.astra_id, action, body)
    except AstraError as ex:
        if charge:
            await _refund(tg_id, charge, "refund_change_ip", str(srv.astra_id))
        raise UserError(f"Не удалось выполнить действие ({ex.code or ex.status}).") from ex
    except AstraUncertain as ex:
        if action == "change-ip":  # таймаут ≠ отказ: проверим результат по статусу операции, деньги пока удержаны
            asyncio.get_running_loop().create_task(verify_change_ip(ctx, server_id, srv.astra_id, srv.ip, utcnow(), charge, tg_id))
            raise UserError("Провайдер отвечает с задержкой. Проверим смену IP автоматически и пришлём результат в течение 10 минут.") from ex
        await ctx.alert_admin(f"⚠️ Неизвестный итог действия {action} #{srv.astra_id} (таймаут).")
        raise UserError("Ответ от провайдера не получен. Проверьте статус сервера через минуту.") from ex
    if action == "vnc":
        link = vnc_link(resp)
        if not link:
            await ctx.alert_once("vnc_format", f"⚠️ Astra не вернула ссылку на VNC (сервер #{srv.astra_id}). Ответ: <code>{html.escape(str(resp)[:600])}</code>", every_s=3600)
            raise UserError("Провайдер не прислал ссылку на консоль. Попробуйте через минуту — если повторится, напишите в поддержку.")
        resp = {"link": link, "raw": resp}
    from . import serverlog
    quiet = {"start": 5, "stop": 10, "reboot": 10, "reinstall": 20, "password": 20, "change-ip": 20}.get(action)
    if quiet:  # мониторинг не будит клиента, пока сервер перезапускается
        async with session() as s:
            row = await s.get(Server, server_id)
            row.mon_quiet_until, row.mon_fail = utcnow() + timedelta(minutes=quiet), 0
            await s.commit()
    await serverlog.add(server_id, actor or tg_id, action, os_label(body["os"]) if action == "reinstall" and body and body.get("os") else "")
    if action in ("reinstall", "password", "change-ip"):
        await refresh_server(ctx, srv.astra_id)
    return resp


async def set_no_markup(tg_id: int, enabled: bool) -> dict:
    """Включает/выключает клиенту цены без наценки (совместимость со старым переключателем)."""
    return await set_markup(tg_id, "none" if enabled else "default")


async def set_markup(tg_id: int, mode: str, pct: float | None = None) -> dict:
    """Наценка клиента: default — общая, custom — своя (pct), none — по себестоимости.
    Сразу пересчитывает цену продления его текущих серверов."""
    if mode not in ("default", "custom", "none"):
        raise UserError("Неизвестный режим наценки.")
    if mode == "custom" and (pct is None or not (-50 <= float(pct) <= 1000)):
        raise UserError("Укажите наценку от −50 до 1000%.")
    async with session() as s:
        u = await s.get(User, tg_id)
        if u is None:
            raise UserError("Клиент не найден.")
        u.no_markup = mode == "none"
        u.markup_pct = float(pct) if mode == "custom" else None
        cfg = await load_settings(s)
        pcfg = user_price_cfg(cfg, u)
        enabled = mode == "none"
        rows = (await s.execute(select(Server).where(Server.tg_id == tg_id, Server.status != "deleted"))).scalars().all()
        changed = []
        for srv in rows:
            if srv.cost_kop > 0:
                new = sell_price_kop(srv.cost_kop / 100, pcfg)
                if new != srv.price_kop:
                    changed.append({"name": srv.name, "old_rub": srv.price_kop / 100, "new_rub": new / 100})
                    srv.price_kop = new
        await s.commit()
    return {"no_markup": bool(enabled), "mode": mode, "markup_pct": float(pct) if mode == "custom" else None, "repriced": changed}


async def rename_server(ctx: Ctx, tg_id: int, server_id: int, name: str) -> str:
    name = " ".join((name or "").split())
    if not (2 <= len(name) <= 60):
        raise UserError("Название: от 2 до 60 символов.")
    async with session() as s:
        srv = await get_user_server(s, tg_id, server_id)
        if srv.status == "provisioning":
            raise UserError("Дождитесь, пока сервер создастся.")
    try:
        await ctx.astra.action(srv.astra_id, "rename", {"name": name})
    except AstraError as ex:
        raise UserError(f"Не удалось переименовать ({ex.code or ex.status}).") from ex
    except AstraUncertain as ex:
        raise UserError("Ответ от провайдера не получен. Попробуйте ещё раз.") from ex
    async with session() as s:
        row = await s.get(Server, server_id)
        row.name = name
        await s.commit()
    from . import serverlog
    await serverlog.add(server_id, tg_id, "rename", name)
    return name


async def server_live(ctx: Ctx, tg_id: int, server_id: int) -> dict:
    """Актуальное состояние из Astra: питание (running/stopped) и готовый текст подключения."""
    async with session() as s:
        srv, _ = await get_server_access(s, tg_id, server_id)
    try:
        r = await ctx.astra.server(srv.astra_id)
    except Exception as ex:
        raise UserError("Не удалось получить состояние сервера. Попробуйте через минуту.") from ex
    async with session() as s:
        row = await s.get(Server, server_id)
        _apply_remote(row, r)
        await s.commit()
    return {"power_status": r.get("power_status"), "conn_info": r.get("conn_info")}


# ───────────────────────── апгрейд CPU/RAM ─────────────────────────


def upgrade_options(presets: list[dict], location: str, srv_cpu: int, srv_ram: int) -> dict:
    """Варианты CPU и RAM для апгрейда: значения из тарифов локации, не меньше текущих."""
    ps = [p for p in presets if location in (p.get("price_month_rub") or {})]
    cpu = sorted({p["cpu"] for p in ps if p["cpu"] >= srv_cpu} | {srv_cpu})
    ram = sorted({p["ram"] for p in ps if p["ram"] >= srv_ram} | {srv_ram})
    return {"cpu": cpu, "ram": ram}


def estimate_month_cost(presets: list[dict], location: str, cpu: int, ram: int, disk: int) -> float | None:
    """Оценка месячной стоимости у Astra: самый дешёвый тариф, покрывающий CPU, RAM и текущий диск.
    Это оценка сверху — точную сумму Astra сообщает только после апгрейда."""
    c = [p["price_month_rub"][location] for p in presets
         if location in (p.get("price_month_rub") or {}) and p["cpu"] >= cpu and p["ram"] >= ram and p["disk"] >= disk]
    return min(c) if c else None


def prorate_kop(month_diff_kop: int, days_left: float) -> int:
    return max(100, math.ceil(max(0, month_diff_kop) * max(0.0, days_left) / 30 / 100) * 100)


def settle_from_charged(charged_rub: float, markup_pct: float) -> int:
    """Сумма для клиента по фактическому списанию Astra: с наценкой, вверх до рубля, в копейках."""
    return max(100, math.ceil(round(charged_rub * (1 + markup_pct / 100), 6)) * 100)


async def upgrade_quote(ctx: Ctx, tg_id: int, server_id: int, cpu: int | None = None, ram: int | None = None) -> dict:
    async with session() as s:
        srv = await get_user_server(s, tg_id, server_id)
        cfg = await load_settings(s)
        u = await s.get(User, tg_id)
    cfg = user_price_cfg(cfg, u)
    if srv.status != "active":
        raise UserError("Апгрейд доступен только для активного сервера.")
    cat = await ctx.catalog()
    presets = cat.get("presets", [])
    opts = upgrade_options(presets, srv.location, srv.cpu, srv.ram)
    conf = configurator(cat)
    if conf:  # с конструктором доступны любые значения из диапазона
        opts = {k: sorted(set(opts[k]) | {v for v in range(conf[k]["min"], conf[k]["max"] + 1, conf[k]["step"]) if v >= getattr(srv, k)})
                for k in ("cpu", "ram")}
    days_left = max(0.0, (srv.expires_at - utcnow()).total_seconds() / 86400) if srv.expires_at else 0.0
    out = {"current": {"cpu": srv.cpu, "ram": srv.ram, "disk": srv.disk}, "options": opts,
           "days_left": round(days_left, 1), "price_kop": srv.price_kop}
    if cpu is None or ram is None:
        return out
    if cpu < srv.cpu or ram < srv.ram:
        raise UserError("Уменьшить процессор или память нельзя.")
    if cpu == srv.cpu and ram == srv.ram:
        raise UserError("Выберите больше процессоров или памяти.")
    if cpu not in opts["cpu"] or ram not in opts["ram"]:
        raise UserError("Такого значения нет в тарифах.")
    est_cost = None
    if conf and fits(conf, cpu, ram, srv.disk):
        try:
            est_cost = quote_cost_rub(await astra_quote(ctx, srv.location, cpu, ram, srv.disk))
        except UserError:
            est_cost = None
    if est_cost is None:
        est_cost = estimate_month_cost(presets, srv.location, cpu, ram, srv.disk)
    if est_cost is None:
        raise UserError("Такого сочетания нет в тарифах. Уменьшите процессор или память.")
    new_price = max(sell_price_kop(est_cost, cfg), srv.price_kop)
    out.update(cpu=cpu, ram=ram, estimate_kop=prorate_kop(new_price - srv.price_kop, days_left), new_price_kop=new_price)
    return out


async def upgrade_server(ctx: Ctx, tg_id: int, server_id: int, cpu: int, ram: int) -> dict:
    q = await upgrade_quote(ctx, tg_id, server_id, cpu, ram)
    hold = q["estimate_kop"]
    async with session() as s:
        srv = await get_user_server(s, tg_id, server_id)
        cfg = await load_settings(s)
        u = await s.get(User, tg_id)
        cfg = user_price_cfg(cfg, u)
        if not await change_balance(s, tg_id, -hold, "upgrade", str(srv.astra_id)):
            raise UserError(f"Для апгрейда нужно около {fmt_rub(hold)} на балансе.")
    try:
        resp = await ctx.astra.action(srv.astra_id, "upgrade", {"cpu": cpu, "ram": ram})
    except AstraError as ex:
        await _refund(tg_id, hold, "refund_upgrade", str(srv.astra_id))
        if ex.status == 402:
            await ctx.alert_once("astra_balance", "⚠️ Баланс Astra закончился — апгрейды не проходят.")
            raise UserError("Сервис временно не может выполнить апгрейд. Деньги возвращены на баланс.") from ex
        if ex.status == 409:
            raise UserError("На узле сейчас нет свободных ресурсов для апгрейда. Деньги возвращены на баланс.") from ex
        await ctx.alert_admin(f"❌ Ошибка апгрейда #{srv.astra_id}: {ex.status} {ex.payload}")
        raise UserError(f"Апгрейд не выполнен ({ex.code or ex.status}). Деньги возвращены на баланс.") from ex
    except AstraUncertain as ex:
        await ctx.alert_admin(f"⚠️ Неизвестный итог апгрейда #{srv.astra_id} (таймаут), удержано {fmt_rub(hold)} у клиента {tg_id}. Проверьте вручную.")
        raise UserError("Ответ от провайдера не получен. Проверим апгрейд вручную, при отказе деньги вернём.") from ex

    # сверка: Astra сообщает фактическое списание — пересчитываем сумму для клиента
    charged = None
    if isinstance(resp, dict):
        for k in ("charged_rub", "charged", "amount_rub", "price_rub", "cost_rub"):
            if isinstance(resp.get(k), (int, float)):
                charged = float(resp[k])
                break
    final = hold
    if charged is not None:
        final = settle_from_charged(charged, cfg["markup_pct"])
        delta = final - hold
        async with session() as s:
            if delta < 0:
                await change_balance(s, tg_id, -delta, "upgrade_settle", str(srv.astra_id))
            elif delta > 0 and not await change_balance(s, tg_id, -delta, "upgrade_settle", str(srv.astra_id)):
                await ctx.alert_admin(f"⚠️ Апгрейд #{srv.astra_id}: Astra списала {charged} ₽, клиенту {tg_id} не хватило {fmt_rub(delta)} на доплату.")
                final = hold
    else:
        await ctx.alert_once("upgrade_format", f"ℹ️ Ответ Astra на апгрейд без суммы списания, клиенту засчитана оценка {fmt_rub(hold)}. Ответ: {str(resp)[:400]}", every_s=7 * 86400)

    # новые характеристики и цена продления
    remote_cost = None
    try:
        r = await ctx.astra.server(srv.astra_id)
        remote_cost = r.get("price_month_rub")
    except Exception:
        r = None
    async with session() as s:
        row = await s.get(Server, server_id)
        if r:
            _apply_remote(row, r)
        else:
            row.cpu, row.ram = cpu, ram
        if isinstance(remote_cost, (int, float)) and remote_cost > 0:
            row.cost_kop = cost_kop(remote_cost)
            row.price_kop = max(sell_price_kop(remote_cost, cfg), row.price_kop)
        else:
            row.price_kop = q["new_price_kop"]
        await s.commit()
        new_price = row.price_kop
        from . import serverlog
        await serverlog.add(server_id, tg_id, "upgrade", f"{row.cpu} vCPU, {row.ram} ГБ")
        u = await s.get(User, tg_id)
        name = row.name
    from . import events
    await events.emit(ctx, "sales", f"⬆️ <b>Апгрейд · {fmt_rub(final)}</b>\n\n👤 {events.who(u)}\n🖥 {html.escape(name)}: {cpu} vCPU · {ram} ГБ\n💳 Продление теперь {fmt_rub(new_price)}")
    return {"charged_kop": final, "new_price_kop": new_price, "cpu": cpu, "ram": ram}


async def cancel_server(ctx: Ctx, tg_id: int, server_id: int, refund: bool = True, actor: int | None = None) -> int:
    """Удаляет сервер, возвращает клиенту часть остатка. Возвращает сумму возврата (коп)."""
    async with session() as s:
        srv = await get_user_server(s, tg_id, server_id)
        cfg = await load_settings(s)
    try:
        await ctx.astra.delete(srv.astra_id)
    except AstraError as ex:
        raise UserError(f"Не удалось удалить сервер ({ex.code or ex.status}).") from ex
    except AstraUncertain as ex:
        await ctx.alert_admin(f"⚠️ Неизвестный итог удаления #{srv.astra_id} (таймаут).")
        raise UserError("Ответ от провайдера не получен. Проверьте список серверов через минуту.") from ex
    do_refund = refund
    refund = refund_amount(paid_base(srv), srv.expires_at, cfg["refund_pct"], paid_months(srv)) if do_refund else 0
    async with session() as s:
        row = await s.get(Server, server_id)
        row.status = "deleted"
        row.auto_renew = False
        row.deleted_at = utcnow()
        await s.commit()
        if refund > 0:
            await change_balance(s, tg_id, refund, "refund_cancel", str(srv.astra_id))
    from .shares import revoke_all
    from . import serverlog
    await serverlog.add(server_id, actor or tg_id, "delete", "" if do_refund else "без возврата")
    await revoke_all(ctx, server_id, "владелец удалил сервер")
    return refund


def paid_base(srv: Server) -> int:
    """База для возврата — реально оплаченное за 30 дней текущего срока (учитывает промокод и скидку за срок)."""
    paid = int(srv.period_paid_kop or 0)
    months = max(1.0, float(srv.period_months or 1.0))
    return min(srv.price_kop, int(paid / months)) if paid > 0 else srv.price_kop


def paid_months(srv: Server) -> float:
    return max(1.0, float(srv.period_months or 1.0))


def refund_amount(price_kop: int, expires_at, refund_pct: float, max_months: float = 1.0) -> int:
    """Возврат за неиспользованные дни: price_kop — оплачено за 30 дней, не больше оплаченного срока."""
    if not expires_at:
        return 0
    left_days = max(0.0, (expires_at - utcnow()).total_seconds() / 86400)
    return int(price_kop * min(left_days / 30.0, max(1.0, max_months)) * refund_pct / 100.0)


# ───────────────────────── платежи ─────────────────────────


def payment_providers(ctx: Ctx, cfg: dict) -> list[dict]:
    """Включённые и настроенные способы пополнения в порядке показа клиенту."""
    out = []
    if cfg.get("pay_yookassa_on", True) and ctx.yk.configured:
        mode = cfg.get("yk_method", "all")
        label = cfg.get("pay_yookassa_label") or ""
        if not label or label == "Карта или СБП · ЮKassa":  # стандартная подпись — по выбранному способу
            label = {"sbp": "СБП · ЮKassa", "bank_card": "Банковская карта · ЮKassa"}.get(mode, "Карта или СБП · ЮKassa")
        # для автоплатежей ЮKassa сохраняет карту или привязывает счёт СБП (если банк клиента это поддерживает)
        what = {"sbp": "счёт СБП", "bank_card": "карту"}.get(mode, "карту или счёт СБП")
        out.append({"code": "yookassa", "label": label, "autopay": True, "save_what": what, "sbp": mode != "bank_card"})
    if cfg.get("pay_platega_on") and ctx.platega is not None and ctx.platega.configured:
        out.append({"code": "platega", "label": cfg.get("pay_platega_label") or "Platega", "autopay": False})
    return out


def provider_client(ctx: Ctx, code: str | None):
    return ctx.platega if code == "platega" else ctx.yk


async def create_topup(ctx: Ctx, tg_id: int, amount_rub: int, save_method: bool = False, provider: str | None = None, source: str = "app") -> tuple[Payment, str]:
    async with session() as s:
        cfg = await load_settings(s)
    provs = payment_providers(ctx, cfg)
    if not provs:
        raise UserError("Оплата временно недоступна. Попробуйте позже.")
    prov = next((p for p in provs if p["code"] == provider), None) if provider else provs[0]
    if prov is None:
        raise UserError("Этот способ оплаты сейчас недоступен — выберите другой.")
    if not (cfg["min_topup_rub"] <= amount_rub <= cfg["max_topup_rub"]):
        raise UserError(f"Сумма пополнения: от {cfg['min_topup_rub']} до {cfg['max_topup_rub']} ₽.")
    save_method = bool(save_method and cfg["autopay_available"] and prov["autopay"])
    if prov["code"] == "platega":
        return await _create_platega_topup(ctx, tg_id, amount_rub)
    async with session() as s:
        pay = Payment(tg_id=tg_id, amount_kop=amount_rub * 100, save_method=save_method)
        s.add(pay)
        if save_method:  # согласие с Правилами автоплатежей — отметка при пополнении
            s.add(Consent(tg_id=tg_id, doc="autopay", version=cfg["docs_version"], source=source))
        await s.commit()
    mode = cfg.get("yk_method", "all")
    kw = dict(
        amount_kop=pay.amount_kop,
        description=f"Пополнение баланса #{pay.id}",
        return_url=env().public_url.rstrip("/") + "/paid",
        metadata={"payment_id": pay.id, "tg_id": tg_id},
        method=None if mode == "all" else mode,
    )
    try:
        data = await ctx.yk.create_payment(**kw, idem_key=f"topup-{pay.id}", save_payment_method=save_method)
    except YooKassaError as ex:
        if kw["method"] and not save_method:
            # способ не подключён в магазине — не теряем клиента: страница выбора ЮKassa
            log.warning("ЮKassa отклонила способ %s: %s", kw["method"], ex)
            await ctx.alert_once("yk_method", f"⚠️ ЮKassa не приняла платёж только через «{kw['method']}» ({ex.status}: {ex.description}). "
                                 "Клиентам показана обычная страница выбора способа. Проверьте, что способ подключён в магазине ЮKassa.")
            kw["method"] = None
            data = await ctx.yk.create_payment(**kw, idem_key=f"topup-{pay.id}-any")
            async with session() as s:
                row = await s.get(Payment, pay.id)
                row.yk_id = data["id"]
                await s.commit()
            return pay, data["confirmation"]["confirmation_url"]
        if not save_method:
            raise
        # магазину ещё не подключили автоплатежи — проводим обычный платёж
        log.warning("save_payment_method отклонён ЮKassa: %s", ex)
        await ctx.alert_once("yk_recurring", f"⚠️ ЮKassa не разрешает сохранять способ оплаты ({ex.status}: {ex.description}). Автоплатежи не работают — подключите их в ЮKassa (для СБП — отдельно «привязку счёта») или выключите в админке.")
        data = await ctx.yk.create_payment(**kw, idem_key=f"topup-{pay.id}-plain")
        async with session() as s:
            row = await s.get(Payment, pay.id)
            row.save_method = False
            await s.commit()
    async with session() as s:
        row = await s.get(Payment, pay.id)
        row.yk_id = data["id"]
        await s.commit()
    return pay, data["confirmation"]["confirmation_url"]


async def _create_platega_topup(ctx: Ctx, tg_id: int, amount_rub: int) -> tuple[Payment, str]:
    from .platega import PlategaError
    async with session() as s:
        pay = Payment(tg_id=tg_id, amount_kop=amount_rub * 100, provider="platega")
        s.add(pay)
        await s.commit()
    try:
        data = await ctx.platega.create_payment(amount_kop=pay.amount_kop, description=f"Пополнение баланса #{pay.id}",
                                                return_url=env().public_url.rstrip("/") + "/paid", metadata={"payment_id": pay.id, "tg_id": tg_id})
    except PlategaError as ex:
        async with session() as s:
            row = await s.get(Payment, pay.id)
            row.status, row.cancel_reason = "canceled", "create_failed"
            await s.commit()
        log.warning("platega create: %s", ex)
        await ctx.alert_once("platega_create", f"⚠️ Platega не создала платёж: {ex}")
        raise UserError("Не удалось создать платёж в Platega. Попробуйте другой способ оплаты или чуть позже.") from ex
    async with session() as s:
        row = await s.get(Payment, pay.id)
        row.yk_id = data["id"]
        await s.commit()
    return pay, data["confirmation"]["confirmation_url"]


REVOKED_REASONS = {"permission_revoked", "card_expired", "payment_method_restricted", "payment_method_limit_exceeded"}
CANCEL_TEXT = {
    "insufficient_funds": "на карте недостаточно средств",
    "card_expired": "истёк срок действия карты",
    "permission_revoked": "банк отозвал разрешение на автоплатежи",
    "payment_method_restricted": "карта заблокирована банком",
    "general_decline": "банк отклонил платёж",
}


def _method_fields(pm: dict) -> dict:
    card = pm.get("card") or {}
    last4 = card.get("last4")
    exp = f"{card.get('expiry_month', '')}/{str(card.get('expiry_year', ''))[-2:]}" if card.get("expiry_month") else None
    if pm.get("type") == "sbp":  # привязанный счёт СБП: карты нет, ЮKassa может прислать название банка в title
        title = pm.get("title") or "Счёт СБП"
    else:
        title = pm.get("title") or (f"{card.get('card_type', 'Карта')} *{last4}" if last4 else pm.get("type", "Способ оплаты"))
    bank = pm.get("payer_bank_details") or {}
    return {"type": pm.get("type", "bank_card"), "title": str(title)[:64], "card_type": card.get("card_type"), "last4": last4, "expiry": exp,
            "bank_id": (str(bank.get("bank_id")) if bank.get("bank_id") else None), "bank_bic": (str(bank.get("bic")) if bank.get("bic") else None),
            "bank_checked": True}


async def _save_method(s, tg_id: int, pm: dict) -> PaymentMethod:
    row = (await s.execute(select(PaymentMethod).where(PaymentMethod.tg_id == tg_id, PaymentMethod.yk_id == pm["id"]))).scalar_one_or_none()
    f = _method_fields(pm)
    if row is None:
        row = PaymentMethod(tg_id=tg_id, yk_id=pm["id"], **f)
        s.add(row)
    else:
        for k, v in f.items():
            setattr(row, k, v)
        row.active, row.deleted_at, row.deleted_by = True, None, None
    return row


async def fill_banks(ctx: Ctx, tg_id: int) -> None:
    """Счёт СБП без банка (привязан до того, как bcloud начал его запоминать, или банк не пришёл сразу): ищем банк
    в платеже, которым счёт привязали, — сначала по пометке, потом среди последних платежей с привязкой.
    Не нашли — повторим через 6 часов."""
    async with session() as s:
        todo = (await s.execute(select(PaymentMethod).where(PaymentMethod.tg_id == tg_id, PaymentMethod.active.is_(True),
                                                            PaymentMethod.type == "sbp", PaymentMethod.bank_id.is_(None)))).scalars().all()
        todo = [m for m in todo if not m.bank_checked_at or m.bank_checked_at < utcnow() - timedelta(hours=6)]
        if not todo or not ctx.yk.configured:
            return
        want = {m.yk_id: m for m in todo}
        marked = (await s.execute(select(Payment).where(Payment.tg_id == tg_id, Payment.method_yk_id.in_(list(want)), Payment.status == "succeeded",
                                                        Payment.yk_id.is_not(None)).order_by(Payment.id))).scalars().all()
        rest = (await s.execute(select(Payment).where(Payment.tg_id == tg_id, Payment.save_method.is_(True), Payment.status == "succeeded",
                                                      Payment.yk_id.is_not(None)).order_by(Payment.id.desc()).limit(60))).scalars().all()
        seen = set()
        for p in [*marked, *rest]:
            if not want:
                break
            if p.yk_id in seen or p.provider not in (None, "", "yookassa"):
                continue
            seen.add(p.yk_id)
            try:
                pm = (await ctx.yk.get_payment(p.yk_id)).get("payment_method") or {}
            except Exception:  # noqa: BLE001
                continue
            m = want.get(pm.get("id"))
            if m is None:
                continue
            f = _method_fields(pm)
            if f["bank_id"]:
                m.bank_id, m.bank_bic = f["bank_id"], f["bank_bic"]
                want.pop(m.yk_id)
            else:
                log.info("СБП банк: в платеже %s нет payer_bank_details (поля способа: %s)", p.yk_id, sorted(pm))
        for m in todo:
            m.bank_checked_at = utcnow()
        await s.commit()


async def topup_saved(ctx: Ctx, tg_id: int, amount_rub: int, method_id: int) -> dict:
    """Пополнение привязанным способом (карта или счёт СБП) без страницы оплаты. Вернёт url, если банк всё же
    попросит подтверждение, иначе status: succeeded | pending (итог придёт вебхуком)."""
    async with session() as s:
        cfg = await load_settings(s)
        m = await s.get(PaymentMethod, method_id)
        if m is None or m.tg_id != tg_id or not m.active:
            raise UserError("Этот способ оплаты больше не привязан — выберите другой.")
        if not ctx.yk.configured or not any(p["code"] == "yookassa" for p in payment_providers(ctx, cfg)):
            raise UserError("Оплата привязанным способом сейчас недоступна.")
        if not (cfg["min_topup_rub"] <= amount_rub <= cfg["max_topup_rub"]):
            raise UserError(f"Сумма пополнения: от {cfg['min_topup_rub']} до {cfg['max_topup_rub']} ₽.")
        recent = (await s.execute(select(func.count()).select_from(Payment).where(
            Payment.tg_id == tg_id, Payment.method_yk_id == m.yk_id, Payment.status == "pending",
            Payment.created_at > utcnow() - timedelta(minutes=10)))).scalar_one()
        if recent:
            raise UserError("Прошлый платёж этим способом ещё обрабатывается — подождите пару минут.")
        pay = Payment(tg_id=tg_id, amount_kop=amount_rub * 100, method_yk_id=m.yk_id, method_title=m.title)
        s.add(pay)
        await s.commit()
        yk_id, title = m.yk_id, m.title
    try:
        data = await ctx.yk.charge_saved(amount_kop=pay.amount_kop, method_id=yk_id, description=f"Пополнение баланса #{pay.id}",
                                         metadata={"payment_id": pay.id, "tg_id": tg_id}, idem_key=f"topup-saved-{pay.id}")
    except YooKassaError as ex:
        async with session() as s:
            row = await s.get(Payment, pay.id)
            row.status, row.cancel_reason = "canceled", "api_error"
            await s.commit()
        log.warning("оплата привязанным способом: %s", ex)
        raise UserError("Банк не принял платёж привязанным способом. Оплатите обычным способом.") from ex
    async with session() as s:
        row = await s.get(Payment, pay.id)
        row.yk_id = data.get("id")
        await s.commit()
    url = ((data.get("confirmation") or {}).get("confirmation_url"))
    if url:
        return {"url": url}
    if data.get("status") in ("succeeded", "canceled"):
        ok = await credit_payment(ctx, data["id"])
        if not ok and data.get("status") == "canceled":
            why = ((data.get("cancellation_details") or {}).get("reason")) or ""
            raise UserError("Платёж отклонён банком" + (f" ({why})" if why else "") + f". {title} — попробуйте другой способ.")
        return {"status": "succeeded"}
    return {"status": "pending"}


async def deactivate_method(tg_id: int, method_id: int | None, by: str) -> int:
    """Удаляет (забывает) способ оплаты; method_id=None — все. Если активных не осталось, выключает автоплатёж."""
    async with session() as s:
        q = select(PaymentMethod).where(PaymentMethod.tg_id == tg_id, PaymentMethod.active.is_(True))
        if method_id is not None:
            q = q.where(PaymentMethod.id == method_id)
        rows = (await s.execute(q)).scalars().all()
        for r in rows:
            r.active, r.deleted_at, r.deleted_by = False, utcnow(), by
        await s.flush()
        left = (await s.execute(select(func.count()).select_from(PaymentMethod).where(PaymentMethod.tg_id == tg_id, PaymentMethod.active.is_(True)))).scalar_one()
        if left == 0:
            u = await s.get(User, tg_id)
            if u:
                u.autopay_enabled = False
        await s.commit()
    return len(rows)


async def credit_payment(ctx: Ctx, yk_id: str) -> bool:
    """Идемпотентно проверяет платёж у провайдера (ЮKassa или Platega) и зачисляет. True — если зачислили сейчас."""
    async with session() as s:
        found = (await s.execute(select(Payment).where(Payment.yk_id == yk_id))).scalar_one_or_none()
    if found is None:
        return False
    data = await provider_client(ctx, found.provider).get_payment(yk_id)  # источник истины — API провайдера, а не тело уведомления
    async with session() as s:
        pay = (await s.execute(select(Payment).where(Payment.yk_id == yk_id))).scalar_one_or_none()
        if pay is None:
            return False
        if data.get("status") == "canceled":
            reason = (data.get("cancellation_details") or {}).get("reason")
            res = await s.execute(update(Payment).where(Payment.id == pay.id, Payment.status == "pending").values(status="canceled", cancel_reason=reason))
            await s.commit()
            if res.rowcount == 1 and pay.save_method and not pay.autopay:
                await ctx.notify(pay.tg_id, f"⚠️ <b>Пополнение не прошло</b>\n\nБанк не провёл платёж {fmt_rub(pay.amount_kop)} с привязкой счёта"
                                 + (f" ({CANCEL_TEXT.get(reason, reason)})" if reason else "") + ". Если счёт этого банка уже привязан, "
                                 "повторная привязка не пройдёт: оплатите им без перехода в банк или снимите галочку «Привязать».")
            if res.rowcount == 1 and pay.autopay:
                why = CANCEL_TEXT.get(reason or "", "платёж отклонён")
                if reason in REVOKED_REASONS:
                    await deactivate_method(pay.tg_id, None, "provider")
                    why += ". Карта удалена из профиля, автоплатежи выключены"
                await ctx.notify(pay.tg_id, f"⚠️ <b>Автоплатёж не прошёл</b>\n\nНе удалось списать <b>{fmt_rub(pay.amount_kop)}</b> с карты {html.escape(pay.method_title or '')}: {why}.\n\nПополните баланс вручную, чтобы сервер не остановился.")
            return False
        if not (data.get("status") == "succeeded" and data.get("paid")):
            return False
        paid_kop = int(round(float(data["amount"]["value"]) * 100))
        if paid_kop != pay.amount_kop or data["amount"].get("currency") != "RUB":
            await ctx.alert_admin(f"⚠️ Несовпадение суммы платежа {yk_id}: ожидали {pay.amount_kop}, пришло {data['amount']}")
            return False
        cfg = await load_settings(s)
        receipt = "pending" if (cfg["receipts_enabled"] and ctx.nalog.enabled) else "none"
        res = await s.execute(
            update(Payment).where(Payment.id == pay.id, Payment.status == "pending").values(status="succeeded", receipt_status=receipt)
        )
        if res.rowcount != 1:
            await s.rollback()
            return False
        saved_title = None
        pm = data.get("payment_method") or {}
        bind_failed = bool(pay.save_method and not pm.get("saved"))
        if pay.save_method and pm.get("saved") and pm.get("id"):
            m = await _save_method(s, pay.tg_id, pm)
            pay.method_yk_id = pay.method_yk_id or pm["id"]  # каким платежом привязан — по нему потом найдём банк
            saved_title = m.title
            if pm.get("type") == "sbp" and not m.bank_id:
                log.info("СБП банк: платёж %s без payer_bank_details (поля способа: %s)", yk_id, sorted(pm))
            u = await s.get(User, pay.tg_id)
            if u:
                u.autopay_enabled = True
        await change_balance(s, pay.tg_id, pay.amount_kop, "autopay" if pay.autopay else "topup", str(pay.id))  # коммитит всё
        tg_id, amount, autopay, title, pay_id = pay.tg_id, pay.amount_kop, pay.autopay, pay.method_title, pay.id
    if autopay:
        await ctx.notify(tg_id, f"💳 <b>Автоплатёж</b>\n\nСписали <b>{fmt_rub(amount)}</b> с карты {html.escape(title or '')} и зачислили на баланс — продлеваем сервер.")
        # сразу пробуем продлить, не дожидаясь следующего прохода
        async with session() as s:
            await s.execute(update(Server).where(Server.tg_id == tg_id).values(renew_try_at=None))
            await s.commit()
    else:
        msg = f"✅ <b>Баланс пополнен</b>\n\n💰 Зачислено: <b>{fmt_rub(amount)}</b>"
        if saved_title:
            msg += f"\n💳 {html.escape(saved_title)} привязан(а), автоплатежи включены. Отключить можно в личном кабинете."
        elif bind_failed:
            msg += "\nℹ️ Деньги зачислены, но банк не привязал счёт для автоплатежей — можно попробовать в другой раз или другим банком."
        await ctx.notify(tg_id, msg)
    from . import events, referral  # бонусы за приглашение — только с реальных оплат
    async with session() as s:
        payer = await s.get(User, tg_id)
    await events.emit(ctx, "payments", f"💳 <b>{'Автоплатёж' if autopay else 'Пополнение'} · {fmt_rub(amount)}</b>\n\n👤 {events.who(payer)}"
                      + (f"\n💳 {html.escape(title)}" if autopay and title else "") + (f"\n💾 Карта сохранена: {html.escape(saved_title)}" if saved_title else ""))
    await referral.reward(ctx, tg_id, amount, pay_id)
    if receipt == "pending":
        await process_receipts(ctx)
    if autopay:
        await autorenew_and_remind(ctx, only_tg=tg_id)
    return True


async def charge_autopay(ctx: Ctx, tg_id: int, need_kop: int, server_name: str) -> str:
    """Списывает недостающую сумму с сохранённой карты. Возвращает: done | pending | failed | skipped."""
    async with session() as s:
        cfg = await load_settings(s)
        user = await s.get(User, tg_id)
        if not (cfg["autopay_available"] and user and user.autopay_enabled and ctx.yk.configured):
            return "skipped"
        m = (await s.execute(select(PaymentMethod).where(PaymentMethod.tg_id == tg_id, PaymentMethod.active.is_(True)).order_by(PaymentMethod.id.desc()))).scalars().first()
        if m is None:
            return "skipped"
        # не чаще одного автоплатежа в 6 часов
        recent = (await s.execute(select(func.count()).select_from(Payment).where(
            Payment.tg_id == tg_id, Payment.autopay.is_(True), Payment.created_at > utcnow() - timedelta(hours=6)))).scalar_one()
        if recent:
            return "skipped"
        amount = max(need_kop, int(cfg["min_topup_rub"]) * 100)
        amount = math.ceil(amount / 100) * 100
        pay = Payment(tg_id=tg_id, amount_kop=amount, autopay=True, method_yk_id=m.yk_id, method_title=m.title)
        s.add(pay)
        await s.commit()
    try:
        data = await ctx.yk.charge_saved(
            amount_kop=amount, method_id=m.yk_id, description=f"Автоплатёж: продление {server_name}"[:128],
            metadata={"payment_id": pay.id, "tg_id": tg_id, "autopay": "1"}, idem_key=f"autopay-{pay.id}",
        )
    except Exception as ex:
        async with session() as s:
            row = await s.get(Payment, pay.id)
            row.status, row.cancel_reason = "canceled", "api_error"
            await s.commit()
        await ctx.alert_admin(f"❌ Автоплатёж клиента {tg_id} не создан: {ex}")
        return "failed"
    async with session() as s:
        row = await s.get(Payment, pay.id)
        row.yk_id = data.get("id")
        await s.commit()
    if data.get("status") in ("succeeded", "canceled"):
        ok = await credit_payment(ctx, data["id"])
        return "done" if ok else "failed"
    return "pending"


async def poll_pending_payments(ctx: Ctx) -> None:
    """Подстраховка на случай потерянного вебхука."""
    async with session() as s:
        rows = (
            await s.execute(
                select(Payment).where(Payment.status == "pending", Payment.yk_id.is_not(None), Payment.created_at > utcnow() - timedelta(days=2))
            )
        ).scalars().all()
    for p in rows:
        if provider_client(ctx, p.provider) is None or not provider_client(ctx, p.provider).configured:
            continue
        try:
            await credit_payment(ctx, p.yk_id)
        except Exception:
            log.warning("poll payment %s failed", p.yk_id, exc_info=True)


# ───────────────────────── чеки ─────────────────────────

MAX_RECEIPT_ATTEMPTS = 500  # ~ 40 часов при проходе раз в 5 минут


async def process_receipts(ctx: Ctx) -> None:
    if not ctx.nalog.enabled:
        return
    async with session() as s:
        cfg = await load_settings(s)
        rows = (await s.execute(select(Payment).where(Payment.receipt_status == "pending"))).scalars().all()
    for p in rows:
        try:
            uid, url = await ctx.nalog.create_receipt(cfg["receipt_name"], p.amount_kop / 100)
        except NalogError as ex:
            async with session() as s:
                row = await s.get(Payment, p.id)
                row.receipt_attempts += 1
                failed = row.receipt_attempts >= MAX_RECEIPT_ATTEMPTS
                if failed:
                    row.receipt_status = "failed"
                await s.commit()
                n = row.receipt_attempts
            if n == 3 or failed:
                await ctx.alert_admin(f"⚠️ Чек для платежа #{p.id} ({fmt_rub(p.amount_kop)}) не создаётся (попыток: {n}): {ex}")
            continue
        async with session() as s:
            row = await s.get(Payment, p.id)
            row.receipt_status, row.receipt_uuid, row.receipt_url = "done", uid, url
            await s.commit()
        await ctx.notify(p.tg_id, f"💳 <b>Чек за пополнение</b>\n{url}")


# ───────────────────────── синхронизация и автопродление ─────────────────────────


async def sync_servers(ctx: Ctx) -> None:
    try:
        remote = {r["id"]: r for r in await ctx.astra.servers()}
    except Exception:
        log.warning("sync: Astra недоступна", exc_info=True)
        return
    async with session() as s:
        rows = (await s.execute(select(Server).where(Server.status != "deleted"))).scalars().all()
        events: list[tuple[str, Server]] = []
        for srv in rows:
            r = remote.get(srv.astra_id)
            if r is None:
                # Astra не выдала сервер: услуга исчезает из списка, деньги реселлеру возвращаются. Сервер ни разу не
                # получил IP — значит, клиенту его так и не выдали (статус мог успеть смениться, например при опросе
                # GET /servers/{id}): это тоже неудачное создание, а не пропажа рабочего сервера, — деньги клиенту
                undelivered = srv.status == "provisioning" or not srv.ip
                if undelivered and (srv.status != "provisioning" or utcnow() - srv.created_at > timedelta(minutes=4)):
                    srv.status = "deleted"
                    srv.auto_renew = False
                    srv.deleted_at = utcnow()
                    events.append(("failed", srv))
                elif srv.status != "provisioning":
                    srv.status = "deleted"
                    srv.deleted_at = utcnow()
                    events.append(("vanished", srv))
                continue
            was = srv.status
            _apply_remote(srv, r)
            if was == "provisioning" and srv.status == "active":
                events.append(("ready", srv))
        await s.commit()
    for kind, srv in events:
        if kind == "ready":
            await ctx.notify(
                srv.tg_id,
                f"✅ <b>Сервер готов</b>\n\n🖥 <b>{html.escape(srv.name)}</b>\nIP: <code>{html.escape(str(srv.ip))}</code>\nЛогин: <code>root</code>\nПароль: <code>{html.escape(str(srv.root_password))}</code>\n\nУправление — в разделе «🖥 Мои серверы» или в личном кабинете.",
            )
        elif kind == "failed":
            if await _refunded(srv):
                continue
            await _refund(srv.tg_id, int(srv.period_paid_kop or 0) or paid_base(srv), "refund_failed", str(srv.astra_id))
            await ctx.notify(srv.tg_id, "❌ <b>Не удалось создать сервер</b>\n\nДеньги уже вернули на баланс. Попробуйте ещё раз или напишите в поддержку.")
        elif kind == "vanished":
            await ctx.alert_admin(f"⚠️ Сервер #{srv.astra_id} клиента {srv.tg_id} пропал из списка Astra.")
            from .shares import revoke_all
            await revoke_all(ctx, srv.id, "сервер удалён")


async def _refunded(srv: Server) -> bool:
    """За этот сервер уже был возврат (неудачное создание или отмена) — второй раз деньги не возвращаем."""
    async with session() as s:
        n = (await s.execute(select(func.count()).select_from(Tx).where(
            Tx.tg_id == srv.tg_id, Tx.ref == str(srv.astra_id), Tx.amount_kop > 0, Tx.kind.like("refund_%")))).scalar_one()
    return n > 0


async def refund_undelivered(ctx: Ctx, days: int = 14) -> int:
    """Разовая починка: серверы, которые Astra не выдала (без IP), удалены недавно, а деньги клиенту не вернулись
    (раньше такой сервер считался «пропавшим», а не неудачным). Возвращает число возвратов."""
    async with session() as s:
        rows = (await s.execute(select(Server).where(Server.status == "deleted", Server.ip.is_(None),
                                                     Server.deleted_at > utcnow() - timedelta(days=days)))).scalars().all()
    n = 0
    for srv in rows:
        if await _refunded(srv):
            continue
        kop = int(srv.period_paid_kop or 0)  # только реально оплаченное: бесплатные серверы не возвращаем
        if kop <= 0:
            continue
        await _refund(srv.tg_id, kop, "refund_failed", str(srv.astra_id))
        n += 1
        await ctx.notify(srv.tg_id, f"↩️ <b>Возврат за несозданный сервер</b>\n\nСервер «{html.escape(srv.name)}» не удалось создать — "
                                    f"{fmt_rub(kop)} вернули на баланс.")
        await ctx.alert_admin(f"↩️ Сервер #{srv.astra_id} клиента {srv.tg_id} не был выдан Astra — вернул клиенту {fmt_rub(kop)}.")
    return n


async def autorenew_and_remind(ctx: Ctx, only_tg: int | None = None) -> None:
    now = utcnow()
    async with session() as s:
        q = select(Server).where(Server.status.in_(["active", "suspended"]), Server.expires_at.is_not(None))
        if only_tg is not None:
            q = q.where(Server.tg_id == only_tg)
        rows = (await s.execute(q)).scalars().all()
    for srv in rows:
        left = srv.expires_at - now
        days_left = left.total_seconds() / 86400
        async with session() as s:
            user = await s.get(User, srv.tg_id)
        balance = user.balance_kop if user else 0
        can_pay = balance >= srv.price_kop

        if srv.auto_renew and left <= timedelta(hours=24):
            if srv.renew_try_at and now - srv.renew_try_at < timedelta(hours=3):
                continue
            async with session() as s:
                row = await s.get(Server, srv.id)
                row.renew_try_at = now
                await s.commit()
            if not can_pay:
                st = await charge_autopay(ctx, srv.tg_id, srv.price_kop - balance, srv.name)
                if st in ("done", "pending"):
                    continue  # продлим после зачисления
            if can_pay:
                try:
                    await renew_server(ctx, srv.id)
                    await ctx.notify(srv.tg_id, f"🔄 <b>Сервер продлён</b>\n\n🖥 {html.escape(srv.name)} — ещё 30 дней\n💳 Списано: <b>{fmt_rub(srv.price_kop)}</b>")
                except UserError as ex:
                    await ctx.notify(srv.tg_id, f"⚠️ <b>Автопродление не прошло</b>\n\n🖥 {html.escape(srv.name)}: {html.escape(str(ex))}")
                continue
        # напоминания, если автопродление не сработает
        if days_left <= 3 and (not srv.auto_renew or not can_pay):
            stage = 2 if days_left <= 1 else 1
            if srv.notified_stage < stage:
                why = "автопродление выключено" if not srv.auto_renew else f"на балансе не хватает средств ({fmt_rub(srv.price_kop)} в месяц)"
                await ctx.notify(srv.tg_id, f"⚠️ <b>Скоро закончится оплата</b>\n\n🖥 {html.escape(srv.name)} · <code>{html.escape(str(srv.ip))}</code>\nОсталось: <b>{max(0, int(days_left))} дн.</b> — {why}.\n\nПродлите сервер или пополните баланс.")
                async with session() as s:
                    row = await s.get(Server, srv.id)
                    row.notified_stage = stage
                    await s.commit()


async def watch_locations(ctx: Ctx) -> None:
    """Воркер: сообщает о новых, снятых и вернувшихся локациях Astra. В продажу они попадают сами —
    бот всегда показывает каталог из API; здесь только уведомление админу."""
    import json as _json
    from .db import Setting
    from . import events
    cat = await ctx.catalog(fresh=True)
    async with session() as s:
        cfg = await load_settings(s)
        row = await s.get(Setting, "known_locations")
    hidden = set(cfg["hidden_presets"])
    now: dict[str, dict] = {}
    for loc in cat.get("locations", []) or []:
        code = loc.get("code")
        if not code:
            continue
        n_offers = sum(1 for p in cat.get("presets", []) if p["idx"] not in hidden and p.get("price_month_rub", {}).get(code) is not None)
        prices = [p["price_month_rub"][code] for p in cat.get("presets", []) if p["idx"] not in hidden and p.get("price_month_rub", {}).get(code) is not None]
        now[code] = {"name": loc.get("name") or code.upper(), "on": bool(loc.get("available", True)) and n_offers > 0,
                     "offers": n_offers, "from": min(prices) if prices else None, "channel": loc.get("channel") or "",
                     "description": loc.get("description") or ""}
    try:
        known = _json.loads(row.value) if row else None
    except ValueError:
        known = None
    async with session() as s:
        r = await s.get(Setting, "known_locations")
        val = _json.dumps({k: v["on"] for k, v in now.items()})
        if r is None:
            s.add(Setting(key="known_locations", value=val))
        else:
            r.value = val
        await s.commit()
    from . import locwatch
    await locwatch.track({c: v["on"] for c, v in now.items()})
    if known is None:  # первый запуск — просто запоминаем
        await _announced_init({c for c, v in now.items() if v["on"]})
        return
    await _announced_init({c for c, v in now.items() if v["on"] and known.get(c)})
    for code, v in now.items():
        name = f"{flag(code)} {html.escape(v['name'])}"
        if code not in known:
            if v["on"]:
                await _announce_queue(code)
                price = fmt_rub(sell_price_kop(v["from"], cfg)) if v["from"] is not None else "—"
                await events.emit(ctx, "locations", f"🆕 <b>Новая локация в продаже: {name}</b>\n\nТарифов: {v['offers']}, цена клиенту от {price} в месяц"
                                  + (f"\nКанал: {html.escape(v['channel'])}" if v["channel"] else "")
                                  + (f"\n📍 {html.escape(v['description'])}" if v.get("description") else "")
                                  + "\n\nУже доступна в боте и кабинете. Проверьте наценку в «Тарифы и маржа».")
            else:
                await events.emit(ctx, "locations", f"🆕 <b>У Astra появилась локация {name}</b>\n\nПока без доступных тарифов — в продаже появится автоматически, как только Astra откроет заказы.")
        elif v["on"] and not known.get(code):
            await _announce_queue(code)  # объявляем, только если о ней ещё не сообщали (первое открытие продаж)
            await events.emit(ctx, "locations", f"✅ <b>Локация {name} снова в продаже</b> — тарифов: {v['offers']}.")
        elif not v["on"] and known.get(code):
            await events.emit(ctx, "locations", f"⚠️ <b>Локация {name} недоступна</b>\n\nAstra закрыла заказы или убрала тарифы — клиенты её не видят. Текущие серверы работают.")
    for code in known:
        if code not in now and known[code]:
            await events.emit(ctx, "locations", f"⚠️ <b>Локация {flag(code)} {code.upper()} пропала из каталога Astra</b>\n\nКлиенты её больше не видят. Текущие серверы работают.")


# ── рассылка клиентам о новой локации ──
# Объявляем каждую страну один раз — при первом открытии продаж. Если в этот момент идёт другая
# рассылка, ждём в очереди до следующей проверки каталога.

async def _setting_json(key: str, default):
    import json as _json
    from .db import Setting
    async with session() as s:
        row = await s.get(Setting, key)
    try:
        return _json.loads(row.value) if row else default
    except ValueError:
        return default


async def _setting_put(key: str, value) -> None:
    import json as _json
    from .db import Setting
    async with session() as s:
        row = await s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=_json.dumps(value)))
        else:
            row.value = _json.dumps(value)
        await s.commit()


async def _announced_init(codes: set[str]) -> None:
    """Локации, которые уже были в продаже до появления функции, не объявляем задним числом."""
    if await _setting_json("announced_locations", None) is None:
        await _setting_put("announced_locations", sorted(codes))


async def _announce_queue(code: str) -> None:
    done = set(await _setting_json("announced_locations", []))
    pend = list(await _setting_json("loc_announce_pending", []))
    if code not in done and code not in pend:
        pend.append(code)
        await _setting_put("loc_announce_pending", pend)


def location_announce_text(code: str, loc: dict, price_kop: int | None) -> str:
    name = clean_name(loc.get("name", "")) or code.upper()
    lines = [f"🆕 <b>Новая локация — {flag(code)} {html.escape(name)}</b>", ""]
    if price_kop:
        lines.append(f"Серверы от <b>{fmt_rub(price_kop)}</b> в месяц, выдача за 1–3 минуты.")
    if loc.get("description"):
        lines.append(f"📍 {html.escape(loc['description'])}")
    sp = channel_speed(loc.get("channel", ""))
    if sp:
        lines.append(f"🌐 Канал до {html.escape(sp)}")
    return "\n".join(lines)


async def announce_locations(ctx: Ctx) -> None:
    """Отправляет ожидающие объявления о новых локациях — рассылкой «Новости» (только с согласием на рекламу)."""
    from . import broadcasts, events
    pend = list(await _setting_json("loc_announce_pending", []))
    if not pend:
        return
    async with session() as s:
        cfg = await load_settings(s)
    if not cfg.get("loc_announce", True):
        await _setting_put("loc_announce_pending", [])
        return
    cat = await ctx.catalog()
    done = set(await _setting_json("announced_locations", []))
    for code in list(pend):
        loc = next((x for x in cat.get("locations", []) if x.get("code") == code), None)
        offs = await offers(ctx, code) if loc and loc.get("available", True) else []
        if not offs:  # уже закрыли — объявлять нечего, объявим при следующем открытии
            pend.remove(code)
            continue
        text = location_announce_text(code, loc, min(o["price_kop"] for o in offs))
        bot = getattr(ctx, "bot_username", "") or ""
        try:
            b = await broadcasts.create(ctx, 0, text, "all", "news", "🛒 Собрать сервер" if bot else "",
                                        f"https://t.me/{bot}?start=loc_{code}" if bot else "")
        except broadcasts.BroadcastError as ex:
            if "другая рассылка" in str(ex):
                break  # подождём окончания текущей
            pend.remove(code)
            done.add(code)
            await events.emit(ctx, "locations", f"ℹ️ Рассылку о локации {flag(code)} {code.upper()} не отправили: {html.escape(str(ex))}")
            continue
        pend.remove(code)
        done.add(code)
        # кто просил «сообщить, когда появится» и получит эту рассылку — второе сообщение не нужно
        from .db import LocationWatch
        from sqlalchemy import delete as _del
        async with session() as s:
            await s.execute(_del(LocationWatch).where(LocationWatch.code == code, LocationWatch.tg_id.in_(
                select(User.tg_id).where(User.ads_consent.is_(True), User.consent_version > 0, User.banned.is_(False), User.bot_blocked.is_(False)))))
            await s.commit()
        await events.emit(ctx, "locations", f"📣 <b>Клиентам ушла рассылка о локации {flag(code)} {html.escape(clean_name(loc.get('name', '')) or code.upper())}</b>\n\n"
                                            f"Получателей: {b.total} (с согласием на рекламу).")
    await _setting_put("loc_announce_pending", pend)
    await _setting_put("announced_locations", sorted(done))


async def check_astra_balance(ctx: Ctx) -> None:
    try:
        b = await ctx.astra.balance()
    except Exception:
        return
    async with session() as s:
        cfg = await load_settings(s)
    if float(b.get("balance_rub", 0)) < cfg["low_balance_rub"]:
        await ctx.alert_once("astra_low", f"⚠️ Баланс Astra: {b.get('balance_rub')} ₽ (порог {cfg['low_balance_rub']} ₽). Пополните через @astravpsbot — иначе покупки и автопродления остановятся.")
