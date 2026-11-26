from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from keystone.core.credentials import CredentialVault
from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.credentials import ChannelCredentials


@pytest.fixture
def key():
    return SecretStr(Fernet.generate_key().decode("ascii"))


def test_ciphertext_is_bound_to_owner_and_purpose(key):
    owner = uuid4()
    vault = CredentialVault(key)
    sealed = vault.encrypt(SecretStr("app-secret"), owner, "messaging.app")
    assert "app-secret" not in sealed
    assert CredentialVault(key).decrypt(sealed, owner, "messaging.app") == "app-secret"
    for owner_id, purpose, token in (
        (uuid4(), "messaging.app", sealed),
        (owner, "messaging.webhook", sealed),
        (owner, "messaging.app", sealed[:-4] + "AAAA"),
    ):
        with pytest.raises(ServiceError) as error:
            vault.decrypt(token, owner_id, purpose)
        assert error.value.code == "credential_unavailable"


def test_model_scope_is_bound_to_configured_key(key):
    model = ChannelCredentials(key)
    another = ChannelCredentials(SecretStr(Fernet.generate_key().decode("ascii")))
    scope = model.scope("https://api.test", "sk")
    assert ChannelCredentials(key).scope("https://api.test", "sk") == scope
    assert another.scope("https://api.test", "sk") != scope
    assert key.get_secret_value() not in repr(model)

    with pytest.raises(ServiceError) as error:
        another.decrypt(model.encrypt(SecretStr("sk"), "c", "inference"), "c", "inference")
    assert error.value.code == "model_credential_unavailable"


def test_model_scope_requires_a_configured_key():
    with pytest.raises(ServiceError) as error:
        ChannelCredentials(None).scope("https://api.test", "sk")
    assert error.value.code == "model_credential_unavailable"
