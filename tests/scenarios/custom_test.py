import asyncio, os, sys, json, time, hmac, hashlib
from urllib.parse import urlencode
from datetime import timedelta
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from app import db as D, services as svc
from sqlalchemy import select
from app.astra import AstraError, AstraUncertain
from app.web.server import build_app
from httpx import AsyncClient, ASGITransport


def init(uid, name="Иван"):
    f = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": name, "username": f"user{uid}"}), "query_id": "q"}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
    sec = hmac.new(b"WebAppData", b"123:TEST", hashlib.sha256).digest()
    return urlencode({**f, "hash": hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest()})


LOCS = [{"code": "nl", "name": "🇳🇱 Нидерланды", "channel": "до 25 Гбит/с · shared, не гарантирован", "available": True, "virtual": False, "physical_location": "nl", "description": None},
        {"code": "cz", "name": "🇨🇿 Чехия", "channel": "до 10 Гбит/с · shared", "available": True, "virtual": True, "physical_location": "de", "description": "IP Чехии · сервер во Франкфурте (DE)"},
        {"code": "ch", "name": "🇨🇭 Швейцария", "channel": "до 10 Гбит/с", "available": False, "virtual": True, "physical_location": "de", "description": "IP Швейцарии · сервер во Франкфурте (DE)"}]
PRESETS = [{"idx": 0, "cpu": 1, "ram": 1, "disk": 10, "price_month_rub": {"nl": 188.0, "cz": 188.0}, "renew_month_rub": {"nl": 200.0, "cz": 188.0}},
           {"idx": 1, "cpu": 2, "ram": 4, "disk": 60, "price_month_rub": {"nl": 675.0, "cz": 675.0}, "renew_month_rub": {"nl": 675.0, "cz": 675.0}}]
CONF = {"cpu": {"min": 1, "max": 16, "step": 1, "max_is_allowed": True}, "ram": {"min": 1, "max": 64, "step": 1, "max_is_allowed": True},
        "disk": {"min": 20, "max": 1024, "step": 10, "max_is_allowed": True}}


class FakeAstra:
    def __init__(s): s.n = 100; s.quotes = 0; s.ip_status = []; s.action_exc = None
    async def catalog(s): return {"locations": LOCS, "presets": PRESETS, "os": ["ubuntu2404"], "periods": ["1m", "3m"], "configurator": CONF}
    async def balance(s): return {"balance_rub": 5000, "discount_pct": 25}
    async def quote(s, *, location, cpu, ram, disk, period="1m"):
        s.quotes += 1
        if (disk - 20) % 10:
            raise AstraError(400, {"error": "invalid_configuration", "hint": "invalid step for disk"})
        p = 100 * cpu + 60 * ram + 3 * disk
        return {"ok": True, "quote_only": True, "spec": {"cpu": cpu, "ram": ram, "disk": disk, "location": location, "port": "25gbps" if cpu > 1 else "200mbps"},
                "period": period, "price_rub": float(p), "renew_month_rub": float(p), "expires_in_days": 30, "discount_pct": 25.0}
    async def create(s, **kw):
        s.n += 1; s.last = kw
        spec = {"cpu": kw.get("cpu") or 1, "ram": kw.get("ram") or 1, "disk": kw.get("disk") or 10, "os": kw["os"]}
        months = {"1m": 1, "3m": 3}[kw.get("period", "1m")]
        charged = (100 * spec["cpu"] + 60 * spec["ram"] + 3 * spec["disk"]) * months if "cpu" in kw else 188 * months
        return {"id": s.n, "spec": spec, "charged_rub": charged, "expires_in_days": 30 * months}
    async def servers(s): return []
    async def server(s, sid): return {"id": sid, "status": "active"}
    async def action(s, sid, a, body=None):
        if s.action_exc: raise s.action_exc
        return {"ok": True}
    async def change_ip_status(s, sid): return s.ip_status.pop(0) if s.ip_status else {}
    async def delete(s, sid): return {"ok": True}
    async def close(s): pass


class FakeYK:
    configured = False
    async def close(s): pass
class FakeNalog:
    enabled = False
    async def close(s): pass


msgs = []
async def notify(uid, t): msgs.append((uid, t))
async def alert(t): msgs.append(("admin", t))
def ok(c, l):
    print(("PASS " if c else "FAIL ") + l); ok.f += 0 if c else 1
ok.f = 0


async def main():
    await D.init_db()
    astra = FakeAstra()
    ctx = svc.Ctx(astra=astra, yk=FakeYK(), nalog=FakeNalog(), notify=notify, alert_admin=alert)
    app = build_app(ctx)
    DOCS4 = {"docs": ["terms", "aup", "privacy", "pd"]}
    from app.settings import save_settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        A = {"X-Init-Data": init(51, "Аня")}
        await c.get("/api/me", headers=A); await c.post("/api/me/consent", headers=A, json=DOCS4)
        async with D.session() as s:
            await D.change_balance(s, 51, 1000000, "admin_adjust")
            await save_settings(s, {"markup_pct": 40, "round_to_rub": 1}); await s.commit()
        cat = (await c.get("/api/me/catalog", headers=A)).json()
        ok(cat["configurator"]["disk"] == {"min": 20, "max": 1024, "step": 10, "max_is_allowed": True}, "каталог: диапазоны конструктора (с max_is_allowed)")
        cz = next(l for l in cat["locations"] if l["code"] == "cz")
        ok(cz["virtual"] and "Франкфурт" in cz["description"], "каталог: виртуальная локация с подписью")
        ok(not any(l["code"] == "ch" for l in cat["locations"]), "закрытая локация (available=false) не показывается")
        nl0 = next(o for o in next(l for l in cat["locations"] if l["code"] == "nl")["offers"] if o["idx"] == 0)
        ok(nl0["price_kop"] == 28000, "готовый тариф: цена от большего из покупки и продления (200 ₽ × 1.4)")
        r = await c.get("/api/me/quote", headers=A, params={"location": "nl", "cpu": 2, "ram": 3, "disk": 30})
        q = r.json()
        ok(r.status_code == 200 and q["price_kop"] == 65800 and "cost_rub" not in q, "своя конфигурация: цена клиенту с наценкой, себестоимость скрыта")
        ok(q["speed"] == "25 Гбит/с", "скорость своей конфигурации — из ответа Astra (port)")
        n = astra.quotes
        await c.get("/api/me/quote", headers=A, params={"location": "nl", "cpu": 2, "ram": 3, "disk": 30})
        ok(astra.quotes == n, "повторный расчёт — из кэша")
        r = await c.get("/api/me/quote", headers=A, params={"location": "nl", "cpu": 2, "ram": 3, "disk": 25})
        ok(r.status_code == 400 and "нет" in r.json()["detail"], "неверный шаг диска — понятная ошибка без запроса к Astra")
        r = await c.get("/api/me/quote", headers=A, params={"location": "nl", "cpu": 99, "ram": 3, "disk": 30}); ok(r.status_code == 400, "вне диапазона — ошибка")
        r = await c.get("/api/me/quote", headers=A, params={"location": "ch", "cpu": 2, "ram": 3, "disk": 30}); ok(r.status_code == 400, "закрытая локация — ошибка")
        # покупка своей конфигурации
        async with D.session() as s: b0 = (await s.get(D.User, 51)).balance_kop
        r = await c.post("/api/me/servers", headers=A, json={"cpu": 2, "ram": 3, "disk": 30, "location": "nl", "os": "ubuntu2404"})
        ok(r.status_code == 200 and "idx" not in astra.last and astra.last["cpu"] == 2 and astra.last["disk"] == 30, "покупка своей конфигурации: Astra получила cpu/ram/disk")
        async with D.session() as s:
            srv = await s.get(D.Server, r.json()["id"]); b1 = (await s.get(D.User, 51)).balance_kop
            tx = (await s.execute(select(D.Tx).where(D.Tx.kind == "purchase").order_by(D.Tx.id.desc()))).scalars().first() if hasattr(D, "select") else None
        ok(b0 - b1 == 65800 and srv.price_kop == 65800 and srv.cost_kop == 47000 and (srv.cpu, srv.ram, srv.disk) == (2, 3, 30), "списано по цене конструктора, себестоимость 470 ₽")
        r = await c.post("/api/me/servers", headers=A, json={"cpu": 2, "ram": 3, "disk": 30, "location": "nl", "os": "ubuntu2404", "period": "3m"})
        async with D.session() as s: srv3 = await s.get(D.Server, r.json()["id"])
        ok(r.status_code == 200 and srv3.period_months == 3.0 and srv3.cost_kop == 47000, "своя конфигурация на 3 месяца")
        r = await c.post("/api/me/servers", headers=A, json={"location": "nl", "os": "ubuntu2404"}); ok(r.status_code == 400, "без тарифа и конфигурации — ошибка")
        # готовый тариф: себестоимость — цена продления
        r = await c.post("/api/me/servers", headers=A, json={"idx": 0, "location": "nl", "os": "ubuntu2404"})
        async with D.session() as s: sp = await s.get(D.Server, r.json()["id"])
        ok(r.status_code == 200 and astra.last.get("idx") == 0 and sp.cost_kop == 20000 and sp.price_kop == 28000, "готовый тариф: idx, себестоимость = цена продления")
        # апгрейд через quote
        async with D.session() as s:
            x = await s.get(D.Server, srv.id); x.status = "active"; x.ip = "1.1.1.1"; await s.commit()
        r = await c.get(f"/api/me/servers/{srv.id}/upgrade", headers=A)
        ok(r.status_code == 200 and 3 in r.json()["options"]["cpu"] and 16 in r.json()["options"]["cpu"] and 64 in r.json()["options"]["ram"], "апгрейд: варианты из диапазона конструктора")
        r = await c.get(f"/api/me/servers/{srv.id}/upgrade", headers=A, params={"cpu": 3, "ram": 3})
        ok(r.status_code == 200 and r.json()["new_price_kop"] == 79800, "апгрейд: новая цена по расчёту Astra (570 ₽ × 1.4)")
        # выключено в админке
        async with D.session() as s: await save_settings(s, {"custom_enabled": False}); await s.commit()
        cat = (await c.get("/api/me/catalog", headers=A)).json(); ok(cat["configurator"] is None, "выключено: конструктор не отдаётся")
        r = await c.get("/api/me/quote", headers=A, params={"location": "nl", "cpu": 2, "ram": 5, "disk": 30}); ok(r.status_code == 400, "выключено: расчёт недоступен")
        async with D.session() as s: await save_settings(s, {"custom_enabled": True}); await s.commit()

        # ── смена IP после таймаута
        called = []
        orig = svc.verify_change_ip
        async def rec(*a, **k): called.append(a)
        svc.verify_change_ip = rec
        astra.action_exc = AstraUncertain("timeout")
        async with D.session() as s: bb = (await s.get(D.User, 51)).balance_kop
        r = await c.post(f"/api/me/servers/{srv.id}/action", headers=A, json={"action": "change-ip"})
        await asyncio.sleep(0.05)
        async with D.session() as s: ba = (await s.get(D.User, 51)).balance_kop
        ok(r.status_code == 400 and "автоматически" in r.json()["detail"] and called and bb - ba == 15000, "таймаут смены IP: деньги удержаны, запущена проверка")
        svc.verify_change_ip = orig; astra.action_exc = None
        started = D.utcnow()
        # 1) IP сменился
        astra.ip_status = [{"ip": "1.1.1.1", "last_operation": {"status": "pending", "created_at": started.isoformat()}},
                           {"ip": "2.2.2.2", "last_operation": {"status": "done", "created_at": started.isoformat()}}]
        res = await svc.verify_change_ip(ctx, srv.id, srv.astra_id, "1.1.1.1", started, 15000, 51, tries=5, delay=0)
        async with D.session() as s: x = await s.get(D.Server, srv.id)
        ok(res == "done" and x.ip == "2.2.2.2" and any(u == 51 and "2.2.2.2" in t for u, t in msgs), "IP сменился — карточка обновлена, клиент уведомлён")
        # 2) новая операция отклонена — возврат
        async with D.session() as s: b0 = (await s.get(D.User, 51)).balance_kop
        astra.ip_status = [{"ip": "2.2.2.2", "last_operation": {"status": "failed", "created_at": (D.utcnow()).isoformat()}}]
        res = await svc.verify_change_ip(ctx, srv.id, srv.astra_id, "2.2.2.2", D.utcnow(), 15000, 51, tries=3, delay=0)
        async with D.session() as s: b1 = (await s.get(D.User, 51)).balance_kop
        ok(res == "refunded" and b1 - b0 == 15000, "операция отклонена — 150 ₽ вернули")
        # 3) отказ — но старая операция: не возвращаем
        async with D.session() as s: b0 = (await s.get(D.User, 51)).balance_kop
        old = (D.utcnow() - timedelta(hours=3)).isoformat()
        astra.ip_status = [{"ip": "2.2.2.2", "last_operation": {"status": "failed", "created_at": old}}] * 3
        msgs.clear()
        res = await svc.verify_change_ip(ctx, srv.id, srv.astra_id, "2.2.2.2", D.utcnow(), 15000, 51, tries=3, delay=0)
        async with D.session() as s: b1 = (await s.get(D.User, 51)).balance_kop
        ok(res == "unknown" and b1 == b0 and any(u == "admin" and "вручную" in t for u, t in msgs), "старая операция не подтверждает новую — без возврата, админу на проверку")
        # 4) неизвестный формат ответа — тоже без автодействий
        astra.ip_status = [{"weird": True}] * 2
        res = await svc.verify_change_ip(ctx, srv.id, srv.astra_id, "2.2.2.2", D.utcnow(), 15000, 51, tries=2, delay=0)
        ok(res == "unknown", "непонятный ответ — без автоматического возврата")
        ok(svc.parse_ip_status({"current_ip": "3.3.3.3", "operation": {"status": "SUCCESS", "at": 1793023834}})[0:2] == ("3.3.3.3", "success"), "разбор статуса: альтернативные поля")
        # реальный формат Astra: current_ip, last_operation, request_correlated
        real = {"id": 4675, "current_ip": "31.77.59.117", "service_status": "active", "last_operation": None, "request_correlated": False, "hint": "..."}
        ok(svc.parse_ip_status(real)[0] == "31.77.59.117" and svc.ip_status_correlated(real) is False, "разбор реального ответа Astra")
        async with D.session() as s: b0 = (await s.get(D.User, 51)).balance_kop
        astra.ip_status = [{"current_ip": "2.2.2.2", "last_operation": {"status": "failed", "created_at": D.utcnow().isoformat()}, "request_correlated": False}] * 2
        res = await svc.verify_change_ip(ctx, srv.id, srv.astra_id, "2.2.2.2", D.utcnow(), 15000, 51, tries=2, delay=0)
        async with D.session() as s: b1 = (await s.get(D.User, 51)).balance_kop
        ok(res == "unknown" and b1 == b0, "Astra говорит «не наша операция» — без возврата, даже если время свежее")
        astra.ip_status = [{"current_ip": "2.2.2.2", "last_operation": {"status": "failed", "created_at": old}, "request_correlated": True}]
        res = await svc.verify_change_ip(ctx, srv.id, srv.astra_id, "2.2.2.2", D.utcnow(), 15000, 51, tries=1, delay=0)
        ok(res == "refunded", "Astra связала отказ с нашим запросом — возврат")
        astra.ip_status = [{"current_ip": "5.5.5.5", "last_operation": None, "request_correlated": False}]
        res = await svc.verify_change_ip(ctx, srv.id, srv.astra_id, "2.2.2.2", D.utcnow(), 15000, 51, tries=1, delay=0)
        ok(res == "done", "IP поменялся — подтверждаем по current_ip")
        ok(svc.port_label("25gbps") == "25 Гбит/с" and svc.port_label("200mbps") == "200 Мбит/с" and svc.port_label("1 Gbps") == "1 Гбит/с" and svc.port_label("x") == "", "подпись скорости порта")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        ADM, A = {"X-Init-Data": init(1, "Админ")}, {"X-Init-Data": init(51, "Аня")}
        await c.get("/api/me", headers=ADM); await c.post("/api/me/consent", headers=ADM, json=DOCS4)
        async with D.session() as s: await D.change_balance(s, 1, 500000, "admin_adjust")
        await c.post("/api/me/servers", headers=ADM, json={"idx": 0, "location": "nl", "os": "ubuntu2404"})
        ja, jc = (await c.get("/api/me", headers=ADM)).json(), (await c.get("/api/me", headers=A)).json()
        ok(ja["servers"][0].get("astra_id") and all("astra_id" not in x for x in jc["servers"]), "ID у Astra — только админу, клиенту не отдаётся")
        async with D.session() as s:
            aid = (await s.execute(select(D.Server.astra_id).where(D.Server.tg_id == 51))).scalars().first()
        r = await c.get("/api/admin/users", headers=ADM, params={"q": f"#{aid}"})
        ok(r.status_code == 200 and [u["tg_id"] for u in r.json()] == [51], "поиск клиента по ID сервера у Astra")
        from app import bot as B
        async with D.session() as s:
            sv = (await s.execute(select(D.Server).where(D.Server.tg_id == 51))).scalars().first()
        B.set_ctx(ctx)
        t_adm, _ = await B._card(sv, viewer=1); t_cli, _ = await B._card(sv, viewer=51)
        ok(f"У Astra: <code>{sv.astra_id}</code>" in t_adm and "У Astra" not in t_cli, "бот: строка с ID у Astra только для админа")
    print("FAILS:", ok.f)

asyncio.run(main())
