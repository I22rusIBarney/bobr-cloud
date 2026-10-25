import hashlib
import hmac
import json
import time
from datetime import timedelta
from urllib.parse import urlencode

from app.settings import DEFAULTS, coerce, fmt_rub, sell_price_kop
from app.web.auth import validate_init_data

CFG = {**DEFAULTS, "markup_pct": 40.0, "markup_fixed_rub": 0.0, "round_to_rub": 10}


def test_price_markup_and_rounding():
    # 158 * 1.4 = 221.2 -> округление вверх до 230
    assert sell_price_kop(158.0, CFG) == 230 * 100
    assert sell_price_kop(675.0, CFG) == 950 * 100  # 945 -> 950


def test_price_rounding_to_rub_and_fixed():
    cfg = {**CFG, "round_to_rub": 1, "markup_pct": 25, "markup_fixed_rub": 10}
    assert sell_price_kop(100.0, cfg) == 135 * 100


def test_price_never_below_cost_with_zero_markup():
    cfg = {**CFG, "markup_pct": 0, "round_to_rub": 1}
    assert sell_price_kop(158.0, cfg) == 158 * 100


def test_coerce():
    assert coerce("maintenance", "true") is True
    assert coerce("min_topup_rub", "150.0") == 150
    assert coerce("hidden_presets", ["1", 2]) == [1, 2]


def test_fmt_rub():
    assert fmt_rub(23000) == "230 ₽"
    assert fmt_rub(23050) == "230.50 ₽"


def _sign(token: str, fields: dict) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def test_init_data_valid_and_tampered():
    token = "123:ABC"
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42, "first_name": "A"}), "query_id": "q"}
    good = _sign(token, fields)
    assert validate_init_data(good, token)["id"] == 42
    assert validate_init_data(good.replace("42", "43"), token) is None
    assert validate_init_data(good, "other:token") is None
    assert validate_init_data("", token) is None


def test_init_data_expired():
    token = "123:ABC"
    fields = {"auth_date": str(int(time.time()) - 90000), "user": json.dumps({"id": 1})}
    assert validate_init_data(_sign(token, fields), token) is None


def test_refund_amount():
    from app.db import utcnow
    from app.services import refund_amount

    price = 100000  # 1000 ₽
    assert refund_amount(price, utcnow() + timedelta(days=15), 80) in range(39990, 40010)
    assert refund_amount(price, utcnow() + timedelta(days=90), 100) == price  # не больше цены
    assert refund_amount(price, utcnow() - timedelta(days=1), 80) == 0
    assert refund_amount(price, None, 80) == 0


def test_flags_and_os_labels():
    from app.services import clean_name, flag, os_label

    assert flag("nl") == "🇳🇱" and flag("xx1") == "🌍"
    assert clean_name("🇳🇱 Нидерланды") == "Нидерланды"
    assert os_label("ubuntu2404") == "Ubuntu 24.04"
    assert os_label("debian12") == "Debian 12"


PRESETS = [
    {"idx": 0, "cpu": 1, "ram": 1, "disk": 10, "price_month_rub": {"nl": 100}},
    {"idx": 1, "cpu": 2, "ram": 4, "disk": 60, "price_month_rub": {"nl": 400}},
    {"idx": 2, "cpu": 4, "ram": 8, "disk": 90, "price_month_rub": {"nl": 800}},
    {"idx": 3, "cpu": 4, "ram": 16, "disk": 160, "price_month_rub": {"nl": 1200, "de": 1300}},
]


def test_upgrade_options_only_grow():
    from app.services import upgrade_options

    o = upgrade_options(PRESETS, "nl", 2, 4)
    assert o == {"cpu": [2, 4], "ram": [4, 8, 16]}


def test_estimate_respects_current_disk():
    from app.services import estimate_month_cost

    # 4 CPU / 8 RAM при диске 60 → тариф 4/8/90
    assert estimate_month_cost(PRESETS, "nl", 4, 8, 60) == 800
    # при диске 120 подходит только 4/16/160
    assert estimate_month_cost(PRESETS, "nl", 4, 8, 120) == 1200
    assert estimate_month_cost(PRESETS, "nl", 8, 8, 10) is None


def test_prorate_and_settle():
    from app.services import prorate_kop, settle_from_charged

    assert prorate_kop(30000, 15) == 15000  # половина месяца от 300 ₽
    assert prorate_kop(30000, 0) == 100  # минимум 1 ₽
    assert prorate_kop(-500, 10) == 100
    assert settle_from_charged(200.0, 10) == 22000
    assert settle_from_charged(100.01, 0) == 10100  # вверх до рубля


def test_password_rules():
    import pytest

    from app.services import UserError, check_password

    assert check_password("Abc12345!") == "Abc12345!"
    for bad in ("short1", "с кириллицей123", "has space 123"):
        with pytest.raises(UserError):
            check_password(bad)


def test_admin_dependency_declared_before_use():
    """Регрессия: эндпоинты с Depends(admin) не должны стоять выше объявления admin (бот падал при старте)."""
    import ast
    from pathlib import Path

    src = (Path(__file__).parent.parent / "app" / "web" / "server.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "build_app")
    local = {n.name for n in fn.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    seen, bad = set(), []
    for st in fn.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for e in [*st.decorator_list, *st.args.defaults, *st.args.kw_defaults]:
                for nm in ast.walk(e) if e is not None else []:
                    if isinstance(nm, ast.Name) and nm.id in local and nm.id not in seen:
                        bad.append((st.name, nm.id))
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seen.add(st.name)
    assert not bad, bad
