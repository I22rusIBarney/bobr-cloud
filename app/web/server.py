"""FastAPI: вебхук ЮKassa + API и страница админки (Telegram Mini App)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from .. import services as svc
from ..config import env
from ..db import Payment, Server, Tx, User, change_balance, session, utcnow
from ..integrations import load_integrations
from ..nalog import NalogError
from ..settings import DEFAULTS, cost_kop, get_secrets, load_settings, save_settings, sell_price_kop, set_secrets
from .. import account as acc
from .. import tickets as tk
from .. import referral
from ..legal import DOCS, render_page
from ..services import UserError
from .auth import validate_init_data

log = logging.getLogger(__name__)
HERE = Path(__file__).parent


# Модели тел запросов — на уровне модуля: из-за `from __future__ import annotations`
# FastAPI ищет аннотации в глобальной области, локальные классы он не видит (→ 422).


class YkIn(BaseModel):
    shop_id: str
    secret_key: str = ""  # пусто = оставить текущий


class NalogIn(BaseModel):
    inn: str
    password: str = ""  # пусто = оставить текущий


class SettingsPatch(BaseModel):
    values: dict[str, Any]


class BalancePatch(BaseModel):
    delta_rub: float


class BanPatch(BaseModel):
    banned: bool


class AutopayPatch(BaseModel):
    enabled: bool


class PromoIn(BaseModel):
    code: str
    kind: str
    value: int
    max_uses: int = 0
    days: int | None = None
    new_users_only: bool = False
    note: str = ""


class PromoPatch(BaseModel):
    active: bool


class AdminMsgIn(BaseModel):
    text: str
    internal: bool = False


class StatusIn(BaseModel):
    status: str


class BroadcastIn(BaseModel):
    text: str
    audience: str = "all"
    kind: str = "news"
    btn_text: str = ""
    btn_url: str = ""
    test: bool = False


class ChatIn(BaseModel):
    chat: str


class MarkupIn(BaseModel):
    no_markup: bool | None = None   # старый переключатель
    mode: str | None = None         # default | custom | none
    pct: float | None = None


class EscalateIn(BaseModel):
    server_id: int | None = None


def _media_dir() -> Path:
    """Папка для загруженных файлов — рядом с базой (в Docker это смонтированный том data/)."""
    url = env().database_url
    if url.startswith("sqlite") and ":///" in url:
        return Path(url.split(":///", 1)[1]).resolve().parent / "media"
    return Path("data/media").resolve()


def build_app(ctx: svc.Ctx) -> FastAPI:
    app = FastAPI(title="vpsbot", docs_url=None, redoc_url=None, openapi_url=None)

    # ───── публичное ─────

    @app.get("/paid", response_class=HTMLResponse)
    async def paid():
        return "<meta name=viewport content='width=device-width'><body style='font-family:sans-serif;text-align:center;padding:48px'><h2>Оплата принята ✅</h2><p>Вернитесь в Telegram — баланс пополнится автоматически.</p>"

    @app.get("/r/{code}")
    async def ref_link(code: str):
        """Веб-ссылка приглашения: ведёт в бота с параметром ref_…, по ней пригласивший получает бонусы."""
        if referral.parse(code) is None or not ctx.bot_username:
            raise HTTPException(404, "Ссылка недействительна")
        return RedirectResponse(f"https://t.me/{ctx.bot_username}?start={referral.PREFIX}{code.lower()}", status_code=302)

    @app.get("/legal/{doc}", response_class=HTMLResponse)
    async def legal_page(doc: str):
        if doc not in DOCS:
            raise HTTPException(404)
        async with session() as s:
            cfg = await load_settings(s)
        return HTMLResponse(render_page(doc, cfg, env().public_url, ctx.bot_username), headers={"Cache-Control": "no-cache"})

    @app.post("/yookassa/webhook/{secret}")
    async def yk_webhook(secret: str, request: Request):
        if secret != env().webhook_secret:
            raise HTTPException(403)
        body = await request.json()
        obj = body.get("object") or {}
        if body.get("event") in ("payment.succeeded", "payment.canceled") and obj.get("id"):
            try:
                await svc.credit_payment(ctx, obj["id"])  # перепроверяет платёж через API ЮKassa
            except Exception:
                log.exception("webhook credit failed")
                raise HTTPException(500)  # ЮKassa повторит
        return {"ok": True}

    # ───── общие стили и скрипты Mini App ─────

    STATIC = {"ui.css": "text/css; charset=utf-8", "ui.js": "application/javascript; charset=utf-8", "fx.js": "application/javascript; charset=utf-8"}

    @app.get("/static/{name}")
    async def static_file(name: str):
        if name not in STATIC:
            raise HTTPException(404)
        return FileResponse(HERE / "static" / name, media_type=STATIC[name], headers={"Cache-Control": "no-cache"})

    # ───── кабинет клиента ─────

    from .cabinet import build_cabinet

    app.include_router(build_cabinet(ctx))

    @app.get("/app", response_class=FileResponse)
    async def cabinet_page():
        return FileResponse(HERE / "cabinet.html", headers={"Cache-Control": "no-store"})

    # ───── проверка прав админа (должна быть объявлена до эндпоинтов, которые её используют) ─────

    async def admin(x_init_data: str = Header(default="")) -> dict:
        user = validate_init_data(x_init_data, env().bot_token)
        if not user or int(user.get("id", 0)) not in env().admins:
            raise HTTPException(403, "forbidden")
        return user

    # ───── своя картинка фона ─────

    MEDIA = _media_dir()
    IMG_TYPES = {b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"\x89PNG": ("png", "image/png"), b"RIFF": ("webp", "image/webp")}

    def _bg_file():
        for ext in ("jpg", "png", "webp"):
            f = MEDIA / f"bg.{ext}"
            if f.exists():
                return f, {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext]
        return None, None

    @app.get("/media/bg")
    async def media_bg():
        f, ctype = _bg_file()
        if not f:
            raise HTTPException(404)
        return FileResponse(f, media_type=ctype, headers={"Cache-Control": "public, max-age=86400"})

    @app.put("/api/admin/theme/background")
    async def upload_bg(request: Request, _: dict = Depends(admin)):
        data = await request.body()
        if not data:
            raise HTTPException(400, "Файл пустой.")
        if len(data) > 4 * 1024 * 1024:
            raise HTTPException(400, "Картинка больше 4 МБ. Уменьшите её и загрузите снова.")
        kind = next((v for sig, v in IMG_TYPES.items() if data.startswith(sig) and (sig != b"RIFF" or data[8:12] == b"WEBP")), None)
        if not kind:
            raise HTTPException(400, "Подойдут JPG, PNG или WebP.")
        MEDIA.mkdir(parents=True, exist_ok=True)
        for ext in ("jpg", "png", "webp"):
            (MEDIA / f"bg.{ext}").unlink(missing_ok=True)
        (MEDIA / f"bg.{kind[0]}").write_bytes(data)
        import time as _t

        async with session() as s:
            cfg = await save_settings(s, {"theme_bg": "image", "theme_bg_image": f"/media/bg?v={int(_t.time())}"})
        return {"theme_bg": cfg["theme_bg"], "theme_bg_image": cfg["theme_bg_image"]}

    @app.delete("/api/admin/theme/background")
    async def delete_bg(_: dict = Depends(admin)):
        for ext in ("jpg", "png", "webp"):
            (MEDIA / f"bg.{ext}").unlink(missing_ok=True)
        async with session() as s:
            cfg = await load_settings(s)
            await save_settings(s, {"theme_bg_image": "", **({"theme_bg": "none"} if cfg["theme_bg"] == "image" else {})})
        return {"ok": True}

    # ───── интеграции: ЮKassa и «Мой налог» ─────

    def _webhook_url() -> str:
        return f"{env().public_url.rstrip('/')}/yookassa/webhook/{env().webhook_secret}"

    @app.get("/api/admin/integrations")
    async def integrations(_: dict = Depends(admin)):
        async with session() as s:
            sec = await get_secrets(s)
        yk_secret = ctx.yk._secret
        return {
            "webhook_url": _webhook_url(),
            "yookassa": {
                "configured": ctx.yk.configured,
                "shop_id": ctx.yk.shop_id,
                "secret_hint": ("••••" + yk_secret[-4:]) if yk_secret else "",
            },
            "nalog": {"configured": ctx.nalog.enabled, "inn": ctx.nalog.inn, "from_env": not sec["nalog_inn"] and ctx.nalog.enabled},
        }

    @app.post("/api/admin/integrations/yookassa")
    async def save_yk(p: YkIn, _: dict = Depends(admin)):
        shop = p.shop_id.strip()
        secret = p.secret_key.strip() or ctx.yk._secret
        if not shop or not secret:
            raise HTTPException(400, "Укажите shopId и секретный ключ")
        async with session() as s:
            await set_secrets(s, {"yk_shop_id": shop, "yk_secret": secret})
        ctx.yk.configure(shop, secret)
        return await _yk_check()

    async def _yk_check() -> dict:
        if not ctx.yk.configured:
            return {"ok": False, "message": "Не настроено"}
        try:
            code, data = await ctx.yk.check()
        except Exception as ex:
            return {"ok": False, "message": f"Нет связи с ЮKassa: {ex}"}
        if code == 200:
            return {"ok": True, "message": f"Ключи верны (аккаунт {data.get('account_id', '?')}, статус магазина: {data.get('status', '?')})"}
        if code == 401:
            return {"ok": False, "message": "Неверные shopId или секретный ключ (401)"}
        return {"ok": False, "message": f"ЮKassa ответила HTTP {code}"}

    @app.post("/api/admin/integrations/yookassa/test")
    async def test_yk(_: dict = Depends(admin)):
        return await _yk_check()

    @app.post("/api/admin/integrations/nalog")
    async def save_nalog(p: NalogIn, _: dict = Depends(admin)):
        inn = p.inn.strip()
        pwd = p.password or ctx.nalog._password
        if not (inn.isdigit() and len(inn) == 12):
            raise HTTPException(400, "ИНН физлица — 12 цифр")
        if not pwd:
            raise HTTPException(400, "Укажите пароль")
        async with session() as s:
            await set_secrets(s, {"nalog_inn": inn, "nalog_password": pwd})
        ctx.nalog.configure(inn, pwd)
        return await _nalog_check()

    async def _nalog_check() -> dict:
        if not ctx.nalog.enabled:
            return {"ok": False, "message": "Не настроено"}
        try:
            await ctx.nalog.check()
        except NalogError as ex:
            return {"ok": False, "message": f"Вход в «Мой налог» не удался: {ex}"[:300]}
        return {"ok": True, "message": "Вход в «Мой налог» выполнен. Чеки будут создаваться после пополнений."}

    @app.post("/api/admin/integrations/nalog/test")
    async def test_nalog(_: dict = Depends(admin)):
        return await _nalog_check()

    @app.delete("/api/admin/integrations/nalog")
    async def disable_nalog(_: dict = Depends(admin)):
        async with session() as s:
            await set_secrets(s, {"nalog_inn": "", "nalog_password": ""})
        ctx.nalog.configure("", "")
        return {"ok": True}

    # ───── админка ─────

    @app.get("/admin", response_class=FileResponse)
    async def admin_page():
        return FileResponse(HERE / "admin.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/admin/state")
    async def state(_: dict = Depends(admin)):
        async with session() as s:
            cfg = await load_settings(s)
            users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
            active = (await s.execute(select(Server).where(Server.status.in_(["active", "suspended", "provisioning"])))).scalars().all()
            since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            from datetime import timedelta

            rev30 = (
                await s.execute(
                    select(func.coalesce(func.sum(Payment.amount_kop), 0)).where(Payment.status == "succeeded", Payment.created_at > since - timedelta(days=30))
                )
            ).scalar_one()
            float_kop = (await s.execute(select(func.coalesce(func.sum(User.balance_kop), 0)))).scalar_one()
        try:
            ab = await ctx.astra.balance()
        except Exception as ex:
            ab = {"error": str(ex)}
        return {
            "settings": cfg,
            "defaults": DEFAULTS,
            "stats": {
                "users": users,
                "servers": len(active),
                "monthly_margin_rub": sum(x.price_kop - x.cost_kop for x in active) / 100,
                "monthly_revenue_rub": sum(x.price_kop for x in active) / 100,
                "topups_30d_rub": rev30 / 100,
                "client_balances_rub": float_kop / 100,
            },
            "astra": ab,
            "tickets": {k: v for k, v in (await tk.list_admin("active")).items() if k in ("counts", "unread")},
        }

    @app.post("/api/admin/settings")
    async def update_settings(p: SettingsPatch, _: dict = Depends(admin)):
        bad = [k for k in p.values if k not in DEFAULTS]
        if bad:
            raise HTTPException(400, f"unknown keys: {bad}")
        async with session() as s:
            try:
                return await save_settings(s, p.values)
            except (ValueError, TypeError) as ex:
                raise HTTPException(400, str(ex))

    @app.get("/api/admin/catalog")
    async def catalog(_: dict = Depends(admin)):
        cat = await ctx.catalog(fresh=True)
        async with session() as s:
            cfg = await load_settings(s)
        presets = []
        for p in cat.get("presets", []):
            presets.append(
                {
                    "idx": p["idx"], "cpu": p["cpu"], "ram": p["ram"], "disk": p["disk"],
                    "cost_rub": p.get("price_month_rub", {}),
                    "sell_rub": {loc: sell_price_kop(c, cfg) / 100 for loc, c in p.get("price_month_rub", {}).items()},
                    "hidden": p["idx"] in cfg["hidden_presets"],
                }
            )
        return {"locations": cat.get("locations", []), "presets": presets}

    def bad(ex: UserError) -> HTTPException:
        return HTTPException(400, str(ex))

    @app.get("/api/admin/users")
    async def users(q: str = "", filter: str = "", _: dict = Depends(admin)):
        return await acc.search_clients(q, filter)

    @app.get("/api/admin/users/{tg_id}")
    async def user_card(tg_id: int, _: dict = Depends(admin)):
        try:
            return await acc.client_card(tg_id)
        except UserError as ex:
            raise HTTPException(404, str(ex))

    @app.get("/api/admin/users/{tg_id}/avatar")
    async def user_avatar(tg_id: int, _: dict = Depends(admin)):
        from fastapi.responses import Response

        a = await acc.avatar(tg_id)
        if not a:
            raise HTTPException(404)
        return Response(a[0], media_type=a[1], headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/admin/users/{tg_id}/autopay")
    async def user_autopay(tg_id: int, p: AutopayPatch, _: dict = Depends(admin)):
        if p.enabled:
            raise HTTPException(400, "Включить автоплатёж может только сам клиент — это требование ЮKassa.")
        async with session() as s:
            u = await s.get(User, tg_id)
            if u is None:
                raise HTTPException(404)
            u.autopay_enabled = False
            await s.commit()
        return {"enabled": False}

    # ───── рассылки ─────

    @app.get("/api/admin/broadcasts")
    async def bc_list(_: dict = Depends(admin)):
        from .. import broadcasts as bc
        counts = {}
        for kind in bc.KINDS:
            for aud in bc.AUDIENCES:
                counts[f"{kind}:{aud}"] = await bc.audience_count(aud, kind)
        return {"items": await bc.history(), "audiences": bc.AUDIENCES, "counts": counts, "unsubscribed": await bc.news_unsubscribed_count()}

    @app.get("/api/admin/broadcasts/{bid}")
    async def bc_get(bid: int, _: dict = Depends(admin)):
        from .. import broadcasts as bc
        b = await bc.get(bid)
        if not b:
            raise HTTPException(404, "Рассылка не найдена")
        return b

    @app.post("/api/admin/broadcasts")
    async def bc_create(p: BroadcastIn, u: dict = Depends(admin)):
        from .. import broadcasts as bc
        try:
            if p.test:
                await bc.send_test(ctx, int(u["id"]), p.text, p.kind, p.btn_text, p.btn_url)
                return {"test": True}
            b = await bc.create(ctx, int(u["id"]), p.text, p.audience, p.kind, p.btn_text, p.btn_url)
        except bc.BroadcastError as ex:
            raise HTTPException(400, str(ex))
        return bc.as_dict(b)

    @app.post("/api/admin/broadcasts/{bid}/cancel")
    async def bc_cancel(bid: int, _: dict = Depends(admin)):
        from .. import broadcasts as bc
        await bc.cancel(bid)
        return await bc.get(bid)

    @app.post("/api/admin/subscription/check")
    async def sub_probe(p: ChatIn, _: dict = Depends(admin)):
        from .. import subscription as sub
        ref = sub.chat_ref(p.chat)
        if not ref:
            raise HTTPException(400, "Укажите @username канала или группы, либо ID вида -100…")
        if ctx.probe_chat is None:
            raise HTTPException(503, "Бот ещё не запущен")
        return await ctx.probe_chat(ref)

    @app.post("/api/admin/users/{tg_id}/markup")
    async def user_markup(tg_id: int, p: MarkupIn, _: dict = Depends(admin)):
        try:
            if p.mode:
                return await svc.set_markup(tg_id, p.mode, p.pct)
            return await svc.set_no_markup(tg_id, bool(p.no_markup))
        except UserError as ex:
            raise HTTPException(404 if "не найден" in str(ex) else 400, str(ex))

    @app.delete("/api/admin/users/{tg_id}/methods/{mid}")
    async def user_method_delete(tg_id: int, mid: int, _: dict = Depends(admin)):
        return {"deleted": await svc.deactivate_method(tg_id, mid, "admin")}

    # ───── обращения ─────

    @app.get("/api/admin/tickets")
    async def admin_tickets(status: str = "active", _: dict = Depends(admin)):
        return await tk.list_admin(status)

    @app.get("/api/admin/tickets/{tid}")
    async def admin_ticket(tid: int, _: dict = Depends(admin)):
        try:
            return await tk.get_admin(tid)
        except UserError as ex:
            raise HTTPException(404, str(ex))

    @app.post("/api/admin/tickets/{tid}/messages")
    async def admin_ticket_reply(tid: int, p: AdminMsgIn, _: dict = Depends(admin)):
        try:
            await tk.admin_reply(ctx, tid, p.text, p.internal)
        except UserError as ex:
            raise bad(ex)
        return {"ok": True}

    @app.post("/api/admin/tickets/{tid}/status")
    async def admin_ticket_status(tid: int, p: StatusIn, _: dict = Depends(admin)):
        try:
            await tk.set_status(ctx, tid, p.status)
        except UserError as ex:
            raise bad(ex)
        return {"ok": True}

    @app.post("/api/admin/tickets/{tid}/escalate")
    async def admin_ticket_escalate(tid: int, p: EscalateIn, _: dict = Depends(admin)):
        try:
            return await tk.escalate(tid, p.server_id)
        except UserError as ex:
            raise bad(ex)

    # ───── промокоды ─────

    @app.get("/api/admin/promos")
    async def promos(_: dict = Depends(admin)):
        from ..db import Promo

        async with session() as s:
            rows = (await s.execute(select(Promo).order_by(Promo.id.desc()))).scalars().all()
        return [acc.promo_dict(p) for p in rows]

    @app.post("/api/admin/promos")
    async def promo_create(p: PromoIn, _: dict = Depends(admin)):
        try:
            return acc.promo_dict(await acc.create_promo(p.code, p.kind, p.value, p.max_uses, p.days, p.new_users_only, p.note))
        except UserError as ex:
            raise bad(ex)

    @app.patch("/api/admin/promos/{pid}")
    async def promo_patch(pid: int, p: PromoPatch, _: dict = Depends(admin)):
        from ..db import Promo

        async with session() as s:
            row = await s.get(Promo, pid)
            if row is None:
                raise HTTPException(404)
            row.active = p.active
            await s.commit()
            return acc.promo_dict(row)

    @app.delete("/api/admin/promos/{pid}")
    async def promo_delete(pid: int, _: dict = Depends(admin)):
        from ..db import Promo

        async with session() as s:
            row = await s.get(Promo, pid)
            if row is None:
                raise HTTPException(404)
            if row.used:
                row.active = False  # историю активаций сохраняем
                await s.commit()
                return {"deleted": False, "deactivated": True}
            await s.delete(row)
            await s.commit()
        return {"deleted": True}

    # ───── документы ─────

    @app.post("/api/admin/docs/publish")
    async def docs_publish(_: dict = Depends(admin)):
        async with session() as s:
            cfg = await load_settings(s)
            missing = [k for k in ("seller_name", "seller_inn", "seller_email") if not str(cfg.get(k) or "").strip()]
            if missing:
                raise HTTPException(400, "Сначала заполните ФИО, ИНН и почту исполнителя.")
            from datetime import datetime

            new = await save_settings(s, {"docs_version": int(cfg["docs_version"]) + 1, "docs_date": utcnow().astimezone().strftime("%d.%m.%Y")})
        return {"docs_version": new["docs_version"], "docs_date": new["docs_date"]}

    @app.post("/api/admin/users/{tg_id}/balance")
    async def adjust_balance(tg_id: int, p: BalancePatch, adm: dict = Depends(admin)):
        kop = int(round(p.delta_rub * 100))
        async with session() as s:
            if await s.get(User, tg_id) is None:
                raise HTTPException(404, "user not found")
            if not await change_balance(s, tg_id, kop, "admin_adjust", f"by:{adm['id']}"):
                raise HTTPException(400, "баланс не может уйти в минус")
            u = await s.get(User, tg_id)
        return {"balance_rub": u.balance_kop / 100}

    @app.post("/api/admin/users/{tg_id}/ban")
    async def ban(tg_id: int, p: BanPatch, _: dict = Depends(admin)):
        async with session() as s:
            u = await s.get(User, tg_id)
            if u is None:
                raise HTTPException(404)
            u.banned = p.banned
            await s.commit()
        return {"banned": p.banned}

    @app.get("/api/admin/payments")
    async def payments(_: dict = Depends(admin)):
        async with session() as s:
            rows = (await s.execute(select(Payment).order_by(Payment.id.desc()).limit(25))).scalars().all()
        return [
            {"id": p.id, "tg_id": p.tg_id, "amount_rub": p.amount_kop / 100, "status": p.status, "receipt": p.receipt_status,
             "receipt_url": p.receipt_url, "created": p.created_at.isoformat()}
            for p in rows
        ]

    @app.post("/api/admin/payments/{pid}/retry-receipt")
    async def retry_receipt(pid: int, _: dict = Depends(admin)):
        async with session() as s:
            p = await s.get(Payment, pid)
            if p is None or p.status != "succeeded" or p.receipt_status == "done":
                raise HTTPException(400, "нечего повторять")
            p.receipt_status, p.receipt_attempts = "pending", 0
            await s.commit()
        await svc.process_receipts(ctx)
        return {"ok": True}

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def on_validation(_: Request, ex: RequestValidationError):
        msgs = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', [])[1:])}: {e.get('msg')}" for e in ex.errors())
        return JSONResponse({"detail": f"Неверные данные: {msgs}"}, status_code=422)

    @app.exception_handler(Exception)
    async def on_error(_: Request, ex: Exception):
        log.exception("unhandled")
        return JSONResponse({"error": "internal"}, status_code=500)

    return app
