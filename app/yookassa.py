"""Минимальный клиент ЮKassa (REST v3) без SDK. Ключи можно менять на лету из админки."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx

API = "https://api.yookassa.ru/v3"


class NotConfigured(Exception):
    pass


class YooKassaError(Exception):
    def __init__(self, status: int, payload: Any):
        self.status, self.payload = status, payload
        super().__init__(f"ЮKassa {status}: {payload}")

    @property
    def description(self) -> str:
        return str(self.payload.get("description", "")) if isinstance(self.payload, dict) else ""


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text[:300]}


class YooKassa:
    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self.shop_id = ""
        self._secret = ""

    @property
    def configured(self) -> bool:
        return bool(self.shop_id and self._secret)

    def configure(self, shop_id: str, secret: str) -> None:
        old = self._http
        self.shop_id, self._secret = shop_id.strip(), secret.strip()
        self._http = None
        if old is not None:
            try:
                asyncio.get_running_loop().create_task(old.aclose())
            except RuntimeError:
                pass

    def _client(self) -> httpx.AsyncClient:
        if not self.configured:
            raise NotConfigured("ЮKassa не настроена")
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=API, auth=(self.shop_id, self._secret), timeout=httpx.Timeout(30.0, connect=10.0))
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def check(self) -> tuple[int, dict]:
        """GET /me: проверка ключей. Возвращает (http_status, json)."""
        r = await self._client().get("/me")
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    async def create_payment(
        self, *, amount_kop: int, description: str, return_url: str, metadata: dict[str, Any],
        idem_key: str | None = None, save_payment_method: bool = False, method: str | None = None,
    ) -> dict:
        body = {
            "amount": {"value": f"{amount_kop / 100:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:128],
            "metadata": metadata,
        }
        if save_payment_method:
            body["save_payment_method"] = True
        if method:  # sbp | bank_card: сразу нужный способ, без страницы выбора ЮKassa
            body["payment_method_data"] = {"type": method}
        r = await self._client().post("/payments", json=body, headers={"Idempotence-Key": idem_key or str(uuid.uuid4())})
        if r.status_code >= 400:
            raise YooKassaError(r.status_code, _json(r))
        return r.json()

    async def charge_saved(self, *, amount_kop: int, method_id: str, description: str, metadata: dict[str, Any], idem_key: str) -> dict:
        """Автоплатёж: списание с сохранённого способа оплаты без участия клиента."""
        body = {
            "amount": {"value": f"{amount_kop / 100:.2f}", "currency": "RUB"},
            "capture": True,
            "payment_method_id": method_id,
            "description": description[:128],
            "metadata": metadata,
        }
        r = await self._client().post("/payments", json=body, headers={"Idempotence-Key": idem_key})
        if r.status_code >= 400:
            raise YooKassaError(r.status_code, _json(r))
        return r.json()

    async def get_payment(self, yk_id: str) -> dict:
        r = await self._client().get(f"/payments/{yk_id}")
        r.raise_for_status()
        return r.json()
