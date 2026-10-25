"""Аккаунт клиента: согласия с документами, промокоды, история операций, аватарка."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, or_, select, update

from .config import env
from .db import Consent, Payment, PaymentMethod, Promo, PromoUse, Server, Tx, User, change_balance, session, utcnow
from .legal import REQUIRED
from . import referral
from .services import UserError
from .settings import fmt_rub, load_settings

log = logging.getLogger(__name__)


# ───────────────────────── согласия ─────────────────────────


async def needs_consent(tg_id: int) -> bool:
    async with session() as s:
        cfg = await load_settings(s)
        u = await s.get(User, tg_id)
    return u is None or (u.consent_version or 0) < int(cfg["docs_version"])


async def accept_consent(tg_id: int, source: str, docs: list[str] | None = None) -> int:
    docs = list(docs or REQUIRED)
    missing = [d for d in REQUIRED if d not in docs]
    if missing:
        raise UserError("Отметьте все пункты, чтобы продолжить.")
    async with session() as s:
        cfg = await load_settings(s)
        ver = int(cfg["docs_version"])
        u = await s.get(User, tg_id)
        if u is None:
            raise UserError("Пользователь не найден. Откройте бота заново.")
        for d in REQUIRED:
            s.add(Consent(tg_id=tg_id, doc=d, version=ver, source=source))
        u.consent_version, u.consent_at = ver, utcnow()
        await s.commit()
    return ver


async def revoke_consent(tg_id: int, source: str) -> None:
    """Отзыв согласия на обработку ПД (152-ФЗ, ст. 9 ч. 2): доступ закрывается, админ получает задачу удалить данные."""
    async with session() as s:
        cfg = await load_settings(s)
        u = await s.get(User, tg_id)
        if u is None:
            return
        s.add(Consent(tg_id=tg_id, doc="pd", version=int(cfg["docs_version"]), source=source, accepted=False))
        u.consent_version, u.autopay_enabled = 0, False
        await s.commit()


# ───────────────────────── промокоды ─────────────────────────

CODE_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")


def norm_code(code: str) -> str:
    return (code or "").strip().upper().replace(" ", "")


def promo_dict(p: Promo) -> dict:
    return {
        "id": p.id, "code": p.code, "kind": p.kind, "value": p.value, "max_uses": p.max_uses, "used": p.used,
        "expires_at": p.expires_at.isoformat() if p.expires_at else None, "new_users_only": p.new_users_only,
        "active": p.active, "note": p.note, "created_at": p.created_at.isoformat(),
    }


def validate_promo_fields(kind: str, value: int, max_uses: int) -> None:
    if kind not in ("bonus", "discount"):
        raise UserError("Тип промокода: бонус на баланс или скидка.")
    if kind == "bonus" and not (1 <= value <= 100000):
        raise UserError("Бонус: от 1 до 100 000 ₽.")
    if kind == "discount" and not (1 <= value <= 90):
        raise UserError("Скидка: от 1 до 90%.")
    if max_uses < 0:
        raise UserError("Лимит активаций не может быть отрицательным.")


async def create_promo(code: str, kind: str, value: int, max_uses: int = 0, days: int | None = None,
                       new_users_only: bool = False, note: str = "") -> Promo:
    code = norm_code(code)
    if not CODE_RE.match(code):
        raise UserError("Код: 3–32 символа, латиница, цифры, «-» и «_».")
    validate_promo_fields(kind, value, max_uses)
    async with session() as s:
        if (await s.execute(select(Promo).where(Promo.code == code))).scalar_one_or_none():
            raise UserError("Такой код уже есть.")
        p = Promo(code=code, kind=kind, value=int(value), max_uses=int(max_uses), new_users_only=new_users_only,
                  note=(note or "")[:120], expires_at=utcnow() + timedelta(days=days) if days else None)
        s.add(p)
        await s.commit()
        return p


async def apply_promo(tg_id: int, code: str) -> dict:
    code = norm_code(code)
    if not code:
        raise UserError("Введите промокод.")
    async with session() as s:
        p = (await s.execute(select(Promo).where(Promo.code == code))).scalar_one_or_none()
        if p is None or not p.active:
            raise UserError("Такого промокода нет или он больше не действует.")
        if p.expires_at and p.expires_at < utcnow():
            raise UserError("Срок действия промокода истёк.")
        if (await s.execute(select(PromoUse).where(PromoUse.promo_id == p.id, PromoUse.tg_id == tg_id))).scalar_one_or_none():
            raise UserError("Вы уже активировали этот промокод.")
        u = await s.get(User, tg_id)
        if p.new_users_only:
            had = (await s.execute(select(func.count()).select_from(Server).where(Server.tg_id == tg_id))).scalar_one()
            paid = (await s.execute(select(func.count()).select_from(Payment).where(Payment.tg_id == tg_id, Payment.status == "succeeded"))).scalar_one()
            if had or paid:
                raise UserError("Этот промокод только для новых клиентов.")
        if p.kind == "discount" and u.promo_discount_pct:
            raise UserError(f"У вас уже есть скидка {u.promo_discount_pct}% на следующую покупку. Используйте её, затем вводите новый код.")
        # атомарно занимаем активацию с учётом лимита
        cond = [Promo.id == p.id]
        if p.max_uses:
            cond.append(Promo.used < p.max_uses)
        res = await s.execute(update(Promo).where(*cond).values(used=Promo.used + 1))
        if res.rowcount != 1:
            await s.rollback()
            raise UserError("Промокод закончился.")
        s.add(PromoUse(promo_id=p.id, tg_id=tg_id))
        if p.kind == "discount":
            u.promo_discount_pct, u.promo_code = p.value, p.code
            await s.commit()
            return {"kind": "discount", "value": p.value, "message": f"Скидка {p.value}% применится к следующей покупке сервера."}
        await s.flush()
        await change_balance(s, tg_id, p.value * 100, "promo", p.code)  # коммитит всё
        return {"kind": "bonus", "value": p.value, "message": f"На баланс зачислено {fmt_rub(p.value * 100)}."}


# ───────────────────────── история операций ─────────────────────────

TX_LABEL = {
    "topup": "Пополнение", "autopay": "Автоплатёж", "purchase": "Покупка сервера", "renew": "Продление",
    "upgrade": "Увеличение мощности", "upgrade_settle": "Перерасчёт апгрейда", "change_ip": "Смена IP",
    "promo": "Промокод", "admin_adjust": "Корректировка", "refund_cancel": "Возврат за удаление",
    "refund_create": "Возврат: сервер не создан", "refund_failed": "Возврат: сервер не создан",
    "refund_renew": "Возврат: продление не прошло", "refund_change_ip": "Возврат: смена IP", "refund_upgrade": "Возврат: апгрейд",
    "ref_bonus": "Бонус за друга", "ref_welcome": "Бонус за приглашение",
}


async def history(tg_id: int, limit: int = 100) -> list[dict]:
    async with session() as s:
        txs = (await s.execute(select(Tx).where(Tx.tg_id == tg_id).order_by(Tx.id.desc()).limit(limit))).scalars().all()
        servers = {str(x.astra_id): x.name for x in (await s.execute(select(Server).where(Server.tg_id == tg_id))).scalars()}
        pays = {str(p.id): p for p in (await s.execute(select(Payment).where(Payment.tg_id == tg_id))).scalars()}
    out = []
    for t in txs:
        item = {"id": t.id, "kind": t.kind, "title": TX_LABEL.get(t.kind, t.kind), "amount_rub": t.amount_kop / 100,
                "created": t.created_at.isoformat(), "detail": "", "receipt_url": None}
        if t.ref in servers:
            item["detail"] = servers[t.ref]
        elif t.kind in ("topup", "autopay") and t.ref in pays:
            p = pays[t.ref]
            item["detail"] = p.method_title or ""
            item["receipt_url"] = p.receipt_url
        elif t.kind == "promo":
            item["detail"] = t.ref
        elif t.kind == "purchase":
            item["detail"] = t.ref.split(":")[0].upper() if ":" in t.ref else ""
        out.append(item)
    return out


async def methods(tg_id: int, include_deleted: bool = False) -> list[dict]:
    async with session() as s:
        q = select(PaymentMethod).where(PaymentMethod.tg_id == tg_id).order_by(PaymentMethod.id.desc())
        if not include_deleted:
            q = q.where(PaymentMethod.active.is_(True))
        rows = (await s.execute(q)).scalars().all()
    return [{"id": m.id, "title": m.title, "type": m.type, "card_type": m.card_type, "last4": m.last4, "expiry": m.expiry,
             "active": m.active, "created": m.created_at.isoformat(), "deleted_at": m.deleted_at.isoformat() if m.deleted_at else None,
             "deleted_by": m.deleted_by} for m in rows]


async def set_autopay(tg_id: int, enabled: bool) -> bool:
    async with session() as s:
        cfg = await load_settings(s)
        u = await s.get(User, tg_id)
        if enabled:
            if not cfg["autopay_available"]:
                raise UserError("Автоплатежи пока недоступны.")
            n = (await s.execute(select(func.count()).select_from(PaymentMethod).where(PaymentMethod.tg_id == tg_id, PaymentMethod.active.is_(True)))).scalar_one()
            if not n:
                raise UserError("Сначала сохраните карту: при пополнении отметьте «Сохранить карту для автоплатежей».")
        u.autopay_enabled = bool(enabled)
        await s.commit()
    return bool(enabled)


# ───────────────────────── аватарки из Telegram ─────────────────────────

_AVA: dict[int, tuple[float, bytes | None, str]] = {}
AVA_TTL, AVA_MISS_TTL, AVA_MAX = 6 * 3600, 3600, 1000


async def avatar(tg_id: int) -> tuple[bytes, str] | None:
    hit = _AVA.get(tg_id)
    if hit and time.time() - hit[0] < (AVA_TTL if hit[1] else AVA_MISS_TTL):
        return (hit[1], hit[2]) if hit[1] else None
    data, ctype = None, "image/jpeg"
    token = env().bot_token
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.telegram.org/bot{token}/getUserProfilePhotos", params={"user_id": tg_id, "limit": 1})
            photos = r.json().get("result", {}).get("photos") or []
            if photos:
                sizes = sorted(photos[0], key=lambda x: x.get("width", 0))
                pick = next((x for x in sizes if x.get("width", 0) >= 160), sizes[-1])
                f = (await c.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": pick["file_id"]})).json()
                path = f.get("result", {}).get("file_path")
                if path:
                    img = await c.get(f"https://api.telegram.org/file/bot{token}/{path}")
                    if img.status_code == 200 and len(img.content) < 2_000_000:
                        data = img.content
                        ctype = "image/png" if path.endswith(".png") else "image/jpeg"
    except Exception:
        log.debug("avatar %s failed", tg_id, exc_info=True)
    if len(_AVA) >= AVA_MAX:
        for k in sorted(_AVA, key=lambda k: _AVA[k][0])[: AVA_MAX // 4]:
            _AVA.pop(k, None)
    _AVA[tg_id] = (time.time(), data, ctype)
    return (data, ctype) if data else None


# ───────────────────────── для админки ─────────────────────────


async def client_card(tg_id: int) -> dict:
    async with session() as s:
        u = await s.get(User, tg_id)
        if u is None:
            raise UserError("Клиент не найден.")
        servers = (await s.execute(select(Server).where(Server.tg_id == tg_id).order_by(Server.id.desc()))).scalars().all()
        pays = (await s.execute(select(Payment).where(Payment.tg_id == tg_id).order_by(Payment.id.desc()).limit(30))).scalars().all()
        consents = (await s.execute(select(Consent).where(Consent.tg_id == tg_id).order_by(Consent.id.desc()).limit(20))).scalars().all()
        uses = (await s.execute(select(PromoUse, Promo).join(Promo, Promo.id == PromoUse.promo_id).where(PromoUse.tg_id == tg_id))).all()
        spent = (await s.execute(select(func.coalesce(func.sum(Payment.amount_kop), 0)).where(Payment.tg_id == tg_id, Payment.status == "succeeded"))).scalar_one()
        cfg = await load_settings(s)
    return {
        "tg_id": u.tg_id, "username": u.username, "first_name": u.first_name, "balance_rub": u.balance_kop / 100,
        "banned": u.banned, "created": u.created_at.isoformat(), "last_seen": u.last_seen_at.isoformat() if u.last_seen_at else None,
        "autopay_enabled": u.autopay_enabled, "promo_discount_pct": u.promo_discount_pct, "promo_code": u.promo_code,
        "no_markup": u.no_markup, "markup_pct": cfg["markup_pct"],
        "consent_version": u.consent_version, "docs_version": cfg["docs_version"], "consent_at": u.consent_at.isoformat() if u.consent_at else None,
        "paid_total_rub": spent / 100,
        "servers": [{"id": x.id, "astra_id": x.astra_id, "name": x.name, "status": x.status, "location": x.location,
                     "cpu": x.cpu, "ram": x.ram, "disk": x.disk, "ip": x.ip, "price_rub": x.price_kop / 100, "cost_rub": x.cost_kop / 100,
                     "auto_renew": x.auto_renew, "expires_at": x.expires_at.isoformat() if x.expires_at else None} for x in servers],
        "methods": await methods(tg_id, include_deleted=True),
        "payments": [{"id": p.id, "amount_rub": p.amount_kop / 100, "status": p.status, "autopay": p.autopay, "receipt": p.receipt_status,
                      "receipt_url": p.receipt_url, "method": p.method_title, "cancel_reason": p.cancel_reason, "created": p.created_at.isoformat()} for p in pays],
        "consents": [{"doc": c.doc, "version": c.version, "source": c.source, "accepted": c.accepted, "created": c.created_at.isoformat()} for c in consents],
        "promos": [{"code": pr.code, "kind": pr.kind, "value": pr.value, "created": pu.created_at.isoformat()} for pu, pr in uses],
        "history": await history(tg_id, 50),
        "referral": await referral.admin_info(tg_id),
    }


async def search_clients(q: str = "", flt: str = "") -> list[dict]:
    q = (q or "").strip().lstrip("@")
    async with session() as s:
        stmt = select(User)
        if q:
            cond = [User.username.ilike(f"%{q}%"), User.first_name.ilike(f"%{q}%")]
            if q.isdigit():
                cond.append(User.tg_id == int(q))
            stmt = stmt.where(or_(*cond))
        if flt == "active":
            stmt = stmt.where(User.tg_id.in_(select(Server.tg_id).where(Server.status.in_(["active", "suspended", "provisioning"]))))
        elif flt == "autopay":
            stmt = stmt.where(User.autopay_enabled.is_(True))
        elif flt == "banned":
            stmt = stmt.where(User.banned.is_(True))
        elif flt == "balance":
            stmt = stmt.where(User.balance_kop > 0)
        rows = (await s.execute(stmt.order_by(User.created_at.desc()).limit(50))).scalars().all()
        ids = [u.tg_id for u in rows]
        cnt = dict((await s.execute(select(Server.tg_id, func.count()).where(Server.tg_id.in_(ids), Server.status != "deleted").group_by(Server.tg_id))).all()) if ids else {}
        mcnt = dict((await s.execute(select(PaymentMethod.tg_id, func.count()).where(PaymentMethod.tg_id.in_(ids), PaymentMethod.active.is_(True)).group_by(PaymentMethod.tg_id))).all()) if ids else {}
    return [{"tg_id": u.tg_id, "username": u.username, "first_name": u.first_name, "balance_rub": u.balance_kop / 100, "banned": u.banned,
             "servers": cnt.get(u.tg_id, 0), "methods": mcnt.get(u.tg_id, 0), "autopay": u.autopay_enabled, "no_markup": u.no_markup} for u in rows]
