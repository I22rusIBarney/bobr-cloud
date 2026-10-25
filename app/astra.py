"""Клиент реселлерского API Astra VPS (https://astravps.co/reseller)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from .config import env


class AstraError(Exception):
    """Ответ API с ошибкой (4xx/5xx). Деньги Astra, как правило, не списала."""

    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(f"Astra API {status}: {payload}")

    @property
    def code(self) -> str:
        if isinstance(self.payload, dict):
            return str(self.payload.get("error", ""))
        return ""


class AstraUncertain(Exception):
    """Сеть/таймаут: неизвестно, выполнилась ли операция. Автоматически не возвращаем деньги."""


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class Astra:
    def __init__(self) -> None:
        e = env()
        self._http = httpx.AsyncClient(
            base_url=e.astra_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {e.astra_api_key}"},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, *, mutating: bool = False, **kw) -> Any:
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.HTTPError as ex:
            if mutating:
                raise AstraUncertain(f"{method} {path}: {ex!r}") from ex
            raise
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:500]}
        if r.status_code >= 400:
            raise AstraError(r.status_code, data)
        return data

    # --- чтение ---
    async def catalog(self) -> dict:
        return await self._req("GET", "/catalog")

    async def balance(self) -> dict:
        return await self._req("GET", "/balance")

    async def servers(self) -> list[dict]:
        return (await self._req("GET", "/servers")).get("servers", [])

    async def server(self, sid: int) -> dict:
        return (await self._req("GET", f"/servers/{sid}")).get("server", {})

    # --- запись ---
    async def create(self, *, idx: int, location: str, os: str, name: str, period: str = "1m", password: str | None = None) -> dict:
        body = {"idx": idx, "location": location, "os": os, "period": period, "name": name[:60]}
        if password:
            body["password"] = password
        return await self._req("POST", "/servers", json=body, mutating=True)

    async def action(self, sid: int, action: str, body: dict | None = None) -> dict:
        return await self._req("POST", f"/servers/{sid}/{action}", json=body or {}, mutating=True)

    async def delete(self, sid: int) -> dict:
        return await self._req("DELETE", f"/servers/{sid}", mutating=True)
