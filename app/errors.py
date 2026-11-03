"""Где упало: файл, строка и функция — самое глубокое место в нашем коде (не в библиотеках)."""
from __future__ import annotations

import os
import traceback


def where(ex: BaseException) -> str:
    frames = traceback.extract_tb(ex.__traceback__)
    if not frames:
        return "?"
    lib = ("site-packages", "dist-packages", f"{os.sep}lib{os.sep}python")
    mine = [f for f in frames if not any(x in f.filename for x in lib)] or frames
    f = mine[-1]
    return f"{os.path.basename(f.filename)}:{f.lineno}, {f.name}"


def describe(ex: BaseException) -> str:
    return f"{type(ex).__name__}: {str(ex)[:200]} ({where(ex)})"


# ───────────── понятные объяснения для администратора ─────────────

TASKS = {
    "sync_servers": "Синхронизация серверов с Astra",
    "refund_undelivered": "Возврат денег за невыданные серверы",
    "relabel_astra": "Подписи серверов в Astra",
    "poll_payments": "Проверка оплат",
    "autorenew": "Автопродление и напоминания",
    "receipts": "Отправка чеков в «Мой налог»",
    "astra_balance": "Проверка баланса Astra",
    "locations": "Проверка локаций Astra",
    "broadcasts": "Рассылки",
    "backup": "Резервные копии",
    "health": "Проверка здоровья бота",
    "security": "Проверка безопасности",
    "monitor": "Мониторинг серверов",
    "margin": "Проверка наценки",
    "retention": "Возврат клиентов",
}

_LATER = "Бот сам повторит попытку, делать ничего не нужно."


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def explain(ex: BaseException) -> tuple[str, str, bool]:
    """(что случилось, что делать, наша ли это ошибка) — простыми словами."""
    import httpx

    from .astra import AstraError, AstraUncertain
    from .backup import BackupError
    from .nalog import NalogError
    from .platega import PlategaError
    from .yookassa import NotConfigured, YooKassaError

    if isinstance(ex, AstraError):
        st = ex.status
        if st in (502, 503, 504, 500):
            return (f"Сервер Astra сейчас не работает (ошибка {st}). Это сбой у Astra, не у вас.",
                    f"{_LATER} Если не проходит больше часа — напишите в поддержку Astra.", False)
        if st == 429:
            return ("Astra просит делать запросы реже (слишком много запросов).", _LATER, False)
        if st in (401, 403):
            return ("Astra не принимает ключ API реселлера.",
                    "Проверьте ключ Astra в настройках бота — возможно, его сменили или отозвали.", False)
        if st == 402 or "balance" in ex.code:
            return ("На балансе Astra не хватает денег.", "Пополните баланс Astra через @astravpsbot.", False)
        return (f"Astra отказала в запросе (ошибка {st}).",
                "Если повторяется — перешлите это сообщение разработчику или в поддержку Astra.", False)
    if isinstance(ex, AstraUncertain) or isinstance(ex, httpx.TimeoutException):
        return ("Astra не ответила вовремя — связь оборвалась или их сервер завис.", _LATER, False)
    if isinstance(ex, httpx.TransportError):
        return ("Нет связи с внешним сервисом (Astra, ЮKassa или другим) — проблемы с сетью.",
                f"{_LATER} Если долго не проходит — проверьте, есть ли интернет на сервере бота.", False)
    if isinstance(ex, NotConfigured):
        return ("Платёжная система не настроена.", "Заполните ключи ЮKassa в админке.", False)
    if isinstance(ex, YooKassaError):
        return (f"ЮKassa вернула ошибку {ex.status}" + (f": {ex.description}" if ex.description else "") + ".",
                "Проверьте ключи ЮKassa в админке; если всё верно — напишите в поддержку ЮKassa.", False)
    if isinstance(ex, PlategaError):
        return (f"Platega вернула ошибку {ex.status}: {ex.description}.",
                "Проверьте настройки Platega в админке; если всё верно — напишите в их поддержку.", False)
    if isinstance(ex, NalogError):
        return (f"«Мой налог» не принял запрос: {ex}.",
                "Чаще всего это сбой на стороне налоговой — бот повторит. Если не проходит сутки, проверьте вход в «Мой налог» в админке.", False)
    if isinstance(ex, BackupError):
        return (f"Не получилось сделать резервную копию: {ex}.", "Проверьте настройки резервных копий в админке.", False)
    if isinstance(ex, OSError) and getattr(ex, "errno", None) == 28:
        return ("На сервере бота закончилось место на диске.", "Освободите место на сервере — иначе бот может перестать работать.", False)
    if type(ex).__module__.startswith("sqlalchemy"):
        return ("Ошибка базы данных бота.", "Перешлите это сообщение разработчику.", True)
    return ("Внутренняя ошибка в коде бота.", "Перешлите это сообщение разработчику — по техническим деталям ниже он найдёт причину.", True)


def human(task: str, ex: BaseException, *, minutes: int | None = None) -> str:
    """Готовое сообщение администратору: что, почему, что делать + мелко тех. детали."""
    what, todo, ours = explain(ex)
    title = TASKS.get(task, task)
    if minutes:
        what += f" Длится уже {minutes} мин."
    icon = "🐞" if ours else "⚠️"
    return (f"{icon} <b>{_esc(title)} — не получилось</b>\n\n"
            f"<b>Что случилось:</b> {_esc(what)}\n"
            f"<b>Что делать:</b> {_esc(todo)}\n\n"
            f"<i>Для разработчика: {_esc(describe(ex))}</i>")
