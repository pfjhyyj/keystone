import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx2
import pytest
from openai import APIStatusError
from pydantic import SecretStr
from pydantic_ai.exceptions import ModelHTTPError

from keystone.core.errors import ServiceError
from keystone.platform.files.adapters.storage import initialize
from keystone.platform.model_access.adapters.billing.openrouter import OpenRouter
from keystone.platform.model_access.adapters.credentials import KEY_FILE, ChannelCredentials
from keystone.platform.model_access.adapters.inference.client import (
    OPENROUTER_BASE,
    InferenceClient,
)
from keystone.platform.model_access.adapters.inference.errors import classify_error
from keystone.platform.model_access.schemas.errors import AdapterError


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


@pytest.mark.parametrize(
    "status,code,state",
    [
        (400, "model_invalid_request", "failed"),
        (401, "model_authentication_failed", "failed"),
        (402, "model_insufficient_credits", "failed"),
        (403, "model_access_denied", "failed"),
        (404, "upstream_model_not_found", "failed"),
        (422, "model_invalid_request", "failed"),
        (429, "model_rate_limited", "failed"),
        (500, "model_upstream_error", "unknown"),
    ],
)
def test_direct_and_agent_http_errors_have_the_same_safe_classification(status, code, state):
    raw = APIStatusError(
        "sensitive",
        response=httpx2.Response(
            status, request=httpx2.Request("POST", "https://example.invalid/chat/completions")
        ),
        body={"message": "sensitive"},
    )
    wrapped = ModelHTTPError(status_code=status, model_name="test", body="sensitive")
    wrapped.__cause__ = raw
    for error in (raw, wrapped, ModelHTTPError(status_code=status, model_name="test")):
        result = classify_error(error)
        assert result[0] == 502 and result[1] == code and result[3] == state
        assert "sensitive" not in result[2]


def test_inference_rejects_untrusted_targets_and_unsupported_providers():
    client = InferenceClient(None)
    client.check_target("openrouter", OPENROUTER_BASE, "/chat/completions")
    for base, path, method in (
        ("https://mock.invalid/v1", "/chat/completions", "POST"),
        ("https://127.0.0.1/api/v1", "/chat/completions", "POST"),
        ("https://openrouter.ai.evil.invalid/api/v1", "/chat/completions", "POST"),
        ("https://openrouter.ai/api", "/chat/completions", "POST"),
        ("https://openrouter.ai:444/api/v1", "/chat/completions", "POST"),
        ("https://openrouter.ai/api/v1/other", "/chat/completions", "POST"),
        (OPENROUTER_BASE, "/credits", "POST"),
        (OPENROUTER_BASE, "/chat/completions", "GET"),
    ):
        with pytest.raises(ServiceError) as caught:
            client.check_target("openrouter", base, path, method=method)
        assert (caught.value.status, caught.value.code) == (409, "model_target_not_allowed")
    for provider in ("deepseek", "aliyun_bailian"):
        with pytest.raises(ServiceError) as caught:
            client.check_target(provider, OPENROUTER_BASE, "/chat/completions")
        assert (caught.value.status, caught.value.code) == (503, "model_provider_unavailable")


def test_billing_rejects_untrusted_bases_and_non_billing_paths():
    adapter = OpenRouter()
    for path in ("/credits", "/generation"):
        adapter.check_target(OPENROUTER_BASE, path)
    for base, path in (
        ("https://mock.invalid/v1", "/credits"),
        (OPENROUTER_BASE, "/keys"),
        (OPENROUTER_BASE, "/chat/completions"),
    ):
        with pytest.raises(AdapterError) as caught:
            adapter.check_target(base, path)
        assert (caught.value.status, caught.value.code) == (409, "model_target_not_allowed")


def test_wrapped_timeout_cancellation_and_safe_errors_remain_unknown():
    for cause, code in (
        (httpx2.ReadTimeout("sensitive"), "model_timeout"),
        (asyncio.CancelledError(), "model_interrupted"),
        (ServiceError(503, "model_result_unavailable", "safe"), "model_result_unavailable"),
        (AdapterError(413, "model_input_too_large", "safe"), "model_input_too_large"),
    ):
        wrapper = RuntimeError("sensitive")
        wrapper.__cause__ = cause
        result = classify_error(wrapper)
        assert result[1] == code and result[3] == "unknown" and "sensitive" not in result[2]
    # A malformed exception chain must not prevent recording the attempt's outcome.
    wrapper = RuntimeError("sensitive")
    wrapper.__cause__ = wrapper
    assert classify_error(wrapper)[1] == "model_response_unknown"
