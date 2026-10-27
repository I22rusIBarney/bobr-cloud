class _O:
    def __init__(s, **k): s.__dict__.update(k)
class InlineKeyboardButton(_O): pass
class InlineKeyboardMarkup(_O): pass
class WebAppInfo(_O): pass
class TelegramObject: pass
class Message(TelegramObject): pass
class CallbackQuery(TelegramObject): pass

class FSInputFile:
    def __init__(self, path, *a, **k): self.path = path
