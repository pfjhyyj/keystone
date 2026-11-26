import os
import stat
import sys
import tomllib

import pytest

from keystone.core.config import Settings
from keystone.entrypoints import cli


@pytest.fixture
def command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KEYSTONE_CONFIG_FILE", raising=False)
    monkeypatch.delenv("KEYSTONE_CREDENTIAL_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["keystone", "init-credential-key"])
    return tmp_path / "keystone.toml"


@pytest.mark.parametrize("placeholder", [None, 'credential_key = "keystone" # placeholder\n'])
def test_init_credential_key_writes_only_the_key_and_is_idempotent(command, placeholder, capsys):
    original = (placeholder or "") + '# Preserve this comment\n[example]\nname = "unchanged"\n'
    command.write_text(original, encoding="utf-8")
    cli.main()
    after = command.read_text(encoding="utf-8")
    key = tomllib.loads(after)["credential_key"]
    assert Settings(config_file=None, credential_key=key).credential_key.get_secret_value() == key
    assert tomllib.loads(after) == tomllib.loads(original) | {"credential_key": key}
    assert '# Preserve this comment\n[example]\nname = "unchanged"\n' in after
    if os.name != "nt":
        assert stat.S_IMODE(command.stat().st_mode) == 0o600
    cli.main()
    assert command.read_text(encoding="utf-8") == after
    output = capsys.readouterr()
    assert key not in output.out + output.err


def test_command_does_not_write_example_or_override_environment(command, monkeypatch, caplog):
    example = command.with_suffix(".toml.example")
    example.write_text("# example\n", encoding="utf-8")
    monkeypatch.setenv("KEYSTONE_CONFIG_FILE", str(example))
    with pytest.raises(SystemExit, match="1"):
        cli.main()
    assert "private_config_required" in caplog.text
    assert example.read_text(encoding="utf-8") == "# example\n"

    monkeypatch.setenv("KEYSTONE_CONFIG_FILE", str(command))
    monkeypatch.setenv("KEYSTONE_CREDENTIAL_KEY", "test-secret-do-not-print")
    command.write_text("# private\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="1"):
        cli.main()
    assert "credential_key_overridden" in caplog.text
    assert "test-secret-do-not-print" not in caplog.text
    assert command.read_text(encoding="utf-8") == "# private\n"


def test_command_leaves_unknown_invalid_values_unchanged(command, caplog):
    original = 'credential_key = "test-invalid-secret"\n'
    command.write_text(original, encoding="utf-8")
    with pytest.raises(SystemExit, match="1"):
        cli.main()
    assert "invalid_credential_key" in caplog.text
    assert "test-invalid-secret" not in caplog.text
    assert command.read_text(encoding="utf-8") == original
