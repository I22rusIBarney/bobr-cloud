"""Служебный API /api/svc: покупка по ref (повтор не покупает второй сервер), бан при переименовании.
Остальное — в tests/scenarios/svc_api_test.py."""
import asyncio

from httpx import ASGITransport, AsyncClient

from tests.test_flows import FakeAstra, FakeNalog, FakeYK, alert, notify

H = {"Authorization": "Bearer svc-token"}


class SlowAstra(FakeAstra):
    """Создание сервера ждёт, пока тест не отпустит, — чтобы повтор пришёл во время первой покупки."""

    def __init__(s):
        super().__init__()
        s.gate = asyncio.Event()
        s.creates = 0

    async def create(s, **kw):
        s.creates += 1
        await s.gate.wait()
        return await super().create(**kw)


async def main():
    from app import db as D, services as svc
    from app.web.server import build_app
    await D.init_db()
    astra = SlowAstra()
    ctx = svc.Ctx(astra=astra, yk=FakeYK(), nalog=FakeNalog(), notify=notify, alert_admin=alert)
    async with D.session() as s:
        s.add(D.User(tg_id=7, username="client7", balance_kop=100_000))
        s.add(D.User(tg_id=8, username="banned8", balance_kop=100_000, banned=True))
        await s.commit()
    app = build_app(ctx)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        # покупка с ref: повтор, пришедший во время первой, ждёт её и получает тот же сервер
        body = {"tg_id": 7, "location": "nl", "os": "ubuntu2404", "idx": 0, "ref": "job1"}
        first = asyncio.create_task(c.post("/api/svc/servers", headers=H, json=body))
        second = asyncio.create_task(c.post("/api/svc/servers", headers=H, json=body))
        await asyncio.sleep(0.2)
        assert astra.creates == 1, "повтор не должен покупать, пока идёт первая покупка"
        astra.gate.set()
        r1, r2 = await first, await second
        assert r1.status_code == r2.status_code == 200, (r1.text, r2.text)
        assert r1.json()["id"] == r2.json()["id"] and not r1.json()["repeat"] and r2.json()["repeat"]
        bal = (await c.get("/api/svc/balance", headers=H, params={"tg_id": 7})).json()["balance_kop"]
        r = await c.post("/api/svc/servers", headers=H, json={**body, "ref": "job2"})
        assert r.status_code == 200 and r.json()["id"] != r1.json()["id"] and astra.creates == 2, "другой ref — новая покупка"
        assert (await c.get("/api/svc/balance", headers=H, params={"tg_id": 7})).json()["balance_kop"] < bal

        # заблокированный клиент ничего не может, в том числе переименовать
        r = await c.post(f"/api/svc/servers/{r1.json()['id']}/rename", headers=H, json={"tg_id": 8, "name": "чужой"})
        assert r.status_code == 403


def test_service_api(tmp_path, monkeypatch):
    for k, v in dict(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                     SERVICE_TOKEN="svc-token", DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/t.db").items():
        monkeypatch.setenv(k, v)
    from app.config import env
    env.cache_clear()
    asyncio.run(main())
