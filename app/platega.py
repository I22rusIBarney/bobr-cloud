"""Клиент Platega (platega.io): платёжная страница с СБП, картами и другими методами.

Интерфейс повторяет нужную часть клиента ЮKassa (create_payment / get_payment), чтобы
зачисление, поллинг и чеки работали одинаково для обоих провайдеров.

API: POST /v2/transaction/process (способ оплаты клиент выбирает на странице Platega),
GET /transaction/{id} — статус. Авторизация — заголовки X-MerchantId и X-Secret.
Уведомления (callback) приходят с теми же заголовками; мы сверяем их и всё равно
перепроверяем платёж запросом статуса.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

API = "https://app.platega.io"
SUCCESS = {"CONFIRMED", "SUCCESS", "SUCCEEDED", "PAID", "COMPLETED"}
FAILED = {"CANCELED", "CANCELLED", "CHARGEBACK", "CHARGEBACKED", "EXPIRED", "FAILED", "DECLINED", "REJECTED", "ERROR"}


class PlategaError(Exception):
    def __init__(self, status: int, description: str):
        super().__init__(f"{status}: {description}")
        self.status, self.description = status, description


class Platega:
    def __init__(self) -> None:
        self.merchant_id = ""
        self._secret = ""
        self._http: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.merchant_id and self._secret)

    def configure(self, merchant_id: str, secret: str) -> None:
        old = self._http
        self.merchant_id, self._secret = (merchant_id or "").strip(), (secret or "").strip()
        self._http = None
        if old is not None:
            try:
                asyncio.get_running_loop().create_task(old.aclose())
            except RuntimeError:
                pass

    def _client(self) -> httpx.AsyncClient:
        if not self.configured:
            raise PlategaError(0, "Platega не настроена")
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=API, timeout=httpx.Timeout(30.0, connect=10.0),
                                           headers={"X-MerchantId": self.merchant_id, "X-Secret": self._secret})
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def verify_callback(self, merchant_id: str | None, secret: str | None) -> bool:
        """Platega подтверждает уведомление, присылая наши же X-MerchantId и X-Secret."""
        if not self.configured:
            return False
        return hmac.compare_digest(merchant_id or "", self.merchant_id) and hmac.compare_digest(secret or "", self._secret)

    @staticmethod
    def _err(r: httpx.Response) -> PlategaError:
        try:
            j = r.json()
            desc = j.get("message") or j.get("error") or j.get("title") or str(j)[:200]
        except ValueError:
            desc = r.text[:200]
        return PlategaError(r.status_code, desc)

    async def create_payment(self, *, amount_kop: int, description: str, return_url: str, metadata: dict[str, Any], method: int | None = None, **_: Any) -> dict:
        """Создаёт платёж. Ответ в формате ЮKassa: {"id", "confirmation": {"confirmation_url"}}."""
        amount = amount_kop / 100
        body: dict[str, Any] = {
            "paymentDetails": {"amount": int(amount) if amount == int(amount) else round(amount, 2), "currency": "RUB"},
            "description": description[:120],
            "return": return_url,
            "failedUrl": return_url,
            "payload": str(metadata.get("payment_id", "")),
        }
        if method:
            body["paymentMethod"] = int(method)
        c = self._client()
        r = await c.post("/v2/transaction/process", json=body)
        if r.status_code == 404:  # старый API: способ оплаты обязателен (2 — СБП)
            body.setdefault("paymentMethod", 2)
            r = await c.post("/transaction/process", json=body)
        if r.status_code >= 400:
            raise self._err(r)
        j = r.json()
        tid = j.get("transactionId") or j.get("id")
        url = j.get("url") or j.get("redirect") or j.get("paymentUrl")
        if not tid or not url:
            raise PlategaError(r.status_code, f"неожиданный ответ: {str(j)[:200]}")
        return {"id": str(tid), "confirmation": {"confirmation_url": url}, "raw": j}

    async def get_payment(self, tid: str) -> dict:
        """Статус в формате ЮKassa: status succeeded|canceled|pending, paid, amount{value, currency}."""
        r = await self._client().get(f"/transaction/{tid}")
        if r.status_code >= 400:
            raise self._err(r)
        j = r.json()
        st = str(j.get("status", "")).upper()
        det = j.get("paymentDetails") or {}
        amount = det.get("amount", j.get("amount", 0))
        status = "succeeded" if st in SUCCESS else "canceled" if st in FAILED else "pending"
        return {"id": str(j.get("id") or tid), "status": status, "paid": status == "succeeded",
                "amount": {"value": f"{float(amount or 0):.2f}", "currency": (det.get("currency") or "RUB").upper()},
                "cancellation_details": {"reason": st.lower()} if status == "canceled" else None,
                "payment_method": {}, "raw_status": st}

    async def check(self) -> tuple[bool, str]:
        """Проверка ключей: запрос несуществующей транзакции. 401/403 — ключи неверные, 400/404 — ключи приняты."""
        try:
            r = await self._client().get("/transaction/00000000-0000-0000-0000-000000000000")
        except httpx.HTTPError as ex:
            return False, f"Platega недоступна: {ex}"
        if r.status_code in (401, 403):
            return False, "Platega не приняла MerchantId или секретный ключ."
        if r.status_code >= 500:
            return False, f"Platega ответила ошибкой {r.status_code}, попробуйте позже."
        return True, "Ключи приняты."
