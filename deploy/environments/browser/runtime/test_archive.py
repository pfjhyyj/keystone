from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from cryptography.exceptions import InvalidTag

from archive import export, restore


class ProfileArchiveTests(TestCase):
    def test_round_trip_excludes_transient_browser_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source", root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / ".keystone-profile").touch()
            (source / "Default").mkdir()
            (source / "Default" / "Login Data").write_bytes(b"synthetic saved entry")
            (source / "Default" / "Cache").mkdir()
            (source / "Default" / "Cache" / "volatile").touch()
            key = b"a" * 32
            archive = BytesIO()
            with patch("archive.MAX_ENTRIES", 4), patch("archive.MAX_UNPACKED", 21):
                size, _ = export(source, key, 4096, archive)
                restore(destination, key, archive, size, ".keystone-profile")
            profile = destination / "restored"
            self.assertEqual((profile / "Default" / "Login Data").read_bytes(), b"synthetic saved entry")
            self.assertFalse((profile / "Default" / "Cache").exists())

    def test_export_rejects_archives_that_would_exceed_restore_limits(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            (source / ".keystone-profile").touch()
            (source / "compressible-data").write_bytes(b"a" * 2048)
            for limit, value in (("MAX_UNPACKED", 1024), ("MAX_ENTRIES", 2)):
                with self.subTest(limit=limit), patch(f"archive.{limit}", value):
                    with self.assertRaisesRegex(ValueError, "exceeds unpacking limits"):
                        export(source, b"a" * 32, 4096, BytesIO())

    def test_wrong_key_or_tampering_never_publishes_profile(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source", root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / ".keystone-profile").touch()
            archive = BytesIO()
            key = b"a" * 32
            size, _ = export(source, key, 4096, archive)
            encrypted = archive.getvalue()
            for candidate, data in (
                (b"b" * 32, encrypted),
                (key, encrypted[:-1] + bytes([encrypted[-1] ^ 1])),
            ):
                with self.assertRaises(InvalidTag):
                    restore(destination, candidate, BytesIO(data), size, ".keystone-profile")
                self.assertEqual(list(destination.iterdir()), [])
