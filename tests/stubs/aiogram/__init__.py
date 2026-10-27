class _F:
    def __getattr__(self, n): return _F()
    def __call__(self, *a, **k): return _F()
    def __eq__(self, o): return _F()
    def __invert__(self): return _F()
    def __or__(self, o): return _F()
    __hash__ = object.__hash__
F = _F()
class BaseMiddleware: pass
class Bot: pass
class _Obs:
    def __init__(s): s.handlers = []
    def __call__(s, *flt, **kw):
        def deco(fn): s.handlers.append(fn); return fn
        return deco
    def middleware(s, m): pass
class Router:
    def __init__(s): s.message = _Obs(); s.callback_query = _Obs()
