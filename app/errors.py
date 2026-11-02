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
