"""Сценарные проверки: каждый скрипт из tests/scenarios поднимает приложение на чистой SQLite
с поддельной Astra/ЮKassa и печатает PASS/FAIL по шагам, в конце — «FAILS: N».

Скрипты запускаются отдельными процессами: у каждого свои переменные окружения и своя база.
Для проверок бота используется заглушка aiogram из tests/stubs — тексты и кнопки рендерятся без Telegram.
Один сценарий вручную: python tests/scenarios/share_test.py /tmp/x
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
ROOT = HERE.parent
SCRIPTS = sorted(p.name for p in (HERE / "scenarios").glob("*.py"))


@pytest.mark.parametrize("name", SCRIPTS)
def test_scenario(name, tmp_path):
    env = {**os.environ, "REPO_ROOT": str(ROOT),
           "PYTHONPATH": os.pathsep.join(x for x in (str(HERE / "stubs"), str(ROOT), os.environ.get("PYTHONPATH", "")) if x)}
    r = subprocess.run([sys.executable, str(HERE / "scenarios" / name), str(tmp_path)],
                       capture_output=True, text=True, timeout=600, env=env, cwd=tmp_path)
    out = r.stdout + r.stderr
    fails = [line for line in out.splitlines() if line.startswith("FAIL ")]
    assert r.returncode == 0, out[-6000:]
    assert not fails, "\n".join(fails)
    assert "FAILS: 0" in out or "ПРОБЛЕМЫ: нет" in out, out[-6000:]
