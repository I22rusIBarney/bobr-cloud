import asyncio, os, sys
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db", VNC_DOMAIN="vnc.bobr-cloud.online")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
import httpx
from app import db as D, services as svc
from app.web import vnc_page
from app.web.server import build_app
from httpx import AsyncClient, ASGITransport

def ok(c, l):
    print(("PASS " if c else "FAIL ") + l); ok.f += 0 if c else 1
ok.f = 0
PAGE = '<html><script>const url = "wss://vnc.astravps.co/vnc-ws/" + "TOK"; const rfb = new RFB(el, "wss://vnc.astravps.co/vnc-ws/y5HqR6hqjDQqsTon3ftt3EAL56vsQhFG");</script><link href="https://VNC.astravps.co/novnc/app.css"></html>'
seen = {}
async def fake(token, headers):
    seen.update(token=token, headers=headers)
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8", "set-cookie": "s=1; Domain=vnc.astravps.co; Path=/", "content-security-policy": "default-src 'self'"}, text=PAGE)
vnc_page.fetch_page = fake

async def main():
    await D.init_db()
    ctx = svc.Ctx(astra=None, yk=None, nalog=None, notify=None, alert_admin=None)
    app = build_app(ctx)
    T = "y5HqR6hqjDQqsTon3ftt3EAL56vsQhFG"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://vnc.bobr-cloud.online") as c:
        r = await c.get(f"/_vnc/{T}", headers={"Cookie": "a=b", "Authorization": "x"})
        ok(r.status_code == 200 and "vnc.astravps.co" not in r.text.lower(), "адрес Astra в странице заменён полностью")
        ok("wss://vnc.bobr-cloud.online/vnc-ws/" + T in r.text, "подключение консоли — через наш домен")
        ok("Domain=vnc.bobr-cloud.online" in r.headers.get("set-cookie", ""), "cookie переписана на наш домен")
        ok("content-security-policy" not in r.headers and r.headers.get("cache-control") == "no-store", "без CSP Astra, без кэша")
        ok(seen["token"] == T and "cookie" in seen["headers"] and "authorization" not in seen["headers"], "к Astra уходят только нужные заголовки")
        ok(r.headers["content-type"].startswith("text/html"), "тип — HTML")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        r = await c.get(f"/_vnc/{T}"); ok(r.status_code == 404, "на домене бота страница консоли не отдаётся (не открытый прокси)")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://vnc.bobr-cloud.online") as c:
        r = await c.get("/_vnc/short"); ok(r.status_code == 404, "неверный токен — 404")
        r = await c.get("/_vnc/..%2F..%2Fetc"); ok(r.status_code == 404, "попытка выйти за путь — 404")
    print("FAILS:", ok.f)
asyncio.run(main())
