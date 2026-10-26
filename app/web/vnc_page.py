"""Страница консоли VNC под своим доменом.

Страница Astra содержит абсолютный адрес подключения (wss://vnc.astravps.co/vnc-ws/<токен>),
поэтому одного прокси мало: браузер пошёл бы к Astra напрямую. Бот сам скачивает страницу
у Astra и заменяет в ней vnc.astravps.co на наш домен. Файлы консоли и поток /vnc-ws/
Caddy проксирует на Astra как есть.

Caddy направляет сюда только запросы вида /<токен> на домене консоли (bcloud vnc).
"""
from __future__ import annotations

import logging
import re

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..config import env

log = logging.getLogger(__name__)

UPSTREAM = "vnc.astravps.co"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
PASS_REQ = ("user-agent", "accept", "accept-language", "cookie")


async def fetch_page(token: str, headers: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0), follow_redirects=False) as c:
        return await c.get(f"https://{UPSTREAM}/{token}", headers=headers)


def rewrite(text: str, domain: str) -> str:
    return re.sub(re.escape(UPSTREAM), domain, text, flags=re.I)


def router() -> APIRouter:
    r = APIRouter()

    @r.get("/_vnc/{token}")
    async def vnc_page(token: str, request: Request):
        domain = (env().vnc_domain or "").strip().lower()
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if not domain or host != domain or not TOKEN_RE.fullmatch(token):
            raise HTTPException(404)
        headers = {k: v for k, v in request.headers.items() if k.lower() in PASS_REQ}
        try:
            up = await fetch_page(token, headers)
        except httpx.HTTPError:
            log.warning("vnc page fetch failed", exc_info=True)
            raise HTTPException(502, "Консоль временно недоступна, попробуйте через минуту")
        ctype = up.headers.get("content-type", "text/html; charset=utf-8")
        body = up.content
        if "text" in ctype or "javascript" in ctype or "json" in ctype:
            body = rewrite(up.text, domain).encode("utf-8")
        resp = Response(content=body, status_code=up.status_code, media_type=ctype.split(";")[0],
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        if up.headers.get("location"):
            resp.headers["Location"] = rewrite(up.headers["location"], domain)
        for c in up.headers.get_list("set-cookie"):
            resp.headers.append("Set-Cookie", re.sub(r"(?i)domain=\.?(vnc\.)?astravps\.co", f"Domain={domain}", c))
        return resp

    return r
