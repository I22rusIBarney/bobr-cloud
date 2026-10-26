"""Аналитика для админки: выручка и маржа по месяцам, клиенты, отток, источники.

Выручка — то, что клиенты потратили с баланса на услуги (покупка, продление, смена IP, апгрейд)
за вычетом возвратов. Маржа — оценка: выручка минус себестоимость Astra по тем же операциям
(себестоимость берём из сервера, к которому относится операция).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .db import Payment, Promo, PromoUse, Server, Tx, User, session, utcnow

SALE = {"purchase", "renew", "change_ip", "upgrade", "upgrade_settle"}
REFUND = {"refund_renew", "refund_change_ip", "refund_upgrade", "refund_cancel", "refund_failed", "refund_purchase", "refund_create"}
LIVE = ("active", "suspended", "expired")
MONTHS_RU = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _aware(d: datetime | None) -> datetime | None:
    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


def _month_starts(n: int, now: datetime) -> list[datetime]:
    y, m = now.year, now.month
    out = []
    for _ in range(n):
        out.append(datetime(y, m, 1, tzinfo=timezone.utc))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def _key(d: datetime) -> str:
    return f"{d.year}-{d.month:02d}"


async def build(months: int = 12) -> dict:
    now = utcnow()
    starts = _month_starts(months, now)
    since = starts[0]
    keys = [_key(d) for d in starts]
    async with session() as s:
        txs = (await s.execute(select(Tx).where(Tx.created_at >= since, Tx.kind.in_(SALE | REFUND)))).scalars().all()
        servers = (await s.execute(select(Server))).scalars().all()
        users = (await s.execute(select(User.tg_id, User.consent_at, User.created_at, User.referrer_id, User.winback_at, User.abandon_promo_at)
                                 .where(User.consent_version > 0))).all()
        first_pay = dict((await s.execute(select(Payment.tg_id, func.min(Payment.created_at)).where(Payment.status == "succeeded").group_by(Payment.tg_id))).all())
        topups = (await s.execute(select(Payment.created_at, Payment.amount_kop).where(Payment.status == "succeeded", Payment.created_at >= since))).all()
        promo_rows = (await s.execute(select(Promo.code, func.count(PromoUse.id)).join(PromoUse, PromoUse.promo_id == Promo.id).group_by(Promo.code)
                                      .order_by(func.count(PromoUse.id).desc()).limit(5))).all()
        promo_users = (await s.execute(select(func.count(func.distinct(PromoUse.tg_id))))).scalar_one()

    by_astra = {sv.astra_id: sv for sv in servers}
    by_user: dict[int, list[Server]] = defaultdict(list)
    for sv in servers:
        by_user[sv.tg_id].append(sv)

    def cost_for(t: Tx) -> int:
        """Себестоимость операции: по ref находим сервер (renew/upgrade — astra_id, purchase — ближайшая покупка клиента)."""
        if t.kind == "renew" and t.ref.isdigit() and int(t.ref) in by_astra:
            return by_astra[int(t.ref)].cost_kop
        if t.kind == "purchase":
            ta = _aware(t.created_at)
            near = [sv for sv in by_user.get(t.tg_id, []) if abs((_aware(sv.created_at) - ta).total_seconds()) < 900]
            if near:
                sv = min(near, key=lambda x: abs((_aware(x.created_at) - ta).total_seconds()))
                months_paid = max(1, round(-t.amount_kop / sv.price_kop)) if sv.price_kop else 1
                return sv.cost_kop * months_paid
        if t.kind in ("upgrade", "upgrade_settle"):
            return int(-t.amount_kop * 0.75)  # апгрейд: ориентировочно, точную себестоимость Astra не сообщает
        return 0

    rev, cost, top = Counter(), Counter(), Counter()
    for t in txs:
        k = _key(_aware(t.created_at))
        rev[k] += -t.amount_kop
        if t.kind in SALE and t.amount_kop < 0:
            cost[k] += cost_for(t)
    for at, amount in topups:
        top[_key(_aware(at))] += amount

    new_clients, new_payers = Counter(), Counter()
    for tg, consent_at, created_at, _, _, _ in users:
        d = _aware(consent_at or created_at)
        if d and d >= since:
            new_clients[_key(d)] += 1
    for tg, d in first_pay.items():
        d = _aware(d)
        if d and d >= since:
            new_payers[_key(d)] += 1

    def ended(sv: Server) -> datetime | None:
        if sv.status != "deleted":
            return None
        return _aware(sv.deleted_at) or min(_aware(sv.expires_at) or now, now)

    churn = Counter()
    for sv in servers:
        e = ended(sv)
        if e and e >= since and sv.ip:  # сервер, который так и не создался, оттоком не считаем
            churn[_key(e)] += 1

    def active_at(t: datetime) -> int:
        n = 0
        for sv in servers:
            c, e = _aware(sv.created_at), ended(sv)
            if c <= t and (e is None or e > t) and (sv.ip or sv.status in LIVE):
                n += 1
        return n

    series = []
    for d, k in zip(starts, keys):
        series.append({"month": k, "label": MONTHS_RU[d.month - 1] + ("" if d.year == now.year else f" {str(d.year)[2:]}"),
                       "revenue_rub": round(rev[k] / 100), "margin_rub": round((rev[k] - cost[k]) / 100), "topups_rub": round(top[k] / 100),
                       "new_clients": new_clients[k], "new_payers": new_payers[k], "churned": churn[k]})

    live = [sv for sv in servers if sv.status in LIVE]
    mrr = sum(sv.price_kop for sv in live)
    mcost = sum(sv.cost_kop for sv in live)
    clients = len({sv.tg_id for sv in live})
    a30 = active_at(now - timedelta(days=30))
    gone30 = sum(1 for sv in servers if (e := ended(sv)) and e > now - timedelta(days=30) and sv.ip and _aware(sv.created_at) <= now - timedelta(days=30))
    lifetimes = [(ended(sv) - _aware(sv.created_at)).days for sv in servers if ended(sv) and sv.ip]
    total_clients = len(users)
    referred = sum(1 for u in users if u[3])

    def conv(col: int) -> tuple[int, int]:
        sent = [u for u in users if u[col] and _aware(u[col]) > now - timedelta(days=90)]
        won = sum(1 for u in sent if any(_aware(sv.created_at) > _aware(u[col]) for sv in by_user.get(u[0], [])))
        return len(sent), won

    wb_sent, wb_won = conv(4)
    ab_sent, ab_won = conv(5)
    return {
        "series": series,
        "kpi": {
            "mrr_rub": round(mrr / 100), "margin_month_rub": round((mrr - mcost) / 100), "active_servers": len(live), "active_clients": clients,
            "arpu_rub": round(mrr / 100 / clients) if clients else 0,
            "churn_30d_pct": round(gone30 / a30 * 100, 1) if a30 else 0.0,
            "avg_lifetime_days": round(sum(lifetimes) / len(lifetimes)) if lifetimes else None,
            "clients_total": total_clients, "paying_total": len(first_pay),
        },
        "sources": {
            "referred": referred, "referred_pct": round(referred / total_clients * 100) if total_clients else 0,
            "promo_users": promo_users, "promos": [{"code": c, "uses": n} for c, n in promo_rows],
            "winback": {"sent": wb_sent, "won": wb_won}, "abandon": {"sent": ab_sent, "won": ab_won},
        },
    }
