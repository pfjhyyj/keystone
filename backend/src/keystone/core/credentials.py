"""Encrypt write-only credentials with the configured key, bound to an owner and purpose."""

import hmac
import json

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from keystone.core.errors import ServiceError

MISSING_KEY_HINT = (
    "未配置 credential_key。新部署请运行 uv run --locked keystone init-credential-key 并重启；"
    "已有加密凭据请恢复原密钥。"
)


def credential_unavailable():
    return ServiceError(503, "credential_unavailable", "凭据或加密密钥不可用")


class CredentialVault:
    def __init__(self, key: SecretStr | None, *, unavailable=credential_unavailable):
        self._key = key
        self.unavailable = unavailable

    def _key_bytes(self) -> bytes:
        if self._key is None:
            error = self.unavailable()
            raise ServiceError(error.status, error.code, MISSING_KEY_HINT)
        return self._key.get_secret_value().encode("ascii")

    def _keyed_digest(self, message):
        try:
            key = self._key_bytes()
            Fernet(key)
            return hmac.digest(key, message, "sha256").hex()
        except (ValueError, TypeError):
            raise self.unavailable() from None

    def encrypt(self, value, owner_id, purpose):
        try:
            payload = json.dumps(
                {"channel": str(owner_id), "purpose": purpose, "value": value.get_secret_value()}
            ).encode()
            return Fernet(self._key_bytes()).encrypt(payload).decode("ascii")
        except (ValueError, TypeError):
            raise self.unavailable() from None

    def decrypt(self, value, owner_id, purpose):
        if not value:
            raise self.unavailable()
        try:
            payload = json.loads(Fernet(self._key_bytes()).decrypt(value.encode("ascii")))
            if (
                payload["channel"] != str(owner_id)
                or payload["purpose"] != purpose
                or not isinstance(payload["value"], str)
                or not payload["value"]
            ):
                raise ValueError("Invalid credential binding")
            return payload["value"]
        except (ValueError, TypeError, KeyError, InvalidToken):
            raise self.unavailable() from None
