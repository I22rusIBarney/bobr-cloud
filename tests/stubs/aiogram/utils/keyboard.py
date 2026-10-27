from ..types import InlineKeyboardButton, InlineKeyboardMarkup
class InlineKeyboardBuilder:
    def __init__(s): s.rows = []; s.loose = []
    @classmethod
    def from_markup(cls, m): b = cls(); b.rows = [list(r) for r in m.inline_keyboard]; return b
    def row(s, *btns): s.rows.append(list(btns)); return s
    def button(s, **k): s.loose.append(InlineKeyboardButton(**k)); return s
    def adjust(s, n):
        for i in range(0, len(s.loose), n): s.rows.append(s.loose[i:i+n])
        s.loose = []; return s
    def as_markup(s): return InlineKeyboardMarkup(inline_keyboard=s.rows + ([s.loose] if s.loose else []))
