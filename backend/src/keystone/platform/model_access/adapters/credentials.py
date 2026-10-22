"""Write-only channel credentials, encrypted with a key in the shared coordination volume."""

import hashlib
import hmac
import json
import os
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from keystone.core.errors import ServiceError
from keystone.core.local_leases import LocalLeases

KEY_FILE = ".model-credentials.key"


def unavailable():
    return ServiceError(503, "model_credential_unavailable", "渠道凭据或加密密钥不可用")


class ChannelCredentials:
    def __init__(self, root):
        self.root = Path(root).resolve() if root else None
        self.leases = LocalLeases(root)

    def cipher(self, *, create=False):
        self.leases.fingerprint()
        path = self.root / KEY_FILE
        if not path.exists() and create:
            temporary = self.root / (KEY_FILE + ".tmp-" + uuid4().hex)
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(Fernet.generate_key())
                    stream.flush()
                    os.fsync(stream.fileno())
                # Publish a complete key without replacing a concurrent initializer's key.
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
            if os.name != "nt":
                descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        return Fernet(path.read_bytes())

    def encrypt(self, value, channel_id, purpose, *, create=False):
        try:
            payload = json.dumps(
                {"channel": str(channel_id), "purpose": purpose, "value": value.get_secret_value()}
            ).encode()
            return self.cipher(create=create).encrypt(payload).decode("ascii")
        except (OSError, ValueError, TypeError, ServiceError):
            raise unavailable() from None

    def decrypt(self, value, channel_id, purpose):
        if not value:
            raise unavailable()
        try:
            payload = json.loads(self.cipher().decrypt(value.encode("ascii")))
            if (
                payload["channel"] != str(channel_id)
                or payload["purpose"] != purpose
                or not isinstance(payload["value"], str)
                or not payload["value"]
            ):
                raise ValueError("Invalid credential binding")
            return payload["value"]
        except (OSError, ValueError, TypeError, KeyError, InvalidToken, ServiceError):
            raise unavailable() from None

    def scope(self, origin, secret):
        """Keyed opaque identity: duplicate Channels share a scope without exposing keys."""
        try:
            self.cipher()  # Verify the initialized volume and key before reading it.
            key = (self.root / KEY_FILE).read_bytes()
            return hmac.new(key, (origin + "\0" + secret).encode(), hashlib.sha256).hexdigest()
        except (OSError, ValueError, TypeError, ServiceError):
            raise unavailable() from None
