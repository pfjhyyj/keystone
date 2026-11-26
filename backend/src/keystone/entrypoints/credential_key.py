"""Initialize the credential key in a private TOML without opening a database."""

import os
import re
import tomllib
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import SecretStr

from keystone.core.config import Settings
from keystone.core.errors import ServiceError

_PLACEHOLDER = re.compile(
    r"^([ \t]*credential_key[ \t]*=[ \t]*)([\"'])(?:keystone)?\2(?=[ \t]*(?:#|\r?$))",
    re.MULTILINE,
)


def initialize_credential_key() -> bool:
    if any(name.upper() == "KEYSTONE_CREDENTIAL_KEY" for name in os.environ):
        raise ServiceError(
            409, "credential_key_overridden", "请先取消 KEYSTONE_CREDENTIAL_KEY 覆盖"
        )
    path = Path(os.environ.get("KEYSTONE_CONFIG_FILE", "keystone.toml"))
    if path.name != "keystone.toml" or path.is_symlink() or not path.is_file():
        raise ServiceError(
            400, "private_config_required", "请先准备私有 keystone.toml，不能写入示例"
        )

    staging = path.with_name(".keystone-config.tmp")
    try:
        staging.mkdir(mode=0o700)
    except FileExistsError:
        raise ServiceError(
            409, "config_busy", "配置正在写入；若没有其他进程，请检查临时目录"
        ) from None
    temporary = staging / path.name
    try:
        original = path.read_bytes()
        try:
            text = original.decode("utf-8")
            before = tomllib.loads(text)
        except (UnicodeError, tomllib.TOMLDecodeError):
            raise ServiceError(400, "invalid_toml", "请先修正 keystone.toml 的语法") from None

        current = before.get("credential_key")
        if current not in (None, "", "keystone"):
            try:
                if not isinstance(current, str):
                    raise ValueError
                Settings.valid_credential_key(SecretStr(current))
            except ValueError:
                raise ServiceError(
                    400, "invalid_credential_key", "现有 credential_key 无效，未覆盖"
                ) from None
            return False

        key = Fernet.generate_key().decode("ascii")
        if current is None:
            newline = "\r\n" if "\r\n" in text else "\n"
            updated = f'credential_key = "{key}"{newline}{text}'
        else:
            updated = _PLACEHOLDER.sub(lambda match: f'{match[1]}"{key}"', text, count=1)
        if tomllib.loads(updated) != before | {"credential_key": key}:
            raise ServiceError(400, "invalid_credential_key", "请先移除非标准写法的 credential_key")

        with open(
            temporary, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)
        ) as stream:
            stream.write(updated.encode("utf-8"))
        if path.read_bytes() != original:
            raise ServiceError(409, "config_changed", "配置已被修改，请重试")
        temporary.replace(path)
        return True
    finally:
        temporary.unlink(missing_ok=True)
        staging.rmdir()
