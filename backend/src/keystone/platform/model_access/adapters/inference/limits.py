"""Normalize bounded HTTP Retry-After data without exposing the original header."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import ceil


def retry_after(value, *, now=None):
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return max(1, min(int(value), 3600))
    try:
        due = parsedate_to_datetime(value)
        if due.tzinfo is None:
            return None
        return max(1, min(ceil((due - (now or datetime.now(UTC))).total_seconds()), 3600))
    except (TypeError, ValueError, OverflowError):
        return None
