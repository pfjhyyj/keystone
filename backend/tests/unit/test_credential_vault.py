import pytest
from pydantic import SecretStr

from keystone.core.credentials import CredentialVault
from keystone.core.errors import ServiceError
from keystone.platform.files.adapters.storage import initialize
from keystone.platform.model_access.adapters.credentials import ChannelCredentials


def test_ciphertext_is_bound_to_owner_and_purpose(tmp_path):
    initialize(tmp_path)
    vault = CredentialVault(tmp_path)
    sealed = vault.encrypt(SecretStr("app-secret"), "owner-1", "messaging.app", create=True)
    assert "app-secret" not in sealed
    assert vault.decrypt(sealed, "owner-1", "messaging.app") == "app-secret"
    for owner, purpose in (("owner-2", "messaging.app"), ("owner-1", "messaging.webhook")):
        with pytest.raises(ServiceError) as error:
            vault.decrypt(sealed, owner, purpose)
        assert error.value.code == "credential_unavailable"


def test_model_channels_share_the_key_and_keep_their_error(tmp_path):
    initialize(tmp_path)
    sealed = CredentialVault(tmp_path).encrypt(SecretStr("sk"), "c", "inference", create=True)
    channels = ChannelCredentials(tmp_path)
    assert channels.decrypt(sealed, "c", "inference") == "sk"
    assert channels.scope("https://api.test", "sk") == channels.scope("https://api.test", "sk")
    with pytest.raises(ServiceError) as error:
        ChannelCredentials(tmp_path / "uninitialized").scope("https://api.test", "sk")
    assert error.value.code == "model_credential_unavailable"
