"""Login attempt policy for the API process; no token or password implementation."""

import time

from keystone.core.errors import ServiceError
from keystone.platform.identity.adapters.security import digest


class LoginLimiter:
    """Single API process: fixed 60-second windows, bounded keys, no trusted forwarded IP."""

    def __init__(self):
        self.buckets = {}

    def check(self, username):
        now = time.monotonic()
        self.buckets = {k: v for k, v in self.buckets.items() if v[0] > now}
        keys = [("global", 60), (f"user:{digest(username)}", 5)]
        if len(self.buckets) + sum(k not in self.buckets for k, _ in keys) > 4096:
            raise ServiceError(429, "login_limited", "登录尝试过多，请稍后再试")
        if any(self.buckets.get(k, (0, 0))[1] >= limit for k, limit in keys):
            raise ServiceError(429, "login_limited", "登录尝试过多，请稍后再试")
        for key, _ in keys:
            expiry, count = self.buckets.get(key, (now + 60, 0))
            self.buckets[key] = expiry, count + 1
