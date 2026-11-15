"""Уведомления Astra: подпись, повтор события, новый IP, Fair Use, ограничение за SNI, включение из админки."""
import asyncio, os, sys, json, time, hmac, hashlib
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from urllib.parse import urlencode
from app import db as D, services as svc
from app.web.server import build_app
from httpx import AsyncClient, ASGITransport


def ok(c, l):
    print(("PASS " if c else "FAIL ") + l); ok.f += 0 if c else 1
ok.f = 0


def init(uid, name="Админ"):
    f = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": name}), "query_id": "q"}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
    sec = hmac.new(b"WebAppData", b"123:TEST", hashlib.sha256).digest()
    return urlencode({**f, "hash": hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest()})


class FakeAstra:
    def __init__(s):
        s.card = {"id": 501, "status": "active", "ip": "1.1.1.1"}
        s.hook = None
    async def server(s, sid): return dict(s.card)
    async def webhook_put(s, url): s.hook = url; return {"webhook": {"url": url, "enabled": True}, "signing_secret": "whsec_test"}
    async def webhook_get(s): return {"url": s.hook, "enabled": bool(s.hook)}
    async def webhook_events(s): return {"events": [{"type": "server.ip_changed", "service_id": 501}]}
    async def webhook_delete(s): s.hook = None; return {}
    async def close(s): pass


class Nop:
    configured = False; enabled = False; shop_id = ""; _secret = ""; inn = ""
    async def close(s): pass


msgs = []
async def notify(uid, t): msgs.append((uid, t))
async def alert(t): msgs.append(("admin", t))


async def card():
    """Карточка сервера, как её видит кабинет."""
    from sqlalchemy import select
    from app.web.cabinet import server_dict
    async with D.session() as s:
        srv = (await s.execute(select(D.Server).where(D.Server.astra_id == 501))).scalar_one()
    return server_dict(srv, 80)


def signed(secret, body, ts=None):
    ts = str(int(ts or time.time()))
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"X-Astra-Timestamp": ts, "X-Astra-Signature": sig, "Content-Type": "application/json"}


async def main():
    await D.init_db()
    astra = FakeAstra()
    ctx = svc.Ctx(astra=astra, yk=Nop(), nalog=Nop(), notify=notify, alert_admin=alert)
    app = build_app(ctx)
    async with D.session() as s:
        s.add(D.User(tg_id=2, first_name="Иван"))
        s.add(D.Server(astra_id=501, tg_id=2, name="vpn-1", location="nl", cpu=1, ram=1, disk=10, os="ubuntu2404",
                       price_kop=27000, status="active", ip="1.1.1.1"))
        await s.commit()

    # подпись
    now = time.time()
    raw, hd = signed("k", {"a": 1}, now)
    ok(svc.astra_signature_ok("k", hd["X-Astra-Timestamp"], raw, hd["X-Astra-Signature"], now), "верная подпись принимается")
    ok(not svc.astra_signature_ok("k", hd["X-Astra-Timestamp"], raw + b" ", hd["X-Astra-Signature"], now), "изменённое тело — отказ")
    ok(not svc.astra_signature_ok("k", hd["X-Astra-Timestamp"], raw, hd["X-Astra-Signature"], now + 600), "старое событие (повтор через 10 мин) — отказ")
    ok(svc.find_secret({"data": {"secret": "abc"}}) == "abc" and svc.find_secret({"signing_secret": "z"}) == "z", "секрет находится в любом поле ответа")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        raw, hd = signed("whsec_test", {"id": "e0", "type": "webhook.test"})
        r = await c.post("/astra/webhook", content=raw, headers=hd)
        ok(r.status_code == 404, "пока уведомления не включены, адрес не принимает события")

        HA = {"X-Init-Data": init(1)}
        r = await c.post("/api/admin/astra-webhook", headers=HA)
        ok(r.status_code == 200 and r.json()["connected"] and astra.hook == "https://bot.test/astra/webhook", "админ включил уведомления, Astra получила наш адрес")

        raw, hd = signed("other", {"id": "e1", "type": "server.ip_changed", "service_id": 501})
        r = await c.post("/astra/webhook", content=raw, headers=hd)
        ok(r.status_code == 401, "чужая подпись — 401")

        # новый IP
        astra.card["ip"] = "2.2.2.2"
        ev = {"id": "e2", "type": "server.ip_changed", "service_id": 501, "before": {"ip": "1.1.1.1"}, "after": {"ip": "2.2.2.2"}}
        raw, hd = signed("whsec_test", ev)
        r = await c.post("/astra/webhook", content=raw, headers=hd)
        await asyncio.sleep(0.3)
        ok(r.status_code == 200, "событие принято")
        ok(any(u == 2 and "2.2.2.2" in t for u, t in msgs), "клиент узнал о новом IP")
        n = len(msgs)
        raw, hd = signed("whsec_test", ev)  # повторная доставка того же события
        await c.post("/astra/webhook", content=raw, headers=hd)
        await asyncio.sleep(0.3)
        ok(len(msgs) == n, "повтор того же события не дублирует сообщение")

        # Fair Use
        astra.card["fup"] = {"limited": True, "limit_mbit": 100, "until": "2030-01-01T10:00:00+00:00"}
        raw, hd = signed("whsec_test", {"id": "e3", "type": "server.fup_changed", "service_id": 501})
        await c.post("/astra/webhook", content=raw, headers=hd)
        await asyncio.sleep(0.3)
        ok(any(u == 2 and "ограничена" in t and "100" in t for u, t in msgs), "клиент узнал об ограничении скорости")
        x = await card()
        ok(x["fup"] and x["fup"]["mbit"] == 100, "в кабинете видно ограничение скорости")
        astra.card["fup"] = {"limited": False}
        raw, hd = signed("whsec_test", {"id": "e4", "type": "server.fup_changed", "service_id": 501})
        await c.post("/astra/webhook", content=raw, headers=hd)
        await asyncio.sleep(0.3)
        ok(any(u == 2 and "снова полная" in t for u, t in msgs), "клиент узнал, что ограничение снято")
        astra.card["fup"] = None  # null — Astra не прочитала состояние: прежнее не трогаем
        astra.card["extra_ips"] = '["3.3.3.3"]'
        astra.card["ipv6"] = {"subnet": "2a01:db8::/64"}
        await svc.refresh_server(ctx, 501)
        x = await card()
        ok(x["extra_ips"] == ["3.3.3.3"] and x["ipv6"] == "2a01:db8::/64" and x["fup"] is None, "доп. IPv4 и IPv6 — в карточке сервера")

        # ограничение за SNI
        raw, hd = signed("whsec_test", {"id": "e5", "type": "sni.suspended", "service_id": 501, "incident": {"action_required": "request_correction_access"}})
        await c.post("/astra/webhook", content=raw, headers=hd)
        await asyncio.sleep(0.3)
        ok(any(u == 2 and "ограничен провайдером" in t for u, t in msgs), "клиент узнал об ограничении за SNI")
        ok(any(u == "admin" and "sni.suspended" in t for u, t in msgs), "админ тоже узнал")

        r = await c.delete("/api/admin/astra-webhook", headers=HA)
        ok(r.status_code == 200 and not r.json()["connected"] and astra.hook is None, "уведомления выключаются")
    print("FAILS:", ok.f)

asyncio.run(main())
