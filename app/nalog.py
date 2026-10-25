"""Чеки самозанятого через неофициальную библиотеку `nalogovich` (API «Мой налог», lknpd.nalog.ru).

ВАЖНО: библиотека неофициальная. Если сигнатуры в вашей версии отличаются,
правьте только этот файл — остальной код зависит от `create_receipt()`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class NalogError(Exception):
    pass


def _pick(obj: Any, *names: str) -> str | None:
    for n in names:
        v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
        if v:
            return str(v)
    return None


class Nalog:
    def __init__(self) -> None:
        self._client = None
        self._lock = asyncio.Lock()
        self.inn = ""
        self._password = ""

    @property
    def enabled(self) -> bool:
        return bool(self.inn and self._password)

    def configure(self, inn: str, password: str) -> None:
        """Новые данные: старая сессия сбрасывается, новая откроется при первом чеке."""
        self.inn, self._password = inn.strip(), password
        old, self._client = self._client, None
        if old is not None:
            try:
                asyncio.get_running_loop().create_task(old.__aexit__(None, None, None))
            except RuntimeError:
                pass

    async def check(self) -> None:
        """Проверка входа в «Мой налог» без создания чека. Бросает NalogError."""
        async with self._lock:
            try:
                await self._ensure()
            except Exception as ex:
                self._client = None
                raise NalogError(str(ex) or repr(ex)) from ex

    async def _ensure(self):
        if not self.enabled:
            raise NalogError("данные «Мой налог» не заданы")
        if self._client is None:
            from nalogovich.lknpd import NpdClient  # импорт здесь: библиотека нужна только при включённых чеках

            c = NpdClient(inn=self.inn, password=self._password)
            await c.__aenter__()
            await c.auth()
            self._client = c
        return self._client

    async def create_receipt(self, name: str, amount_rub: float) -> tuple[str, str]:
        """Создаёт чек, возвращает (uuid, ссылка на печатную форму)."""
        async with self._lock:
            try:
                c = await self._ensure()
                res = await c.create_check(name=name, amount=amount_rub)
            except Exception as ex:
                # сессия могла протухнуть — пересоздадим клиент при следующей попытке
                self._client = None
                raise NalogError(repr(ex)) from ex
        uuid_ = _pick(res, "approved_receipt_uuid", "approvedReceiptUuid", "receipt_uuid", "receiptUuid", "uuid")
        if not uuid_:
            raise NalogError(f"в ответе нет uuid чека: {res!r}")
        url = _pick(res, "print_url", "printUrl") or f"https://lknpd.nalog.ru/api/v1/receipt/{self.inn}/{uuid_}/print"
        return uuid_, url

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None
