"""Explicit safe snapshots and caller-owned transactions, adapted from Ether audit."""

import json
import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func

from keystone.platform.audit.models import AuditLog

# Each action has a deliberately small, explicit allowlist; no model/request serialization.


@dataclass(frozen=True)
class Actor:
    kind: str = "anonymous"
    id: UUID | None = None


def validate_details(value, allowed):
    if isinstance(value, dict):
        if value.keys() - allowed:
            raise ValueError("Unsupported audit detail field")
        for child in value.values():
            validate_details(child, allowed)
    elif isinstance(value, list):
        for child in value:
            validate_details(child, allowed)


async def record(
    session,
    action,
    actor,
    target_type=None,
    target_id=None,
    *,
    outcome="success",
    correlation_id=None,
    details=None,
):
    if actor.kind not in {"user", "system", "anonymous"} or (
        (actor.kind == "user") != (actor.id is not None)
    ):
        raise ValueError("Invalid audit actor")
    allowed = session.info["catalog"].audit_actions[action] | {"reason", "credential_id"}
    validate_details(details, allowed)
    if len(json.dumps(details, ensure_ascii=False).encode()) > 8192:
        raise ValueError("Audit details too large")
    session.add(
        AuditLog(
            occurred_at=func.clock_timestamp(),
            actor_type=actor.kind,
            actor_id=actor.id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            outcome=outcome,
            correlation_id=correlation_id,
            details=details,
        )
    )
    await session.flush()


async def failed(
    database,
    action,
    actor,
    target_type,
    target_id,
    correlation_id,
    code,
    denied,
    *,
    credential_id=None,
):
    details = {"reason": code}
    if credential_id is not None:
        details["credential_id"] = str(credential_id)
    try:
        async with database.transaction() as session:
            await record(
                session,
                action,
                actor,
                target_type,
                target_id,
                outcome="denied" if denied else "failure",
                correlation_id=correlation_id,
                details=details,
            )
    except Exception:
        logging.getLogger(__name__).error("failure_audit_unavailable action=%s", action)
