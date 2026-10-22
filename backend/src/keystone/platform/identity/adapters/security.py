"""Reuse Ether's Argon2 verification and SHA-256 digest; no JWT/refresh tokens."""

import hashlib
import hmac
import secrets

import anyio
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

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
