"""Reuse Ether's Argon2 verification and SHA-256 digest; no JWT/refresh tokens."""

import hashlib
import hmac
import secrets
import time

import anyio
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from keystone.core.errors import ServiceError

hasher = PasswordHasher()
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(token: str) -> str:
    return hmac.new(token.encode(), b"keystone.csrf.v1", hashlib.sha256).hexdigest()


def verify_password(encoded, password):
    try:
        return hasher.verify(encoded or DUMMY_HASH, password) and encoded is not None
    except (VerificationError, InvalidHashError):
        return False


class Passwords:
    def __init__(self):
        self.limiter = anyio.CapacityLimiter(4)

    async def hash(self, value):
        return await anyio.to_thread.run_sync(hasher.hash, value, limiter=self.limiter)

    async def verify(self, encoded, value):
        return await anyio.to_thread.run_sync(verify_password, encoded, value, limiter=self.limiter)


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
