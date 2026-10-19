import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import SecretStr

from keystone.core.errors import ServiceError
from keystone.platform.files.leases import initialize
from keystone.platform.model_access.credentials import KEY_FILE, ChannelCredentials


def test_credentials_are_bound_to_channel_and_purpose_and_shared_between_processes(tmp_path):
    initialize(tmp_path)
    identifier = uuid4()
    secret = SecretStr("channel-api-key-never-store-in-cleartext")
    store = ChannelCredentials(tmp_path)
    encrypted = store.encrypt(secret, identifier, "inference", create=True)
    assert secret.get_secret_value() not in encrypted
    assert (
        ChannelCredentials(tmp_path).decrypt(encrypted, identifier, "inference")
        == secret.get_secret_value()
    )
    for channel, purpose, value in (
        (uuid4(), "inference", encrypted),
        (identifier, "billing", encrypted),
        (identifier, "inference", encrypted[:-4] + "AAAA"),
    ):
        with pytest.raises(ServiceError) as error:
            store.decrypt(value, channel, purpose)
        assert error.value.code == "model_credential_unavailable"
    if os.name != "nt":
        assert (tmp_path / KEY_FILE).stat().st_mode & 0o777 == 0o600
    (tmp_path / KEY_FILE).unlink()
    with pytest.raises(ServiceError):
        store.decrypt(encrypted, identifier, "inference")
    with pytest.raises(ServiceError):
        store.encrypt(secret, identifier, "inference")
    assert not (tmp_path / KEY_FILE).exists()


def test_concurrent_first_saves_publish_one_complete_key(tmp_path):
    initialize(tmp_path)

    def save(index):
        identifier = uuid4()
        store = ChannelCredentials(tmp_path)
        value = store.encrypt(SecretStr(f"test-key-{index}"), identifier, "inference", create=True)
        return identifier, value, f"test-key-{index}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        saved = list(pool.map(save, range(16)))
    for identifier, encrypted, secret in saved:
        assert ChannelCredentials(tmp_path).decrypt(encrypted, identifier, "inference") == secret
    assert not list(tmp_path.glob(KEY_FILE + ".tmp-*"))
