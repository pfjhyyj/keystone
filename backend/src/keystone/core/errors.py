class ServiceError(Exception):
    """Only explicitly safe messages belong in this application boundary."""

    def __init__(self, status: int, code: str, detail: str):
        self.status, self.code, self.detail = status, code, detail
        super().__init__(detail)

    def __reduce__(self):
        # DBOS can persist safe step failures and reconstruct them on recovery.
        return type(self), (self.status, self.code, self.detail)
