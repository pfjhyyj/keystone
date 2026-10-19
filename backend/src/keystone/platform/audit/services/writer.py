"""Explicit safe snapshots and caller-owned transactions, adapted from Ether audit."""

import json
import logging
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import func

from keystone.platform.audit.models import AuditLog

# Each action has a deliberately small, explicit allowlist; no model/request serialization.
ACTIONS = {
    "flow.start": {"definition_key"},
    "flow.cancel": set(),
    "flow.complete": {"status", "error_code"},
    "identity.login": {"reason"},
    "identity.logout": {"user_id"},
    "identity.password.change": {"sessions_revoked"},
    "identity.user.create": {"username", "display_name"},
    "identity.user.update": {"before", "after", "display_name"},
    "identity.user.enable": {"is_active"},
    "identity.user.disable": {"is_active", "sessions_revoked", "tokens_revoked"},
    "identity.password.reset": {"sessions_revoked"},
    "identity.credentials.revoke": {"sessions_revoked", "tokens_revoked", "user_id"},
    "identity.role.create": {"key", "name", "permissions"},
    "identity.role.update": {"before", "after", "name", "description", "permissions"},
    "identity.role.delete": {"key", "permissions"},
    "identity.role.assign": {"role_id", "assigned"},
    "identity.pat.issue": {"user_id", "permissions"},
    "identity.pat.revoke": {"user_id"},
    "identity.admin.initialize": {"source", "username"},
    "dictionary.definition.create": {"before", "after", "key", "name", "description"},
    "dictionary.definition.update": {"before", "after", "name", "description", "is_active"},
    "dictionary.item.create": {"before", "after", "value", "label", "sort_order"},
    "dictionary.item.update": {"before", "after", "label", "sort_order", "is_active"},
    "file.upload.start": {"initial_grants"},
    "file.upload.complete": {"size_bytes"},
    "file.upload.failure": {"reason"},
    "file.upload.residue_cleanup": set(),
    "file.update": {"before", "after", "name", "status"},
    "file.delete.request": set(),
    "file.delete.complete": {"grants_removed", "grant_scope"},
    "file.cleanup.failure": {"reason"},
    "file.grant": {"grant_id", "subject_type", "user_id", "role_id", "action", "added"},
    "file.reference": {"owner", "slot", "added"},
    "model.channel.create": {
        "before",
        "after",
        "name",
        "provider",
        "api_base",
        "credential_ref",
        "billing_credential_ref",
        "upstream_account_ref",
        "is_active",
    },
    "model.channel.update": {
        "before",
        "after",
        "name",
        "api_base",
        "credential_ref",
        "billing_credential_ref",
        "upstream_account_ref",
        "is_active",
    },
    "model.entry.create": {"before", "after", "key", "channel_id", "provider_model", "is_active"},
    "model.entry.update": {"before", "after", "channel_id", "provider_model", "is_active"},
    "model.balance.refresh": {"status"},
    "model.invocation.recover": {"source"},
}
ACTIONS = MappingProxyType({key: frozenset(fields) for key, fields in ACTIONS.items()})


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
