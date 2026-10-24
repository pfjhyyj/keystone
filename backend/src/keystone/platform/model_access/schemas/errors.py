"""Safe protocol failures, independent of the Keystone application boundary."""


class AdapterError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        self.status, self.code, self.detail = status, code, detail
        super().__init__(detail)


class QueryLater(Exception):
    def __init__(self, seconds=5, *, rate_limited=False):
        self.seconds = max(1, min(int(seconds or 5), 60))
        self.rate_limited, self.cooldown_seconds = rate_limited, seconds


class ResponseTooLarge(ValueError):
    pass
