"""Model channel credentials on the shared credential vault, plus keyed scope identities."""

from keystone.core.credentials import CredentialVault
from keystone.core.errors import ServiceError


def unavailable():
    return ServiceError(503, "model_credential_unavailable", "渠道凭据或加密密钥不可用")


class ChannelCredentials(CredentialVault):
    def __init__(self, key):
        super().__init__(key, unavailable=unavailable)

    def scope(self, origin, secret):
        """Keyed opaque identity: duplicate Channels share a scope without exposing keys."""
        return self._keyed_digest((origin + "\0" + secret).encode())
