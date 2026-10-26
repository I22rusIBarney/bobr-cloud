"""API личного кабинета клиента (Telegram Mini App, страница /app)."""
from __future__ import annotations

import logging
from datetime import timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select

from .. import services as svc
from ..config import env
from ..db import Payment, Server, User, get_or_create_user, session, utcnow
from ..services import UserError
from .. import account as acc
from .. import tickets as tk
from .. import referral
from .. import subscription as sub
from ..legal import DOCS, REQUIRED, render_fragment
from ..settings import load_settings
from .auth import validate_init_data

log = logging.getLogger(__name__)

ACTIONS = {"start", "stop", "reboot", "vnc", "password", "change-ip", "reinstall"}


def server_dict(srv: Server, refund_pct: float) -> dict:
    left = None
    if srv.expires_at:
        left = (srv.expires_at - utcnow()).total_seconds() / 86400
    return {
        "id": srv.id,
        "name": srv.name,
        "status": srv.status,
        "location": srv.location,
        "cpu": srv.cpu,
        "ram": srv.ram,
        "disk": srv.disk,
        "os": srv.os,
        "os_label": svc.os_label(srv.os),
        "flag": svc.flag(srv.location),
        "ip": srv.ip,
        "password": srv.root_password,
        "expires_at": srv.expires_at.astimezone(timezone.utc).isoformat() if srv.expires_at else None,
        "days_left": None if left is None else round(left, 1),
        "price_rub": srv.price_kop / 100,
        "auto_renew": srv.auto_renew,
        "refund_rub": svc.refund_amount(svc.paid_base(srv), srv.expires_at, refund_pct, svc.paid_months(srv)) / 100,
    }


class TopupIn(BaseModel):
    amount_rub: int
    save_method: bool = False


class ConsentIn(BaseModel):
    docs: list[str]


class PromoIn(BaseModel):
    code: str


class TicketIn(BaseModel):
    subject: str = ""
    text: str
    server_id: int | None = None


class TicketMsgIn(BaseModel):
    text: str


class BuyIn(BaseModel):
    idx: int
    location: str
    os: str
    password: str | None = None  # пусто — Astra сгенерирует сама
    period: str = "1m"


class NewsIn(BaseModel):
    enabled: bool


class RenameIn(BaseModel):
    name: str


class UpgradeIn(BaseModel):
    cpu: int
    ram: int


class ActionIn(BaseModel):
    action: str
    os: str | None = None


class AutoIn(BaseModel):
    value: bool


def build_cabinet(ctx: svc.Ctx) -> APIRouter:
    r = APIRouter(prefix="/api/me")

    async def me(x_init_data: str = Header(default="")) -> dict:
        u = validate_init_data(x_init_data, env().bot_token)
        if not u:
            raise HTTPException(403, "Откройте кабинет из Telegram")
        async with session() as s:
            user = await get_or_create_user(s, int(u["id"]), u.get("username"))
            first = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)[:128] or None
            if first != user.first_name or not user.last_seen_at or (utcnow() - user.last_seen_at).total_seconds() > 300:
                user.first_name, user.last_seen_at = first, utcnow()
                await s.commit()
            cfg = await load_settings(s)
        if user.banned:
            raise HTTPException(403, "Доступ ограничен")
        return {"id": int(u["id"]), "consented": (user.consent_version or 0) >= int(cfg["docs_version"])}

    async def me_ok(m: dict = Depends(me)) -> dict:
        if not m["consented"]:
            raise HTTPException(403, "Чтобы продолжить, примите документы сервиса.")
        if await sub.required(ctx, m["id"]):
            raise HTTPException(403, "Чтобы продолжить, подпишитесь на наш канал.")
        return m

    def wrap(ex: UserError) -> HTTPException:
        return HTTPException(400, str(ex))

    @r.get("")
    async def overview(m: dict = Depends(me)):
        async with session() as s:
            cfg = await load_settings(s)
            user = await s.get(User, m["id"])
            rows = (
                await s.execute(select(Server).where(Server.tg_id == m["id"], Server.status != "deleted").order_by(Server.id))
            ).scalars().all()
        n_methods = len(await acc.methods(m["id"]))
        sub_info = await sub.required(ctx, m["id"]) if m["consented"] else None
        return {
            "subscription": {"needed": True, **sub_info} if sub_info else {"needed": False},
            "news_off": not user.ads_consent,
            "ads_consent": bool(user.ads_consent),
            "is_admin": m["id"] in env().admins,
            "user": {"id": user.tg_id, "username": user.username, "first_name": user.first_name, "since": user.created_at.isoformat()},
            "consent": {"needed": not m["consented"], "version": cfg["docs_version"], "date": cfg["docs_date"],
                        "docs": [{"key": k, **DOCS[k]} for k in REQUIRED], "optional": [{"key": "ads", **DOCS["ads"]}]},
            "discount": {"pct": user.promo_discount_pct, "code": user.promo_code} if user.promo_discount_pct else None,
            "autopay": {"available": cfg["autopay_available"], "enabled": user.autopay_enabled, "methods": n_methods},
            "service_name": cfg["service_name"],
            "theme": {"bg": cfg["theme_bg"], "image": cfg["theme_bg_image"], "effect": cfg["theme_effect"], "client_choice": cfg["theme_client_choice"]},
            "tickets_unread": await tk.unread_for_client(m["id"]) if m["consented"] else 0,
            "balance_rub": user.balance_kop / 100,
            "servers": [server_dict(x, cfg["refund_pct"]) for x in rows] if m["consented"] else [],
            "cfg": {
                "min_topup_rub": cfg["min_topup_rub"],
                "max_topup_rub": cfg["max_topup_rub"],
                "change_ip_price_rub": cfg["change_ip_price_rub"],
                "maintenance": cfg["maintenance"],
                "welcome_text": cfg["welcome_text"],
                "support": None,  # поддержка — через обращения в кабинете
            },
        }

    # ───── подписка на канал и новости ─────

    @r.post("/subscription/check")
    async def subscription_check(m: dict = Depends(me)):
        sub.forget(m["id"])
        info = await sub.required(ctx, m["id"])
        return {"needed": bool(info), **(info or {})}

    @r.post("/news")
    async def news_toggle(p: NewsIn, m: dict = Depends(me_ok)):
        return {"enabled": await acc.set_ads_consent(m["id"], p.enabled, "app")}

    # ───── согласия и документы ─────

    @r.post("/consent")
    async def consent(p: ConsentIn, m: dict = Depends(me)):
        try:
            return {"version": await acc.accept_consent(m["id"], "app", p.docs)}
        except UserError as ex:
            raise wrap(ex)

    @r.post("/consent/revoke")
    async def consent_revoke(m: dict = Depends(me)):
        await acc.revoke_consent(m["id"], "app")
        await ctx.alert_admin(f"🗑 Клиент {m['id']} отозвал согласие на обработку персональных данных. Удалите его данные в течение 30 дней (152-ФЗ), если нет действующих договорных обязательств.")
        return {"ok": True}

    @r.get("/legal/{doc}")
    async def legal_doc(doc: str, m: dict = Depends(me)):
        if doc not in DOCS:
            raise HTTPException(404)
        async with session() as s:
            cfg = await load_settings(s)
        return {"title": DOCS[doc]["title"], "html": render_fragment(doc, cfg, env().public_url, ctx.bot_username)}

    @r.get("/avatar")
    async def my_avatar(m: dict = Depends(me)):
        a = await acc.avatar(m["id"])
        if not a:
            raise HTTPException(404)
        return Response(a[0], media_type=a[1], headers={"Cache-Control": "private, max-age=3600"})

    # ───── профиль: промокоды, способы оплаты, автоплатёж, история ─────

    @r.post("/promo")
    async def promo(p: PromoIn, m: dict = Depends(me_ok)):
        try:
            return await acc.apply_promo(m["id"], p.code)
        except UserError as ex:
            raise wrap(ex)

    @r.get("/methods")
    async def my_methods(m: dict = Depends(me_ok)):
        return await acc.methods(m["id"])

    @r.delete("/methods/{mid}")
    async def delete_method(mid: int, m: dict = Depends(me_ok)):
        n = await svc.deactivate_method(m["id"], mid, "user")
        if not n:
            raise HTTPException(404, "Способ оплаты не найден")
        return {"deleted": n}

    @r.delete("/methods")
    async def delete_all_methods(m: dict = Depends(me_ok)):
        return {"deleted": await svc.deactivate_method(m["id"], None, "user")}

    @r.post("/autopay")
    async def autopay(p: AutoIn, m: dict = Depends(me_ok)):
        try:
            return {"enabled": await acc.set_autopay(m["id"], p.value)}
        except UserError as ex:
            raise wrap(ex)

    @r.get("/history")
    async def my_history(m: dict = Depends(me_ok)):
        return await acc.history(m["id"])

    @r.get("/referral")
    async def my_referral(m: dict = Depends(me_ok)):
        return await referral.stats(m["id"], ctx.bot_username)

    # ───── обращения в поддержку ─────

    @r.get("/tickets")
    async def tickets(m: dict = Depends(me_ok)):
        return await tk.list_client(m["id"])

    @r.post("/tickets")
    async def ticket_new(p: TicketIn, m: dict = Depends(me_ok)):
        try:
            t = await tk.create(ctx, m["id"], p.subject, p.text, p.server_id)
        except UserError as ex:
            raise wrap(ex)
        return {"id": t.id}

    @r.get("/tickets/{tid}")
    async def ticket(tid: int, m: dict = Depends(me_ok)):
        try:
            return await tk.get_client(m["id"], tid)
        except UserError as ex:
            raise HTTPException(404, str(ex))

    @r.post("/tickets/{tid}/messages")
    async def ticket_reply(tid: int, p: TicketMsgIn, m: dict = Depends(me_ok)):
        try:
            await tk.client_reply(ctx, m["id"], tid, p.text)
        except UserError as ex:
            raise wrap(ex)
        return {"ok": True}

    @r.post("/tickets/{tid}/close")
    async def ticket_close(tid: int, m: dict = Depends(me_ok)):
        try:
            await tk.client_close(m["id"], tid)
        except UserError as ex:
            raise wrap(ex)
        return {"ok": True}

    @r.get("/catalog")
    async def catalog(m: dict = Depends(me_ok)):
        cat = await ctx.catalog()
        out = []
        for loc in cat.get("locations", []):
            if not loc.get("available", True):
                continue
            out.append(
                {
                    "code": loc["code"],
                    "name": loc["name"],
                    "flag": loc.get("flag") or svc.flag(loc["code"]),
                    "channel": loc.get("channel", ""),
                    # себестоимость клиенту не отдаём
                    "offers": [{k: v for k, v in o.items() if k != "cost_rub"} for o in await svc.offers(ctx, loc["code"], m["id"])],
                }
            )
        oses = cat.get("os", [])
        async with session() as s:
            cfg = await load_settings(s)
        return {"locations": out, "os": oses, "os_labels": cat.get("os_labels") or {o: svc.os_label(o) for o in oses},
                "periods": svc.period_options(cfg, cat)}

    @r.post("/topup")
    async def topup(p: TopupIn, m: dict = Depends(me_ok)):
        try:
            _, url = await svc.create_topup(ctx, m["id"], p.amount_rub, p.save_method)
        except UserError as ex:
            raise wrap(ex)
        except Exception:
            log.exception("topup")
            raise HTTPException(502, "Не удалось создать платёж")
        return {"url": url}

    @r.post("/servers")
    async def buy(p: BuyIn, m: dict = Depends(me_ok)):
        try:
            srv = await svc.buy_server(ctx, m["id"], p.idx, p.location, p.os, p.password, p.period)
        except UserError as ex:
            raise wrap(ex)
        return {"id": srv.id, "name": srv.name}

    async def _own(s, m, sid: int) -> Server:
        srv = await s.get(Server, sid)
        if not srv or srv.tg_id != m["id"] or srv.status == "deleted":
            raise HTTPException(404, "Сервер не найден")
        return srv

    @r.post("/servers/{sid}/action")
    async def action(sid: int, p: ActionIn, m: dict = Depends(me_ok)):
        if p.action not in ACTIONS:
            raise HTTPException(400, "Неизвестное действие")
        body = {"os": p.os} if p.action == "reinstall" else None
        if p.action == "reinstall" and not p.os:
            raise HTTPException(400, "Не указана ОС")
        try:
            resp = await svc.server_action(ctx, m["id"], sid, p.action, body)
        except UserError as ex:
            raise wrap(ex)
        link = None
        if p.action == "vnc" and isinstance(resp, dict):
            link = next((resp.get(k) for k in ("url", "link", "vnc_url", "console_url") if resp.get(k)), None)
        return {"ok": True, "link": link}

    @r.post("/servers/{sid}/renew")
    async def renew(sid: int, m: dict = Depends(me_ok)):
        async with session() as s:
            await _own(s, m, sid)
        try:
            await svc.renew_server(ctx, sid)
        except UserError as ex:
            raise wrap(ex)
        return {"ok": True}

    @r.post("/servers/{sid}/autorenew")
    async def autorenew(sid: int, p: AutoIn, m: dict = Depends(me_ok)):
        async with session() as s:
            srv = await _own(s, m, sid)
            srv.auto_renew = p.value
            await s.commit()
        return {"auto_renew": p.value}

    @r.post("/servers/{sid}/rename")
    async def rename(sid: int, p: RenameIn, m: dict = Depends(me_ok)):
        try:
            return {"name": await svc.rename_server(ctx, m["id"], sid, p.name)}
        except UserError as ex:
            raise wrap(ex)

    @r.get("/servers/{sid}/live")
    async def live(sid: int, m: dict = Depends(me_ok)):
        try:
            return await svc.server_live(ctx, m["id"], sid)
        except UserError as ex:
            raise wrap(ex)

    @r.get("/servers/{sid}/upgrade")
    async def upgrade_quote(sid: int, cpu: int | None = None, ram: int | None = None, m: dict = Depends(me_ok)):
        try:
            return await svc.upgrade_quote(ctx, m["id"], sid, cpu, ram)
        except UserError as ex:
            raise wrap(ex)

    @r.post("/servers/{sid}/upgrade")
    async def upgrade(sid: int, p: UpgradeIn, m: dict = Depends(me_ok)):
        try:
            return await svc.upgrade_server(ctx, m["id"], sid, p.cpu, p.ram)
        except UserError as ex:
            raise wrap(ex)

    @r.delete("/servers/{sid}")
    async def delete(sid: int, m: dict = Depends(me_ok)):
        try:
            refund = await svc.cancel_server(ctx, m["id"], sid)
        except UserError as ex:
            raise wrap(ex)
        return {"refund_rub": refund / 100}

    @r.get("/payments")
    async def payments(m: dict = Depends(me_ok)):
        async with session() as s:
            rows = (await s.execute(select(Payment).where(Payment.tg_id == m["id"]).order_by(Payment.id.desc()).limit(30))).scalars().all()
        return [
            {"id": p.id, "amount_rub": p.amount_kop / 100, "status": p.status, "receipt_url": p.receipt_url, "created": p.created_at.isoformat()}
            for p in rows
        ]

    return r
