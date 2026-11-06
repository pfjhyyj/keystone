import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.postgres


def test_real_dbos_retries_replay_and_cancel(empty_database_url):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / "support/native_retries.py")],
        env={**os.environ, "KEYSTONE_NATIVE_RETRY_TEST_URL": empty_database_url},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "native-retry-checkpoint-and-cancel-passed" in result.stdout
