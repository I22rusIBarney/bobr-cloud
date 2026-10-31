"""Банки СБП: название и эмблема по идентификатору участника СБП (bank_id из ЮKassa, например 100000000111).

Источник — открытый справочник НСПК c2bmembers.json, по которому банковские приложения и платёжные страницы
показывают список банков с логотипами. Справочник кэшируется на сутки, логотипы — на диске (media/banks),
клиенту отдаются с нашего адреса: кабинет не ходит на сторонние сайты."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

MEMBERS_URL = "https://qr.nspk.ru/proxyapp/c2bmembers.json"
TTL = 86400
_dir: dict[str, dict] = {}
_loaded_at = 0.0
_lock = asyncio.Lock()
_media: Path | None = None


def setup(media_dir: Path) -> None:
    global _media
    _media = media_dir / "banks"


def valid_id(bank_id: str | None) -> bool:
    return bool(bank_id) and bool(re.fullmatch(r"\d{6,16}", str(bank_id)))


def _parse(data: dict) -> dict[str, dict]:
    out = {}
    for b in data.get("dictionary") or []:
        m = re.search(r"(\d{12})", str(b.get("schema") or "")) or re.search(r"bank(\d{12})", str(b.get("logoURL") or ""))
        if m and b.get("bankName"):
            out[m.group(1)] = {"name": str(b["bankName"]).strip(), "logo_src": str(b.get("logoURL") or "")}
    return out


async def directory(fresh: bool = False) -> dict[str, dict]:
    """bank_id → {name, logo_src}. Сеть недоступна — последний сохранённый справочник."""
    global _dir, _loaded_at
    if _dir and not fresh and time.time() - _loaded_at < TTL:
        return _dir
    async with _lock:
        if _dir and not fresh and time.time() - _loaded_at < TTL:
            return _dir
        cache = (_media / "members.json") if _media else None
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get(MEMBERS_URL)
                r.raise_for_status()
                parsed = _parse(r.json())
            if parsed:
                _dir, _loaded_at = parsed, time.time()
                if cache:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps(parsed, ensure_ascii=False))
        except Exception as ex:  # noqa: BLE001 — эмблема банка не стоит ошибки в кабинете
            log.info("справочник банков СБП недоступен: %s", ex)
            if not _dir and cache and cache.exists():
                try:
                    _dir = json.loads(cache.read_text())
                except ValueError:
                    _dir = {}
            _loaded_at = time.time() - TTL + 600  # повторим через 10 минут
    return _dir


# если справочник НСПК недоступен или банка в нём нет — крупные банки по БИК
BIC_NAMES = {"044525225": "Сбербанк", "044525187": "ВТБ", "044525593": "Альфа-Банк", "044525974": "Т-Банк",
             "044525700": "Райффайзенбанк", "044525823": "Газпромбанк"}
LOGO_PATTERN = "https://qr.nspk.ru/proxyapp/logo/bank{}.png"
# как искать банк из BIC_NAMES в справочнике по названию (номер участника у ЮKassa и НСПК может не совпасть)
NAME_KEYS = {"Сбербанк": ("сбербанк", "сбер"), "ВТБ": ("втб",), "Альфа-Банк": ("альфа",), "Т-Банк": ("т-банк", "тинькофф"),
             "Райффайзенбанк": ("райффайзен",), "Газпромбанк": ("газпромбанк",)}


def _core(name: str) -> str:
    """«Банк ВТБ (ПАО)» → «втб», «АО «АЛЬФА-БАНК»» → «альфа»: без правовой формы, кавычек, скобок и слова «банк»."""
    n = re.sub(r"\([^)]*\)|[«»\"']", " ", name.casefold())
    n = re.sub(r"\b(пао|ао|оао|зао|ооо|нко|кб|акб|ак)\b", " ", n)
    n = re.sub(r"-?банк\b|\bбанк-?", " ", n)
    return " ".join(n.split())


def _by_name(d: dict[str, dict], name: str) -> str | None:
    """Номер банка в справочнике по названию. Полное совпадение сути названия важнее частичного:
    «Банк ВТБ (ПАО)», а не «ВТБ Лизинг»."""
    keys = NAME_KEYS.get(name) or (name.casefold(),)
    hits = []
    for bid, b in d.items():
        core = _core(b["name"])
        if any(k in b["name"].casefold() for k in keys):
            hits.append((0 if core in keys or any(core == _core(k) for k in keys) else 1, len(core), bid))
    return min(hits)[2] if hits else None


async def info(bank_id: str | None, bic: str | None = None) -> dict | None:
    """{id, name, logo} для кабинета или None, если банк неизвестен."""
    if not valid_id(bank_id):
        return {"id": "", "name": BIC_NAMES[bic], "logo": None} if bic in BIC_NAMES else None
    d = await directory()
    b = d.get(str(bank_id))
    if not b:  # нет в справочнике под этим номером — имя по БИК, эмблема — у того же банка в справочнике по названию
        name = BIC_NAMES.get(bic or "")
        alt = _by_name(d, name) if name else None
        log.info("СБП банк: номера %s нет в справочнике НСПК (%d банков), БИК %s → %s", bank_id, len(d), bic, alt or "не найден")
        logo_id = alt or str(bank_id)
        return {"id": str(bank_id), "name": name or "Банк СБП", "logo": f"/bank-logo/{logo_id}.png", "unknown": not name}
    return {"id": str(bank_id), "name": b["name"], "logo": f"/bank-logo/{bank_id}.png" if b.get("logo_src") else None}


async def logo(bank_id: str) -> bytes | None:
    """PNG-эмблема банка: с диска, иначе скачиваем по ссылке из справочника и сохраняем."""
    if not valid_id(bank_id) or _media is None:
        return None
    f = _media / f"{bank_id}.png"
    if f.exists() and time.time() - f.stat().st_mtime < 30 * TTL:
        return f.read_bytes()
    src = ((await directory()).get(bank_id) or {}).get("logo_src") or LOGO_PATTERN.format(bank_id)
    if not src or not src.startswith("https://"):
        return f.read_bytes() if f.exists() else None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(src)
            r.raise_for_status()
        data = r.content
        if not data.startswith(b"\x89PNG") or len(data) > 512 * 1024:
            return None
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        return data
    except Exception as ex:  # noqa: BLE001
        log.info("эмблема банка %s: %s", bank_id, ex)
        return f.read_bytes() if f.exists() else None
