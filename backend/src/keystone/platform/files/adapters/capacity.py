"""Single-host disk reservations, owned by OS locks rather than PIDs or timeouts.

All users share the coordination directory. Remaining bytes are charged per
filesystem; flushed, sequential writes consume their reservation. External
writers retain their full reservation until their workspace closes.
"""

import errno
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import portalocker

from keystone.core.errors import ServiceError


def full():
    return ServiceError(507, "file_capacity_full", "磁盘可用空间不足或已被预留，请清理空间后重试")


def invalid():
    return ServiceError(503, "file_capacity_unavailable", "磁盘容量预留状态暂不可用")


def io_error(error):
    if (
        error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}
        or getattr(error, "winerror", None) == 112
    ):
        return full()
    return invalid()


def clear_scratch(path, coordination_root):
    """Remove only a fixed, unowned transfer slot inside its verified namespace."""
    root = Path(coordination_root).resolve()
    parent = root / ".transfers"
    if (
        path.parent.resolve() != parent.resolve()
        or not parent.resolve().is_relative_to(root)
        or not re.fullmatch(r"(?:[0-9]|[12][0-9]|3[01])", path.name)
        or path.is_symlink()
        or path.is_junction()
        or not path.resolve().is_relative_to(parent.resolve())
    ):
        raise invalid()
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def scratch_size(path):
    if path.is_symlink() or path.is_junction():
        raise invalid()
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_symlink() or item.is_junction():
            raise invalid()
        try:
            if item.is_file():
                total += item.stat().st_size
        except FileNotFoundError:
            continue  # Native writers may replace intermediate files.
    return total


def filesystem(path):
    return str(Path(path).stat().st_dev)


class Capacity:
    def __init__(self, coordination, settings):
        self.coordination, self.settings = coordination, settings

    @contextmanager
    def locked(self):
        # This lock is permanent. Never replace/unlink its inode while in use.
        deadline = time.monotonic() + 5
        while True:
            lease = self.coordination.lease("disk-capacity")
            try:
                lease.__enter__()
                break
            except ServiceError as error:
                if error.code != "file_busy":
                    raise
                if time.monotonic() >= deadline:
                    raise ServiceError(
                        503, "file_capacity_busy", "磁盘容量检查繁忙，请重试"
                    ) from None
                time.sleep(0.01)
        try:
            try:
                yield
            except OSError as error:
                raise io_error(error) from None
        finally:
            lease.__exit__(None, None, None)

    def directory(self):
        self.coordination.fingerprint()
        path = self.coordination.root / ".capacity"
        if path.is_symlink() or not path.resolve().is_relative_to(self.coordination.root):
            raise invalid()
        path.mkdir(exist_ok=True)
        return path

    def read(self, path):
        try:
            with path.open("rb") as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError()
            record = json.loads(raw)
            amounts, floor, slot = record["amounts"], record["floor"], record["slot"]
            if (
                type(record["version"]) is not int
                or record["version"] != 3
                or not isinstance(amounts, dict)
                or not 1 <= len(amounts) <= 3
                or any(
                    not re.fullmatch(r"[0-9]+", device)
                    or type(size) is not int
                    or not 0 <= size <= 2 * 1024**4
                    for device, size in amounts.items()
                )
                or type(floor) is not int
                or not 0 <= floor <= 1024**4
                or (slot is not None and (type(slot) is not int or not 0 <= slot < 32))
            ):
                raise ValueError()
            return record
        except (OSError, ValueError, KeyError, TypeError):
            raise invalid() from None

    def live(self):
        """Called under the global lock; reclaim only owners whose OS lock is free."""
        directory = self.directory()
        self.reclaim_scratch()
        records = {}
        for path in sorted(directory.glob("*.lease")):
            identifier = path.stem
            if not re.fullmatch(r"[0-9a-f]{32}", identifier) or path.is_symlink():
                raise invalid()
            metadata = directory / f"{identifier}.json"
            with path.open("r+b") as handle:
                try:
                    portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                except portalocker.exceptions.LockException:
                    records[identifier] = self.read(metadata)
                    continue
                # Scratch reclamation uses its own slot lock, independent of this
                # metadata: interrupted cleanup or a lost record must not leak bytes.
                metadata.unlink(missing_ok=True)
                (directory / f"{identifier}.tmp").unlink(missing_ok=True)
            path.unlink()
        return records

    def reclaim_scratch(self):
        directory = self.coordination.root / ".transfers"
        if not directory.resolve().is_relative_to(self.coordination.root):
            raise invalid()
        if not directory.exists():
            return
        for path in directory.iterdir():
            # Slots 0..31 are the entire managed scratch namespace, including
            # leftovers from an earlier configuration with more transfer slots.
            if not re.fullmatch(r"(?:[0-9]|[12][0-9]|3[01])", path.name):
                continue
            slot = self.coordination.lease(f"transfer:{path.name}")
            try:
                slot.__enter__()
            except ServiceError as error:
                if error.code != "file_busy":
                    raise
                continue
            try:
                clear_scratch(path, self.coordination.root)
            finally:
                slot.__exit__(None, None, None)

    def check(self, roots, records, extra=None, *, floor_only=False):
        for root, device in roots.items():
            # Retain and revalidate every path, even when they initially share a
            # filesystem. Mount replacement must not spend another disk's claim.
            if filesystem(root) != device:
                raise invalid()
            floor = max(
                [self.settings.file_min_free_bytes]
                + [r["floor"] for r in records.values() if device in r["amounts"]]
            )
            remaining = (
                0
                if floor_only
                else sum(record["amounts"].get(device, 0) for record in records.values())
            )
            if shutil.disk_usage(root).free < floor + remaining + (extra or {}).get(device, 0):
                raise full()

    def reserve(self, targets, *, slot=None):
        """Reserve every future copy before receiving/writing its first byte."""
        roots, amounts = {}, {}
        # Even a direct write to another disk needs writable coordination space
        # for its ledger. Its metadata must respect that disk's floor as well.
        for path, size in [(self.coordination.root, 0), *targets]:
            if type(size) is not int or size < 0:
                raise ValueError("Invalid disk reservation size")
            try:
                path = Path(path).resolve()
                device = filesystem(path)
            except OSError as error:
                raise io_error(error) from None
            roots[path] = device
            amounts[device] = amounts.get(device, 0) + size
        with self.locked():
            records = self.live()
            self.check(roots, records, amounts)
            identifier = uuid4().hex
            directory = self.directory()
            owner = (directory / f"{identifier}.lease").open("x+b")
            reservation = Reservation(self, identifier, owner, roots, amounts, slot)
            try:
                portalocker.lock(owner, portalocker.LOCK_EX | portalocker.LOCK_NB)
                reservation.save()
            except BaseException:
                owner.close()
                reservation.remove()
                raise
            return reservation


class Reservation:
    def __init__(self, capacity, identifier, owner, roots, amounts, slot):
        self.capacity, self.identifier, self.owner = capacity, identifier, owner
        self.roots, self.amounts, self.slot = roots, amounts, slot
        self.closed = False

    def save(self):
        directory = self.capacity.directory()
        path = directory / f"{self.identifier}.tmp"
        record = {
            "version": 3,
            "amounts": self.amounts,
            "floor": self.capacity.settings.file_min_free_bytes,
            "slot": self.slot,
        }
        with path.open("wb") as stream:
            stream.write(json.dumps(record).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, directory / f"{self.identifier}.json")

    def remove(self):
        directory = self.capacity.directory()
        for suffix in ("tmp", "json", "lease"):
            (directory / f"{self.identifier}.{suffix}").unlink(missing_ok=True)

    def check(self, path, limit):
        with self.capacity.locked():
            if self.closed:
                raise invalid()
            if scratch_size(path) > limit:
                raise ServiceError(413, "file_temporary_limit", "文件超过临时空间配额")
            # External tools own their writes, so keep their full promise charged.
            self.capacity.check(self.roots, self.capacity.live(), floor_only=True)

    def write(self, stream, data):
        with self.capacity.locked():
            if self.closed:
                raise invalid()
            device = str(os.fstat(stream.fileno()).st_dev)
            if device not in self.amounts or len(data) > self.amounts[device]:
                raise ServiceError(413, "file_temporary_limit", "写入超过已预留容量")
            self.capacity.check(self.roots, self.capacity.live())
            try:
                written = stream.write(data)
                stream.flush()
            except OSError as error:
                raise io_error(error) from None
            if written != len(data):
                raise full()
            # Write+flush precedes reducing the promise; interruption is conservative.
            self.amounts[device] -= written
            self.save()

    def close(self):
        if self.closed:
            return
        with self.capacity.locked():
            self.owner.close()
            self.closed = True
            self.remove()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
