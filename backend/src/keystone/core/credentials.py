"""Write-only stored credentials, encrypted with a key in the shared coordination volume.

Each ciphertext is bound to an owner ID and a namespaced purpose, so a value copied to another
record or reused for another purpose fails to decrypt. The payload layout and key file name are
stable: existing model channel credentials keep decrypting.
"""

import json
import os
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from keystone.core.errors import ServiceError
from keystone.core.local_leases import LocalLeases

KEY_FILE = ".model-credentials.key"


def credential_unavailable():
    return ServiceError(503, "credential_unavailable", "凭据或加密密钥不可用")


class CredentialVault:
    def __init__(self, root, *, unavailable=credential_unavailable):
        self.root = Path(root).resolve() if root else None
        self.leases = LocalLeases(root)
        self.unavailable = unavailable

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

    def key(self):
        """Raw key bytes for keyed digests; verifies the initialized volume first."""
        try:
            self.cipher()
            return (self.root / KEY_FILE).read_bytes()
        except (OSError, ValueError, TypeError, ServiceError):
            raise self.unavailable() from None

    def encrypt(self, value, owner_id, purpose, *, create=False):
        try:
            payload = json.dumps(
                # "channel" is the historical name of the owner binding.
                {"channel": str(owner_id), "purpose": purpose, "value": value.get_secret_value()}
            ).encode()
            return self.cipher(create=create).encrypt(payload).decode("ascii")
        except (OSError, ValueError, TypeError, ServiceError):
            raise self.unavailable() from None

    def decrypt(self, value, owner_id, purpose):
        if not value:
            raise self.unavailable()
        try:
            payload = json.loads(self.cipher().decrypt(value.encode("ascii")))
            if (
                payload["channel"] != str(owner_id)
                or payload["purpose"] != purpose
                or not isinstance(payload["value"], str)
                or not payload["value"]
            ):
                raise ValueError("Invalid credential binding")
            return payload["value"]
        except (OSError, ValueError, TypeError, KeyError, InvalidToken, ServiceError):
            raise self.unavailable() from None
