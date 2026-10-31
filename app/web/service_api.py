"""Служебный API для своих ботов (например, бота развёртывания VPN-нод).

Доступ — по токену SERVICE_TOKEN из .env (заголовок `Authorization: Bearer <токен>`).
Покупать можно от имени любого клиента и с его баланса — так владелец настраивает серверы за клиентов.
Токен равен доступу к балансам всех клиентов: храните его как пароль от админки. Если SERVICE_TOKEN пуст, API выключен (404).
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from .. import services as svc
from ..config import env
from ..db import Server, User, session
from ..services import UserError
from ..settings import load_settings


class SvcBuyIn(BaseModel):
    tg_id: int
    location: str
    os: str
    idx: int | None = None
    cpu: int | None = None
    ram: int | None = None
    disk: int | None = None
    period: str = "1m"
    password: str | None = None


class SvcRenameIn(BaseModel):
    tg_id: int
    name: str


class SvcOwnerIn(BaseModel):
    tg_id: int


def build_service_router(ctx: svc.Ctx) -> APIRouter:
    r = APIRouter(prefix="/api/svc")

    async def auth(authorization: str = Header(default="")) -> None:
        token = env().service_token
        if not token:
            raise HTTPException(404)
        got = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(got.encode(), token.encode()):
            raise HTTPException(401, "Неверный токен")

    async def client(tg_id: int) -> User:
        async with session() as s:
            u = await s.get(User, tg_id)
        if u is None:
            raise HTTPException(404, "Клиент не найден — он должен хотя бы раз открыть бота bcloud")
        if u.banned:
            raise HTTPException(403, "Клиент заблокирован в bcloud")
        return u

    def bad(ex: UserError) -> HTTPException:
        return HTTPException(400, str(ex))

    @r.get("/ping")
    async def ping(_: None = Depends(auth)):
        return {"ok": True}

    @r.get("/catalog")
    async def catalog(tg_id: int, _: None = Depends(auth)):
        await client(tg_id)
        cat = await ctx.catalog()
        async with session() as s:
            cfg = await load_settings(s)
        locs = await svc.locations(ctx)
        out = []
        for loc in locs:
            out.append({"code": loc["code"], "name": loc.get("name", ""), "flag": loc.get("flag", ""),
                        "offers": await svc.offers(ctx, loc["code"], tg_id)})
        conf = svc.configurator(cat) if cfg.get("custom_enabled", True) else None
        return {"locations": out,
                "os": [{"code": o, "label": svc.os_label(o)} for o in cat.get("os", [])],
                "periods": svc.period_options(cfg, cat),
                "configurator": conf,
                "maintenance": bool(cfg.get("maintenance"))}

    @r.get("/quote")
    async def quote(tg_id: int, location: str, cpu: int, ram: int, disk: int, _: None = Depends(auth)):
        """Цена своей конфигурации (конструктор) для клиента за месяц."""
        await client(tg_id)
        try:
            o = await svc.custom_offer(ctx, location, cpu, ram, disk, tg_id)
        except UserError as ex:
            raise bad(ex)
        return {k: v for k, v in o.items() if k != "cost_rub"}

    @r.get("/balance")
    async def balance(tg_id: int, _: None = Depends(auth)):
        u = await client(tg_id)
        return {"balance_kop": u.balance_kop}

    @r.get("/users/resolve")
    async def resolve(q: str, _: None = Depends(auth)):
        """Клиент по Telegram ID или @username."""
        q = q.strip()
        async with session() as s:
            if q.lstrip("-").isdigit():
                u = await s.get(User, int(q))
            else:
                name = q.removeprefix("@").lower()
                u = (await s.execute(select(User).where(func.lower(User.username) == name))).scalars().first() if name else None
        if u is None:
            raise HTTPException(404, "Клиент не найден: он должен хотя бы раз открыть бота bcloud. По @username ищем только тех, у кого он указан в Telegram")
        return {"tg_id": u.tg_id, "username": u.username, "first_name": u.first_name, "balance_kop": u.balance_kop,
                "banned": bool(u.banned), "admin": u.tg_id in env().admins}

    @r.post("/servers")
    async def buy(p: SvcBuyIn, _: None = Depends(auth)):
        await client(p.tg_id)
        try:
            if p.idx is None and not (p.cpu and p.ram and p.disk):
                raise UserError("Не указана конфигурация.")
            spec = None if p.idx is not None else {"cpu": p.cpu, "ram": p.ram, "disk": p.disk}
            srv = await svc.buy_server(ctx, p.tg_id, p.idx, p.location, p.os, p.password, p.period, spec=spec)
        except UserError as ex:
            raise bad(ex)
        return _view(srv)

    @r.get("/servers")
    async def servers(tg_id: int, _: None = Depends(auth)):
        """Все серверы клиента в bcloud (кроме удалённых) — с IP и root-паролем, где бы они ни были куплены."""
        await client(tg_id)
        async with session() as s:
            rows = (await s.execute(select(Server).where(Server.tg_id == tg_id, Server.status != "deleted")
                                    .order_by(Server.id))).scalars().all()
        locs = {loc["code"]: loc for loc in await svc.locations(ctx)}
        return {"servers": [{**_view(x), "loc_name": (locs.get(x.location) or {}).get("name", x.location),
                             "flag": (locs.get(x.location) or {}).get("flag") or svc.flag(x.location)} for x in rows]}

    async def owned(server_id: int, tg_id: int) -> Server:
        async with session() as s:
            srv = await s.get(Server, server_id)
        if srv is None or srv.tg_id != tg_id:
            raise HTTPException(404, "Сервер не найден")
        return srv

    async def view(srv: Server) -> dict:
        async with session() as s:
            cfg = await load_settings(s)
            u = await s.get(User, srv.tg_id)
        return {**_view(srv), "change_ip_price_kop": svc.change_ip_price_kop(cfg, u)}

    @r.get("/servers/{server_id}")
    async def server(server_id: int, tg_id: int, fresh: int = 1, sync: int = 0, _: None = Depends(auth)):
        """sync=1 — сначала обновить данные у провайдера (например, после смены IP в кабинете)."""
        srv = await owned(server_id, tg_id)
        if sync or (fresh and srv.status == "provisioning"):
            await svc.refresh_server(ctx, srv.astra_id)
            srv = await owned(server_id, tg_id)
        return await view(srv)

    @r.post("/servers/{server_id}/change-ip")
    async def change_ip(server_id: int, p: SvcOwnerIn, _: None = Depends(auth)):
        """Смена IP у провайдера с баланса владельца сервера (как кнопка в bcloud).
        pending=true — провайдер ответил с задержкой: bcloud сам проверит результат и вернёт деньги при отказе."""
        await client(p.tg_id)
        await owned(server_id, p.tg_id)
        try:
            await svc.server_action(ctx, p.tg_id, server_id, "change-ip")
        except UserError as ex:
            if "задержкой" in str(ex):
                return {**await view(await owned(server_id, p.tg_id)), "pending": True, "message": str(ex)}
            raise bad(ex)
        return {**await view(await owned(server_id, p.tg_id)), "pending": False}

    @r.post("/servers/{server_id}/rename")
    async def rename(server_id: int, p: SvcRenameIn, _: None = Depends(auth)):
        try:
            return {"name": await svc.rename_server(ctx, p.tg_id, server_id, p.name)}
        except UserError as ex:
            raise bad(ex)

    return r


def _view(srv: Server) -> dict:
    return {"id": srv.id, "name": srv.name, "status": srv.status, "ip": srv.ip, "root_password": srv.root_password,
            "location": srv.location, "cpu": srv.cpu, "ram": srv.ram, "disk": srv.disk, "os": srv.os,
            "price_kop": srv.price_kop, "expires_at": srv.expires_at.isoformat() if srv.expires_at else None}
