"""Резервные копии базы в Telegram.

Копия делается штатным механизмом SQLite (целостная даже во время работы), из неё удаляются
секретные ключи ЮKassa и «Мой налог» (их нужно будет ввести заново после восстановления),
затем файл сжимается и отправляется в чат события «Бэкапы». Восстановление: bcloud restore файл.
"""
from __future__ import annotations

import asyncio
import gzip
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone

from .config import env
from .db import Setting, session, utcnow
from . import events

LIMIT = 49 * 1024 * 1024  # Bot API принимает файлы до 50 МБ


class BackupError(Exception):
    pass


def db_path() -> str:
    url = env().database_url
    if not url.startswith("sqlite") or ":///" not in url:
        raise BackupError("Резервные копии в Telegram работают только с SQLite.")
    return url.split(":///", 1)[1]


def _make(src_path: str, out_dir: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    raw = os.path.join(out_dir, "bot.db")
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(raw)
    try:
        src.backup(dst)
        dst.execute("DELETE FROM settings WHERE key LIKE 'secret:%'")
        dst.commit()
        dst.execute("VACUUM")
    finally:
        dst.close()
        src.close()
    gz = os.path.join(out_dir, f"bcloud-{stamp}.db.gz")
    with open(raw, "rb") as f, gzip.open(gz, "wb", compresslevel=9) as g:
        shutil.copyfileobj(f, g)
    os.remove(raw)
    return gz


async def run(ctx, reason: str = "по расписанию") -> dict:
    path = db_path()
    if not os.path.exists(path):
        raise BackupError("Файл базы не найден.")
    tmp = tempfile.mkdtemp(prefix="bcloud-")
    try:
        gz = await asyncio.to_thread(_make, path, tmp)
        size = os.path.getsize(gz)
        if size > LIMIT:
            raise BackupError(f"Копия больше 50 МБ ({size // 1024 // 1024} МБ) — Telegram её не примет. Используйте bcloud backup на сервере.")
        caption = (f"💾 Резервная копия базы ({reason})\n{datetime.now(timezone.utc):%d.%m.%Y %H:%M} UTC · {max(1, size // 1024)} КБ\n\n"
                   "Восстановление: загрузите файл на сервер и выполните bcloud restore имя_файла. "
                   "Ключи ЮKassa и «Мой налог» в копию не входят — введите их заново в админке.")
        err = await events.send_file(ctx, "backups", gz, caption)
        async with session() as s:
            row = await s.get(Setting, "backup_last_ts")
            val = str(int(time.time()))
            if row is None:
                s.add(Setting(key="backup_last_ts", value=val))
            else:
                row.value = val
            await s.commit()
        return {"ok": err is None, "size_kb": max(1, size // 1024), "error": err}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def last_ts() -> int:
    async with session() as s:
        row = await s.get(Setting, "backup_last_ts")
    try:
        return int(row.value) if row else 0
    except ValueError:
        return 0


async def scheduled(ctx) -> None:
    """Воркер: делает копию, если пора (backup_hours > 0)."""
    from .settings import load_settings
    async with session() as s:
        cfg = await load_settings(s)
    hours = int(cfg.get("backup_hours", 0) or 0)
    if hours <= 0:
        return
    if time.time() - await last_ts() < hours * 3600 - 60:
        return
    try:
        await run(ctx)
    except BackupError as ex:
        await ctx.alert_once("backup_err", f"⚠️ Резервная копия не создана: {ex}")
