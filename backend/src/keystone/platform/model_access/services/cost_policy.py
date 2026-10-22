"""Cost status and waiting policy; no I/O, supplier branches or scheduler."""

import random
from datetime import timedelta

WAIT_SECONDS = (10, 30, 120, 600, 3600, 21600, 86400)
WINDOW = timedelta(hours=24)
MAX_ERRORS = 12


def stop_check(row, state, reason=None):
    row.cost_check_status, row.cost_check_reason = state, reason
    row.cost_next_check_at = None
    if row.cost_amount is None:
        row.cost_note = reason


def initialize_check(row, now):
    row.cost_deadline_at = now + WINDOW
    if row.cost_amount is not None:
        stop_check(row, "confirmed")
    elif not row.cost_context:
        stop_check(row, "unavailable", "cost_query_unavailable")
    elif not row.upstream_request_id:
        stop_check(row, "unavailable", "cost_request_id_missing")
    else:
        row.cost_check_status, row.cost_check_reason = "pending", "cost_awaiting_log"
        row.cost_next_check_at = now + timedelta(seconds=WAIT_SECONDS[0])
        row.cost_note = "cost_awaiting_log"


def expire_check(row, now):
    deadline = row.cost_deadline_at or (row.finished_at + WINDOW if row.finished_at else None)
    if deadline and now >= deadline:
        stop_check(row, "exhausted", "cost_window_exhausted")
        return True
    return False


def backoff(errors):
    return int(min(3600, 30 * 2 ** min(errors - 1, 7)) * random.uniform(0.8, 1.2))
