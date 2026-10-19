"""Process-local readiness marker, adapted from Ether's Worker ready file."""

import logging
import time

MAX_AGE_SECONDS = 30


class WorkerHealth:
    def __init__(self, path):
        self.path = path

    def clear(self):
        if self.path is not None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).error("worker_ready_file_cleanup_failed")

    def refresh(self):
        if self.path is not None:
            self.path.touch()

    def healthy(self):
        if self.path is None:
            return False
        try:
            age = time.time() - self.path.stat().st_mtime
            return self.path.is_file() and 0 <= age <= MAX_AGE_SECONDS
        except OSError:
            return False
