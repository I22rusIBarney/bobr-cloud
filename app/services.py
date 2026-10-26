"""Бизнес-логика: покупка, продление, платежи, чеки, синхронизация с Astra."""
from __future__ import annotations

import html
import math
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select, update

from .astra import Astra, AstraError, AstraUncertain, parse_dt
from .config import env
from .db import Consent, Payment, PaymentMethod, Server, User, change_balance, session, utcnow
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
    # сообщение с кнопкой, открывающей мини-приложение: (tg_id, text, button_text, url)
    notify_btn: Callable[[int, str, str, str], Awaitable[None]] | None = None
    # проверка подписки: (chat, tg_id) → True/False, None — проверить нельзя
    check_member: Callable[[Any, int], Awaitable[bool | None]] | None = None
    # проверка канала для админки: chat → {ok, title, link, error}
    probe_chat: Callable[[Any], Awaitable[dict]] | None = None
    # отправка в произвольный чат (канал событий): → None или текст ошибки
    post: Callable[[Any, str], Awaitable[str | None]] | None = None
    tg_ping: Callable[[], Awaitable[Any]] | None = None
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
    "rockylinux": "Rocky Linux", "rocky": "Rocky Linux", "fedora": "Fedora", "windows": "Windows", "alpine": "Alpine",
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


async def offers(ctx: Ctx, location: str, tg_id: int | None = None) -> list[dict]:
    """Тарифы для локации с ценой клиенту (с учётом персонального «без наценки»)."""
    cat = await ctx.catalog()
    async with session() as s:
        cfg = await load_settings(s)
        u = await s.get(User, tg_id) if tg_id else None
    cfg = user_price_cfg(cfg, u)
    out = []
    for p in cat.get("presets", []):
        if p["idx"] in cfg["hidden_presets"]:
            continue
        cost = p.get("price_month_rub", {}).get(location)
        if cost is None:
            continue
        out.append(
            {
                "idx": p["idx"],
                "cpu": p["cpu"],
                "ram": p["ram"],
                "disk": p["disk"],
                "cost_rub": cost,
                "price_kop": sell_price_kop(cost, cfg),
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


async def buy_server(ctx: Ctx, tg_id: int, idx: int, location: str, os_: str, password: str | None = None, period: str = "1m") -> Server:
    if password:
        password = check_password(password)
    async with session() as s:
        cfg = await load_settings(s)
        user = await s.get(User, tg_id)
        if user is None or user.banned:
            raise UserError("Доступ ограничен.")
        if cfg["maintenance"]:
            raise UserError("Продажи временно приостановлены. Попробуйте позже.")
        if idx in cfg["hidden_presets"]:
            raise UserError("Этот тариф недоступен.")

        cat = await ctx.catalog(fresh=True)
        preset = next((p for p in cat.get("presets", []) if p["idx"] == idx), None)
        loc = next((x for x in cat.get("locations", []) if x["code"] == location), None)
        if not preset or not loc or not loc.get("available", True):
            raise UserError("Тариф или локация недоступны.")
        cost_rub = preset["price_month_rub"].get(location)
        if cost_rub is None:
            raise UserError("В этой локации тариф недоступен.")
        if os_ not in cat.get("os", []):
            raise UserError("Эта ОС недоступна.")

        opt = next((o for o in period_options(cfg, cat) if o["code"] == period), None)
        if opt is None:
            raise UserError("Этот срок аренды недоступен.")
        months = opt["months"]
        price = sell_price_kop(cost_rub, user_price_cfg(cfg, user))  # цена месяца = цена продления
        discount_pct = max(0, min(100, int(user.promo_discount_pct or 0)))
        # промокод — скидка на первый месяц, скидка за срок — на весь срок
        charge = max(0, period_price(price, months, opt["pct"]) - (price - apply_discount(price, discount_pct)))
        if not await change_balance(s, tg_id, -charge, "purchase", f"{location}:{idx}"):
            raise UserError("Недостаточно средств на балансе. Пополните баланс и повторите.")

    name = f"vps-{secrets.token_hex(3)}"
    try:
        resp = await ctx.astra.create(idx=idx, location=location, os=os_, name=name, period=period, password=password or None)
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
        cost_kop=cost_kop(charged / months) if charged is not None else cost_kop(cost_rub),
        status="provisioning",
        expires_at=utcnow() + timedelta(days=int(resp.get("expires_in_days", 30 * months))),
        auto_renew=cfg["auto_renew_default"],
    )
    async with session() as s:
        s.add(srv)
        u = await s.get(User, tg_id)
        if discount_pct:
            u.promo_discount_pct, u.promo_code = 0, None
        await s.commit()
    from . import events
    term = {1: "1 мес", 3: "3 мес", 6: "6 мес", 12: "1 год"}.get(months, f"{months} мес")
    await events.emit(ctx, "sales", f"🛒 <b>Покупка · {fmt_rub(charge)}</b>\n\n👤 {events.who(u)}\n🖥 {flag(location)} {srv.cpu} vCPU · {srv.ram} ГБ · {srv.disk} ГБ · {html.escape(os_label(srv.os))}\n"
                      f"🔄 {term}" + (f" · промокод −{discount_pct}%" if discount_pct else "") + f"\n💰 Маржа в месяц: {fmt_rub(price - srv.cost_kop)}")
    return srv


async def _refund(tg_id: int, kop: int, kind: str, ref: str) -> None:
    async with session() as s:
        await change_balance(s, tg_id, kop, kind, ref)


# ───────────────────────── продление ─────────────────────────


async def renew_server(ctx: Ctx, server_id: int) -> Server:
    async with session() as s:
        srv = await s.get(Server, server_id)
        if srv is None or srv.status in ("deleted", "provisioning"):
            raise UserError("Сервер недоступен для продления.")
        price = srv.price_kop
        if not await change_balance(s, srv.tg_id, -price, "renew", str(srv.astra_id)):
            raise UserError("Недостаточно средств для продления.")
    try:
        await ctx.astra.action(srv.astra_id, "renew")
    except AstraError as ex:
        await _refund(srv.tg_id, price, "refund_renew", str(srv.astra_id))
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


# ───────────────────────── действия над сервером ─────────────────────────


async def get_user_server(s, tg_id: int, server_id: int) -> Server:
    srv = await s.get(Server, server_id)
    if srv is None or srv.tg_id != tg_id or srv.status == "deleted":
        raise UserError("Сервер не найден.")
    return srv


async def server_action(ctx: Ctx, tg_id: int, server_id: int, action: str, body: dict | None = None) -> dict:
    """start | stop | reboot | reinstall | password | vnc | change-ip."""
    async with session() as s:
        srv = await get_user_server(s, tg_id, server_id)
        cfg = await load_settings(s)
        if srv.status not in ("active", "suspended"):
            raise UserError("Сервер сейчас недоступен для этого действия.")
        charge = 0
        if action == "change-ip":
            charge = int(cfg["change_ip_price_rub"]) * 100
            if not await change_balance(s, tg_id, -charge, "change_ip", str(srv.astra_id)):
                raise UserError(f"Для смены IP нужно {fmt_rub(charge)} на балансе.")
    try:
        resp = await ctx.astra.action(srv.astra_id, action, body)
    except AstraError as ex:
        if charge:
            await _refund(tg_id, charge, "refund_change_ip", str(srv.astra_id))
        raise UserError(f"Не удалось выполнить действие ({ex.code or ex.status}).") from ex
    except AstraUncertain as ex:
        await ctx.alert_admin(f"⚠️ Неизвестный итог действия {action} #{srv.astra_id} (таймаут).")
        raise UserError("Ответ от провайдера не получен. Проверьте статус сервера через минуту.") from ex
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
    return name


async def server_live(ctx: Ctx, tg_id: int, server_id: int) -> dict:
    """Актуальное состояние из Astra: питание (running/stopped) и готовый текст подключения."""
    async with session() as s:
        srv = await get_user_server(s, tg_id, server_id)
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
        u = await s.get(User, tg_id)
        name = row.name
    from . import events
    await events.emit(ctx, "sales", f"⬆️ <b>Апгрейд · {fmt_rub(final)}</b>\n\n👤 {events.who(u)}\n🖥 {html.escape(name)}: {cpu} vCPU · {ram} ГБ\n💳 Продление теперь {fmt_rub(new_price)}")
    return {"charged_kop": final, "new_price_kop": new_price, "cpu": cpu, "ram": ram}


async def cancel_server(ctx: Ctx, tg_id: int, server_id: int) -> int:
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
    refund = refund_amount(paid_base(srv), srv.expires_at, cfg["refund_pct"], paid_months(srv))
    async with session() as s:
        row = await s.get(Server, server_id)
        row.status = "deleted"
        row.auto_renew = False
        await s.commit()
        if refund > 0:
            await change_balance(s, tg_id, refund, "refund_cancel", str(srv.astra_id))
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


async def create_topup(ctx: Ctx, tg_id: int, amount_rub: int, save_method: bool = False) -> tuple[Payment, str]:
    if not ctx.yk.configured:
        raise UserError("Оплата временно недоступна. Попробуйте позже.")
    async with session() as s:
        cfg = await load_settings(s)
    if not (cfg["min_topup_rub"] <= amount_rub <= cfg["max_topup_rub"]):
        raise UserError(f"Сумма пополнения: от {cfg['min_topup_rub']} до {cfg['max_topup_rub']} ₽.")
    save_method = bool(save_method and cfg["autopay_available"])
    async with session() as s:
        pay = Payment(tg_id=tg_id, amount_kop=amount_rub * 100, save_method=save_method)
        s.add(pay)
        if save_method:  # согласие с Правилами автоплатежей — отметка при пополнении
            s.add(Consent(tg_id=tg_id, doc="autopay", version=cfg["docs_version"], source="app"))
        await s.commit()
    kw = dict(
        amount_kop=pay.amount_kop,
        description=f"Пополнение баланса #{pay.id}",
        return_url=env().public_url.rstrip("/") + "/paid",
        metadata={"payment_id": pay.id, "tg_id": tg_id},
    )
    try:
        data = await ctx.yk.create_payment(**kw, idem_key=f"topup-{pay.id}", save_payment_method=save_method)
    except YooKassaError as ex:
        if not save_method:
            raise
        # магазину ещё не подключили автоплатежи — проводим обычный платёж
        log.warning("save_payment_method отклонён ЮKassa: %s", ex)
        await ctx.alert_once("yk_recurring", f"⚠️ ЮKassa не разрешает сохранять карты ({ex.status}: {ex.description}). Автоплатежи не работают — подключите их в ЮKassa или выключите в админке.")
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
    title = pm.get("title") or (f"{card.get('card_type', 'Карта')} *{last4}" if last4 else pm.get("type", "Способ оплаты"))
    return {"type": pm.get("type", "bank_card"), "title": str(title)[:64], "card_type": card.get("card_type"), "last4": last4, "expiry": exp}


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
    """Идемпотентно проверяет платёж в ЮKassa и зачисляет. True — если зачислили сейчас."""
    data = await ctx.yk.get_payment(yk_id)  # источник истины — API, а не тело вебхука
    async with session() as s:
        pay = (await s.execute(select(Payment).where(Payment.yk_id == yk_id))).scalar_one_or_none()
        if pay is None:
            return False
        if data.get("status") == "canceled":
            reason = (data.get("cancellation_details") or {}).get("reason")
            res = await s.execute(update(Payment).where(Payment.id == pay.id, Payment.status == "pending").values(status="canceled", cancel_reason=reason))
            await s.commit()
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
        if pay.save_method and pm.get("saved") and pm.get("id"):
            m = await _save_method(s, pay.tg_id, pm)
            saved_title = m.title
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
            msg += f"\n💳 Карта {html.escape(saved_title)} сохранена, автоплатежи включены. Отключить можно в личном кабинете."
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
                if srv.status == "provisioning" and utcnow() - srv.created_at > timedelta(minutes=4):
                    srv.status = "deleted"
                    srv.auto_renew = False
                    events.append(("failed", srv))
                elif srv.status != "provisioning":
                    srv.status = "deleted"
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
            await _refund(srv.tg_id, int(srv.period_paid_kop or 0) or paid_base(srv), "refund_failed", str(srv.astra_id))
            await ctx.notify(srv.tg_id, "❌ <b>Не удалось создать сервер</b>\n\nДеньги уже вернули на баланс. Попробуйте ещё раз или напишите в поддержку.")
        elif kind == "vanished":
            await ctx.alert_admin(f"⚠️ Сервер #{srv.astra_id} клиента {srv.tg_id} пропал из списка Astra.")


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


async def check_astra_balance(ctx: Ctx) -> None:
    try:
        b = await ctx.astra.balance()
    except Exception:
        return
    async with session() as s:
        cfg = await load_settings(s)
    if float(b.get("balance_rub", 0)) < cfg["low_balance_rub"]:
        await ctx.alert_once("astra_low", f"⚠️ Баланс Astra: {b.get('balance_rub')} ₽ (порог {cfg['low_balance_rub']} ₽). Пополните через @astravpsbot — иначе покупки и автопродления остановятся.")
