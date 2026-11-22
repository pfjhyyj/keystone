"""Model channel credentials on the shared credential vault, plus keyed scope identities."""

import hashlib
import hmac

from keystone.core.credentials import CredentialVault
from keystone.core.errors import ServiceError


def unavailable():
    return ServiceError(503, "model_credential_unavailable", "渠道凭据或加密密钥不可用")


class ChannelCredentials(CredentialVault):
    def __init__(self, root, *, stored=None):
        super().__init__(root, unavailable=unavailable, stored=stored)

    def scope(self, origin, secret):
        """Keyed opaque identity: duplicate Channels share a scope without exposing keys."""
        key = self.key()
        return hmac.new(key, (origin + "\0" + secret).encode(), hashlib.sha256).hexdigest()
