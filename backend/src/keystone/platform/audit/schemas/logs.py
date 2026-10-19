"""Explicit public audit representation; never expose arbitrary row attributes."""


def view(row):
    fields = (
        "id",
        "occurred_at",
        "actor_type",
        "actor_id",
        "action",
        "target_type",
        "target_id",
        "outcome",
        "correlation_id",
        "details",
    )
    return {key: getattr(row, key) for key in fields}
