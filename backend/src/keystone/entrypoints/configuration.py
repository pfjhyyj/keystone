"""Convert startup settings errors to messages that never contain the input."""

from collections.abc import Callable

from pydantic import ValidationError

from keystone.core.config import CredentialKeyRequired, Settings
from keystone.core.credentials import MISSING_KEY_HINT
from keystone.core.errors import ServiceError


def load_settings(factory: Callable[[], Settings]) -> Settings:
    try:
        return factory()
    except Exception as error:
        if isinstance(error, ValidationError):
            for issue in error.errors(include_input=False, include_url=False):
                if issue["loc"] == ("credential_key",):
                    raise ServiceError(
                        400,
                        "invalid_credential_key",
                        "credential_key 必须是有效的 Fernet 密钥。请检查私有 keystone.toml 或 "
                        "KEYSTONE_CREDENTIAL_KEY；如使用示例占位值，请运行 "
                        "uv run --locked keystone init-credential-key。",
                    ) from error
                if isinstance(issue.get("ctx", {}).get("error"), CredentialKeyRequired):
                    raise ServiceError(400, "credential_key_required", MISSING_KEY_HINT) from error
        raise ServiceError(
            400, "invalid_configuration", "请检查 keystone.toml 和环境变量"
        ) from error
