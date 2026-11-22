"""Authenticated, bounded archives of a stopped Chromium profile."""

import hashlib
import os
import tarfile
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"KSBR1"
BLOCK = 1024 * 1024
MAX_ARCHIVE = 1024**3
MAX_UNPACKED = 4 * 1024**3
MAX_ENTRIES = 250_000
CACHES = {"Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "ShaderCache", "GrShaderCache"}


def _entry(info):
    path = Path(info.name)
    if any(part in CACHES or part.startswith("Singleton") for part in path.parts):
        return None
    if path.name == ".keystone-writer" or not (info.isdir() or info.isfile()):
        return None
    return info


def _check_unpack_limits(entries, size):
    if entries > MAX_ENTRIES or size > MAX_UNPACKED:
        raise ValueError("Profile archive exceeds unpacking limits")


def export(profile, key, limit, target):
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    digest = hashlib.sha256()
    size = 0
    entries = unpacked = 0

    def include(info):
        nonlocal entries, unpacked
        info = _entry(info)
        if info is not None:
            entries += 1
            unpacked += info.size
            _check_unpack_limits(entries, unpacked)
        return info

    def append(data):
        nonlocal size
        size += len(data)
        if size > limit:
            raise ValueError("Archive exceeds limit")
        digest.update(data)
        target.write(data)

    class Encrypted:
        def write(self, data):
            append(encryptor.update(data))
            return len(data)

    append(MAGIC + nonce)
    with tarfile.open(fileobj=Encrypted(), mode="w|gz") as archive:
        archive.add(profile, arcname="profile", filter=include)
    append(encryptor.finalize() + encryptor.tag)
    target.seek(0)
    return size, digest.hexdigest()


def restore(root, key, source, length, marker):
    prefix = source.read(len(MAGIC) + 12)
    if len(prefix) != len(MAGIC) + 12 or not prefix.startswith(MAGIC):
        raise ValueError("Invalid archive format")
    remaining = length - len(prefix) - 16
    decryptor = Cipher(algorithms.AES(key), modes.GCM(prefix[len(MAGIC):])).decryptor()
    decryptor.authenticate_additional_data(MAGIC)
    with tempfile.TemporaryFile() as decrypted:
        while remaining > 0:
            chunk = source.read(min(BLOCK, remaining))
            if not chunk:
                raise ValueError("Truncated archive")
            decrypted.write(decryptor.update(chunk))
            remaining -= len(chunk)
        decrypted.write(decryptor.finalize_with_tag(source.read(16)))
        decrypted.seek(0)
        with tempfile.TemporaryDirectory(dir=root, prefix=".keystone-stage-") as directory:
            stage = Path(directory)
            total = 0
            with tarfile.open(fileobj=decrypted, mode="r|gz") as archive:
                for index, entry in enumerate(archive, start=1):
                    parts = Path(entry.name).parts
                    total += entry.size
                    _check_unpack_limits(index, total)
                    if not parts or parts[0] != "profile" or ".." in parts or _entry(entry) is None:
                        raise ValueError("Unsafe profile archive")
                    archive.extract(entry, stage, filter="data")
            profile = stage / "profile"
            if not (profile / marker).is_file():
                raise ValueError("Profile marker missing")
            profile.chmod(0o700)
            profile.rename(root / "restored")
