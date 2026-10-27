class TelegramBadRequest(Exception): pass
class TelegramNetworkError(Exception): pass
class TelegramServerError(Exception): pass
class TelegramRetryAfter(Exception):
    def __init__(self, retry_after=1, *a):
        super().__init__(f"retry after {retry_after}"); self.retry_after = retry_after
