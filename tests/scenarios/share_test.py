import asyncio, os, sys, json, time, hmac, hashlib
from urllib.parse import urlencode
from datetime import timedelta
TMP = sys.argv[1]
os.environ.update(BOT_TOKEN="123:TEST", ASTRA_API_KEY="k", ADMIN_IDS="1", PUBLIC_URL="https://bot.test", WEBHOOK_SECRET="sec",
                  DATABASE_URL=f"sqlite+aiosqlite:///{TMP}/t.db")
sys.path.insert(0, os.environ.get("REPO_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from app import db as D, services as svc
from app.web.server import build_app
from httpx import AsyncClient, ASGITransport

def init(uid, name="Иван"):
    f = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": name, "username": f"user{uid}"}), "query_id": "q"}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
    sec = hmac.new(b"WebAppData", b"123:TEST", hashlib.sha256).digest()
    return urlencode({**f, "hash": hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest()})

PRESETS = [{"idx": 0, "cpu": 1, "ram": 1, "disk": 10, "price_month_rub": {"nl": 188}}, {"idx": 1, "cpu": 2, "ram": 4, "disk": 60, "price_month_rub": {"nl": 675}}]
class FakeAstra:
    def __init__(s): s.n = 100
    async def catalog(s): return {"locations": [{"code": "nl", "name": "🇳🇱 Нидерланды", "available": True}], "presets": PRESETS, "os": ["ubuntu2404"]}
    async def balance(s): return {"balance_rub": 5000, "discount_pct": 25}
    async def create(s, **kw): s.n += 1; s.last = kw; return {"id": s.n, "spec": {"cpu": 1, "ram": 1, "disk": 10, "os": kw["os"]}, "charged_rub": 188, "expires_in_days": 30}
    async def servers(s): return []
    async def server(s, sid): return {"id": sid, "status": "active", "price_month_rub": 188}
    async def action(s, sid, a, body=None): s.last_action = (a, body); return {"ok": True, "url": "https://vnc.example/x"} if a == "vnc" else {"ok": True}
    async def delete(s, sid): return {"ok": True}
    async def close(s): pass
class FakeYK:
    configured = True; shop_id = "1"; _secret = "x"
    def __init__(s): s.pays = {}; s.n = 0
    async def create_payment(s, **kw):
        s.n += 1; pid = f"yk{s.n}"; s.pays[pid] = {"id": pid, "status": "succeeded", "paid": True, "amount": {"value": f"{kw['amount_kop']/100:.2f}", "currency": "RUB"},
            "payment_method": {"id": "pm_1", "saved": bool(kw.get("save_payment_method")), "type": "bank_card", "title": "Bank card *4444", "card": {"last4": "4444", "card_type": "MasterCard", "expiry_month": "12", "expiry_year": "2029"}}}
        s.last = kw; return {"id": pid, "confirmation": {"confirmation_url": "https://pay/" + pid}}
    async def charge_saved(s, **kw):
        s.n += 1; pid = f"yk{s.n}"; s.pays[pid] = {"id": pid, "status": "succeeded", "paid": True, "amount": {"value": f"{kw['amount_kop']/100:.2f}", "currency": "RUB"}, "payment_method": {"id": kw["method_id"], "saved": True}}
        s.charged = kw; return s.pays[pid]
    async def get_payment(s, pid): return s.pays[pid]
    async def close(s): pass
class FakeNalog:
    enabled = False
    async def close(s): pass

msgs = []
async def notify(uid, t): msgs.append((uid, t))
async def alert(t): msgs.append(("admin", t))

def ok(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond: ok.fails += 1
ok.fails = 0

async def main():
    await D.init_db()
    ctx = svc.Ctx(astra=FakeAstra(), yk=FakeYK(), nalog=FakeNalog(), notify=notify, alert_admin=alert)
    OFFERS = []
    async def offer(uid, text, tid): OFFERS.append((uid, text, tid))
    ctx.send_transfer_offer = offer
    SHO = []
    async def shoffer(uid, text, tid): SHO.append((uid, text, tid))
    ctx.send_share_offer = shoffer
    app = build_app(ctx)
    from sqlalchemy import select
    DOCS4 = {"docs": ["terms", "aup", "privacy", "pd"]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://bot.test") as c:
        A, B, C, E = {"X-Init-Data": init(51, "Аня")}, {"X-Init-Data": init(52, "Боря")}, {"X-Init-Data": init(53, "Вова")}, {"X-Init-Data": init(54, "Женя")}
        for h in (A, B, E): await c.get("/api/me", headers=h); await c.post("/api/me/consent", headers=h, json=DOCS4)
        await c.get("/api/me", headers=C)
        async with D.session() as s:
            await D.change_balance(s, 51, 200000, "admin_adjust"); await D.change_balance(s, 52, 100000, "admin_adjust")
        r = await c.post("/api/me/servers", headers=A, json={"idx": 1, "location": "nl", "os": "ubuntu2404"}); sid = r.json()["id"]
        async with D.session() as s:
            srv = await s.get(D.Server, sid); srv.status = "active"; srv.ip = "1.1.1.1"; srv.root_password = "Pw123"; await s.commit()
        r = await c.post(f"/api/me/servers/{sid}/share", headers=A, json={"to": "user53"}); ok(r.status_code == 400, "не клиенту доступ не дать")
        r = await c.post(f"/api/me/servers/{sid}/share", headers=A, json={"to": "51"}); ok(r.status_code == 400, "себе доступ не дать")
        r = await c.post(f"/api/me/servers/{sid}/share", headers=B, json={"to": "51"}); ok(r.status_code == 400, "чужим сервером не поделиться")
        r = await c.post(f"/api/me/servers/{sid}/share", headers=A, json={"to": "@User52"}); ok(r.status_code == 200 and SHO and SHO[-1][0] == 52, "приглашение по @username")
        shid = r.json()["id"]
        ok("открывают доступ" in SHO[-1][1] and "Аня" in SHO[-1][1], "в приглашении — владелец и права")
        r = await c.post(f"/api/me/servers/{sid}/share", headers=A, json={"to": "52"}); ok(r.status_code == 400 and "уже отправлено" in r.json()["detail"], "повторно того же не пригласить")
        jb = (await c.get("/api/me", headers=B)).json(); ja = (await c.get("/api/me", headers=A)).json()
        ok(len(jb["share_invites"]) == 1 and not jb["servers"], "у приглашённого — приглашение, сервера ещё нет")
        ok(ja["servers"][0]["members"][0]["status"] == "pending", "у владельца — ожидающее приглашение")
        r = await c.post(f"/api/me/servers/{sid}/action", headers=B, json={"action": "reboot"}); ok(r.status_code in (400, 404), "до принятия управлять нельзя")
        r = await c.post(f"/api/me/shares/{shid}/accept", headers=A); ok(r.status_code == 400, "владелец не принимает за друга")
        r = await c.post(f"/api/me/shares/{shid}/accept", headers=B); ok(r.status_code == 200 and any(u == 51 and "принял" in t for u, t in msgs), "друг принял — владелец уведомлён")
        jb = (await c.get("/api/me", headers=B)).json(); ja = (await c.get("/api/me", headers=A)).json()
        sb = jb["servers"][0] if jb["servers"] else {}
        ok(sb.get("id") == sid and sb.get("role") == "member" and sb.get("password") == "Pw123" and sb.get("owner", {}).get("tg_id") == 51, "сервер у обоих: у друга с ролью участника и паролем")
        ok(ja["servers"][0]["role"] == "owner" and ja["servers"][0]["members"][0]["status"] == "active", "у владельца участник активен")
        for a in ("reboot", "stop", "start", "vnc"):
            r = await c.post(f"/api/me/servers/{sid}/action", headers=B, json={"action": a}); ok(r.status_code == 200, f"участник: {a}")
        r = await c.get(f"/api/me/servers/{sid}/live", headers=B); ok(r.status_code == 200, "участник видит состояние")
        for a, body in (("change-ip", {}), ("password", {}), ("reinstall", {"os": "ubuntu2404"})):
            r = await c.post(f"/api/me/servers/{sid}/action", headers=B, json={"action": a, **body}); ok(r.status_code == 400 and "только владельцу" in r.json()["detail"], f"участнику нельзя: {a}")
        r = await c.post(f"/api/me/servers/{sid}/rename", headers=B, json={"name": "hack"}); ok(r.status_code in (400, 404), "участнику нельзя переименовать")
        r = await c.post(f"/api/me/servers/{sid}/autorenew", headers=B, json={"value": False}); ok(r.status_code in (400, 404), "участнику нельзя автопродление")
        r = await c.delete(f"/api/me/servers/{sid}", headers=B); ok(r.status_code in (400, 404), "участнику нельзя удалить")
        r = await c.post(f"/api/me/servers/{sid}/transfer", headers=B, json={"to": "54"}); ok(r.status_code == 400, "участнику нельзя передать")
        r = await c.post(f"/api/me/servers/{sid}/share", headers=B, json={"to": "54"}); ok(r.status_code == 400, "участнику нельзя приглашать")
        async with D.session() as s:
            a0, b0, srv0 = (await s.get(D.User, 51)).balance_kop, (await s.get(D.User, 52)).balance_kop, await s.get(D.Server, sid)
            exp0, price = srv0.expires_at, srv0.price_kop
        r = await c.post(f"/api/me/servers/{sid}/renew", headers=B); ok(r.status_code == 200, "участник продлил")
        async with D.session() as s:
            a1, b1, srv1 = (await s.get(D.User, 51)).balance_kop, (await s.get(D.User, 52)).balance_kop, await s.get(D.Server, sid)
        ok(b0 - b1 == price and a1 == a0, "продление списано с баланса участника, у владельца без изменений")
        ok(srv1.expires_at > exp0 and srv1.tg_id == 51, "срок продлён, сервер остался у владельца")
        ok(any(u == 51 and "продлил" in t for u, t in msgs), "владелец узнал о продлении")
        # лимит
        for uid in range(60, 66):
            h = {"X-Init-Data": init(uid, f"U{uid}")}; await c.get("/api/me", headers=h); await c.post("/api/me/consent", headers=h, json=DOCS4)
        codes = [(await c.post(f"/api/me/servers/{sid}/share", headers=A, json={"to": str(uid)})).status_code for uid in range(60, 65)]
        ok(codes[:4] == [200] * 4 and codes[4] == 400, "не больше 5 участников (вместе с приглашёнными)")
        mem = (await c.get("/api/me", headers=A)).json()["servers"][0]["members"]
        pend = [m for m in mem if m["status"] == "pending"]
        r = await c.post(f"/api/me/shares/{pend[0]['id']}/remove", headers=A); ok(r.status_code == 200, "владелец отменил приглашение")
        r = await c.post(f"/api/me/shares/{pend[0]['id']}/accept", headers={"X-Init-Data": init(pend[0]['user']['tg_id'], 'x')}); ok(r.status_code == 400, "отменённое приглашение не принять")
        r = await c.post(f"/api/me/shares/{pend[1]['id']}/decline", headers={"X-Init-Data": init(pend[1]['user']['tg_id'], 'x')}); ok(r.status_code == 200, "приглашение можно отклонить")
        async with D.session() as s:
            row = await s.get(D.ServerShare, pend[2]["id"]); row.created_at = D.utcnow() - timedelta(hours=25); await s.commit()
        r = await c.post(f"/api/me/shares/{pend[2]['id']}/accept", headers={"X-Init-Data": init(pend[2]['user']['tg_id'], 'x')}); ok(r.status_code == 400 and "истекло" in r.json()["detail"], "приглашение истекает через 24 часа")
        # выход и закрытие
        r = await c.post(f"/api/me/servers/{sid}/leave", headers=B); ok(r.status_code == 200 and not (await c.get("/api/me", headers=B)).json()["servers"], "участник вышел — сервер пропал у него")
        ok(any(u == 51 and "вышел" in t for u, t in msgs), "владелец узнал о выходе")
        r = await c.post(f"/api/me/servers/{sid}/action", headers=B, json={"action": "reboot"}); ok(r.status_code in (400, 404), "после выхода управлять нельзя")
        r = await c.post(f"/api/me/servers/{sid}/share", headers=A, json={"to": "52"}); sh2 = r.json()["id"]
        await c.post(f"/api/me/shares/{sh2}/accept", headers=B)
        r = await c.post(f"/api/me/shares/{sh2}/remove", headers=A); ok(r.status_code == 200 and not (await c.get("/api/me", headers=B)).json()["servers"], "владелец закрыл доступ")
        ok(any(u == 52 and "закрыл" in t for u, t in msgs), "участник узнал о закрытии")
        # передача закрывает доступ
        r = await c.post(f"/api/me/servers/{sid}/share", headers=A, json={"to": "52"}); sh3 = r.json()["id"]; await c.post(f"/api/me/shares/{sh3}/accept", headers=B)
        r = await c.post(f"/api/me/servers/{sid}/transfer", headers=A, json={"to": "54"}); tid = r.json()["id"]
        r = await c.post(f"/api/me/transfers/{tid}/accept", headers=E); ok(r.status_code == 200, "сервер передан другому")
        jb = (await c.get("/api/me", headers=B)).json(); je = (await c.get("/api/me", headers=E)).json()
        ok(not jb["servers"] and je["servers"][0]["members"] == [], "после передачи доступ закрыт у всех")
        ok(any(u == 52 and "передан другому владельцу" in t for u, t in msgs), "участник узнал, что доступ закрыт из-за передачи")
        # удаление закрывает доступ
        r = await c.post(f"/api/me/servers/{sid}/share", headers=E, json={"to": "52"}); sh4 = r.json()["id"]; await c.post(f"/api/me/shares/{sh4}/accept", headers=B)
        async with D.session() as s:
            from app.settings import save_settings as _ss
            await _ss(s, {"confirm_codes": False}); await s.commit()
        r = await c.delete(f"/api/me/servers/{sid}", headers=E); ok(r.status_code == 200, "владелец удалил сервер")
        ok(not (await c.get("/api/me", headers=B)).json()["servers"] and any(u == 52 and "удалил" in t for u, t in msgs), "после удаления сервер пропал у участника, он уведомлён")
        t = (await c.get("/legal/terms")).text; ok("4.8" in t and "общий доступ" in t, "пункт об общем доступе в оферте")
        # отключение в админке
        async with D.session() as s:
            from app.settings import save_settings
            await save_settings(s, {"shares_enabled": False}); await s.commit()
        r = await c.post("/api/me/servers", headers=A, json={"idx": 0, "location": "nl", "os": "ubuntu2404"}); sid2 = r.json()["id"]
        async with D.session() as s:
            x = await s.get(D.Server, sid2); x.status = "active"; await s.commit()
        r = await c.post(f"/api/me/servers/{sid2}/share", headers=A, json={"to": "52"}); ok(r.status_code == 400 and "отключён" in r.json()["detail"], "выключено в админке — приглашать нельзя")
    print("FAILS:", ok.fails)

asyncio.run(main())
