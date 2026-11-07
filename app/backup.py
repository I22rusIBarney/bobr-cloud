"""Резервные копии базы в Telegram.

Копия делается штатным механизмом SQLite (целостная даже во время работы).
- Задан ключ копий (в админке): копия целиком, со всеми ключами ЮKassa, Platega и «Мой налог», сжата и
  зашифрована (AES-256-GCM, ключ из пароля — scrypt). Восстановление — загрузкой файла в админке: бот проверит
  файл, отложит текущую базу (data/bot.db.before-restore-…) и перезапустится уже с копией.
- Ключа нет: как раньше — без секретных ключей, сжатая; восстановление: bcloud restore файл.
Последние KEEP копий лежат и на сервере, в data/backups.
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
KEEP = 7
MAGIC = b"BCB1"
KEY_SETTING = "backup_key"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_key() -> str:
    """Ключ копий: 24 знака без похожих (0/O, 1/I), группами по 4 — удобно переписать."""
    import secrets
    raw = "".join(secrets.choice(ALPHABET) for _ in range(24))
    return "-".join(raw[i:i + 4] for i in range(0, 24, 4))


def norm_key(k: str) -> str:
    return "".join(ch for ch in (k or "").upper() if ch.isalnum())


def _derive(key: str, salt: bytes) -> bytes:
    import hashlib
    return hashlib.scrypt(norm_key(key).encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def encrypt(data: bytes, key: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(_derive(key, salt)).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, key: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(MAGIC) or len(blob) < 4 + 16 + 12 + 16:
        raise BackupError("Это не зашифрованная копия Bobr Cloud")
    salt, nonce = blob[4:20], blob[20:32]
    try:
        return AESGCM(_derive(key, salt)).decrypt(nonce, blob[32:], MAGIC)
    except InvalidTag:
        raise BackupError("Ключ не подходит к этой копии") from None


class BackupError(Exception):
    pass


def db_path() -> str:
    url = env().database_url
    if not url.startswith("sqlite") or ":///" not in url:
        raise BackupError("Резервные копии в Telegram работают только с SQLite.")
    return url.split(":///", 1)[1]


def _make(src_path: str, out_dir: str, key: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    raw = os.path.join(out_dir, "bot.db")
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(raw)
    try:
        src.backup(dst)
        if not key:  # без шифрования секретов в копии нет
            dst.execute("DELETE FROM settings WHERE key LIKE 'secret:%'")
            # root-пароли серверов клиентов — тоже секрет: копия без ключа уходит в чат Telegram
            if "root_password" in {r[1] for r in dst.execute("PRAGMA table_info(servers)")}:
                dst.execute("UPDATE servers SET root_password = NULL")
        dst.commit()
        dst.execute("VACUUM")
    finally:
        dst.close()
        src.close()
    if key:
        with open(raw, "rb") as f:
            blob = encrypt(gzip.compress(f.read(), 9), key)
        out = os.path.join(out_dir, f"bcloud-{stamp}.bcb")
        with open(out, "wb") as f:
            f.write(blob)
        os.remove(raw)
        return out
    gz = os.path.join(out_dir, f"bcloud-{stamp}.db.gz")
    with open(raw, "rb") as f, gzip.open(gz, "wb", compresslevel=9) as g:
        shutil.copyfileobj(f, g)
    os.remove(raw)
    return gz


def keep_local(path: str, data: str) -> None:
    """Копия остаётся и на сервере: data/backups, последние KEEP."""
    d = os.path.join(data, "backups")
    os.makedirs(d, exist_ok=True)
    shutil.copy2(path, os.path.join(d, os.path.basename(path)))
    files = sorted((f for f in os.listdir(d) if f.startswith("bcloud-")), reverse=True)
    for f in files[KEEP:]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass


def local_copies(data: str) -> list[dict]:
    d = os.path.join(data, "backups")
    try:
        names = sorted((f for f in os.listdir(d) if f.startswith("bcloud-")), reverse=True)
    except OSError:
        return []
    return [{"name": n, "size_kb": max(1, os.path.getsize(os.path.join(d, n)) // 1024), "encrypted": n.endswith(".bcb")} for n in names]


# ───────── восстановление ─────────
def open_copy(blob: bytes, key: str = "") -> bytes:
    """Файл копии → база SQLite: зашифрованная (.bcb, нужен ключ), сжатая (.gz) или сама база. Проверяет целостность."""
    if blob.startswith(MAGIC):
        if not key.strip():
            raise BackupError("Копия зашифрована — введите ключ")
        blob = gzip.decompress(decrypt(blob, key))
    elif blob[:2] == b"\x1f\x8b":
        try:
            blob = gzip.decompress(blob)
        except (OSError, EOFError):
            raise BackupError("Файл повреждён") from None
    if not blob.startswith(b"SQLite format 3"):
        raise BackupError("Это не копия базы Bobr Cloud")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(blob)
        tmp = f.name
    try:
        con = sqlite3.connect(tmp)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    finally:
        os.remove(tmp)
    if not ok:
        raise BackupError("База в копии повреждена")
    if not {"users", "servers", "settings"} <= tables:
        raise BackupError("Это не копия базы Bobr Cloud")
    return blob


def stage(db: bytes, data: str) -> None:
    """Положить базу из копии рядом — бот подменит её при следующем запуске (до открытия базы)."""
    with open(os.path.join(data, "restore.db"), "wb") as f:
        f.write(db)


def apply_pending(db_file: str) -> str | None:
    """При запуске, до init_db: есть data/restore.db — текущая база в сторону, копия на её место. Возвращает заметку."""
    data = os.path.dirname(db_file) or "."
    pend = os.path.join(data, "restore.db")
    if not os.path.exists(pend):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    keep = f"{db_file}.before-restore-{stamp}"
    if os.path.exists(db_file):
        try:  # база в режиме WAL: последние изменения могут быть только в журнале — сливаем их в файл базы
            con = sqlite3.connect(db_file)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
        except sqlite3.Error:
            pass
        os.replace(db_file, keep)
    for ext in ("-wal", "-shm"):  # журнал не удаляем: если слить не вышло, он нужен прежней базе
        if os.path.exists(db_file + ext):
            os.replace(db_file + ext, keep + ext)
    os.replace(pend, db_file)
    note = f"База восстановлена из копии. Прежняя сохранена: {os.path.basename(db_file)}.before-restore-{stamp}"
    with open(os.path.join(data, "restored.txt"), "w", encoding="utf-8") as f:
        f.write(note)
    return note


def restored_note(data: str) -> str | None:
    p = os.path.join(data, "restored.txt")
    try:
        with open(p, encoding="utf-8") as f:
            t = f.read().strip()
        os.remove(p)
        return t or None
    except OSError:
        return None


async def run(ctx, reason: str = "по расписанию") -> dict:
    path = db_path()
    if not os.path.exists(path):
        raise BackupError("Файл базы не найден.")
    tmp = tempfile.mkdtemp(prefix="bcloud-")
    key = await get_key()
    try:
        gz = await asyncio.to_thread(_make, path, tmp, key)
        try:
            await asyncio.to_thread(keep_local, gz, os.path.dirname(path) or ".")
        except OSError:
            pass
        size = os.path.getsize(gz)
        if size > LIMIT:
            raise BackupError(f"Копия больше 50 МБ ({size // 1024 // 1024} МБ) — Telegram её не примет. Используйте bcloud backup на сервере.")
        caption = (f"💾 Резервная копия базы ({reason})\n{datetime.now(timezone.utc):%d.%m.%Y %H:%M} UTC · {max(1, size // 1024)} КБ\n\n"
                   + ("Копия полная и зашифрована вашим ключом. Восстановление: админка → Уведомления и бэкапы → "
                      "«Восстановить из копии», файл и ключ." if key else
                      "Восстановление: загрузите файл на сервер и выполните bcloud restore имя_файла. "
                      "Ключи ЮKassa и «Мой налог» в копию не входят — введите их заново в админке. "
                      "Задайте ключ копий в админке — копии станут полными и зашифрованными."))
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


async def get_key() -> str:
    async with session() as s:
        row = await s.get(Setting, KEY_SETTING)
    return row.value if row else ""


async def set_key(key: str) -> None:
    async with session() as s:
        row = await s.get(Setting, KEY_SETTING)
        if row is None:
            s.add(Setting(key=KEY_SETTING, value=key))
        else:
            row.value = key
        await s.commit()


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
