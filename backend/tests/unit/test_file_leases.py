"""Native file leases require no database."""

from keystone.platform.files.adapters.storage import initialize


def test_local_lease_is_cross_process(tmp_path):
    import subprocess
    import sys

    from keystone.core.local_leases import LocalLeases

    initialize(tmp_path / "store")
    storage = LocalLeases(tmp_path / "store")
    with storage.lease("one"):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from keystone.core.local_leases import LocalLeases; import sys; "
                'LocalLeases(sys.argv[1]).lease("one").__enter__()',
                str(storage.root),
            ],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode != 0 and b"file" in result.stderr.lower()
    with storage.lease("one"):
        pass
