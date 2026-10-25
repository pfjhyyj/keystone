"""Scoped SQL analytics over invocation facts; never infer fees from tokens or balances."""

from datetime import UTC, timedelta
from decimal import Decimal, localcontext
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Numeric, String, and_, case, cast, exists, func, literal, or_, select, tuple_

from keystone.core.errors import ServiceError
from keystone.core.time_range import history_range
from keystone.platform.identity.services.subjects import user_labels_in_transaction
from keystone.platform.model_access.models import Channel, ModelEntry
from keystone.platform.model_access.models import ModelInvocation as Call
from keystone.platform.model_access.services.queries import history_filters, invocation_source

STATISTICS_TIMEZONE = "Asia/Shanghai"

DIMENSIONS = {
    "channel": (cast(Call.channel_id, String), "channel_id"),
    "model": (cast(Call.model_entry_id, String), "model_entry_id"),
    "upstream_model": (Call.provider_model, "provider_model"),
    "user": (cast(Call.user_id, String), "user_id"),
    "source": (invocation_source(), "source"),
    "flow_run": (Call.flow_context["flow_run_id"].astext, "flow_run_id"),
    "node": (Call.flow_context["node_key"].astext, "node_key"),
    "model_role": (Call.flow_context["model_role"].astext, "model_role"),
    "agent": (Call.flow_context["agent_key"].astext, "agent_key"),
    "operation": (Call.operation, "operation"),
    "status": (Call.status, "status"),
    "cost_status": (Call.cost_check_status, "cost_status"),
    "cost_source": (Call.cost_source, "cost_source"),
    "error": (Call.error_code, "error_code"),
}
COUNTS = (
    "calls",
    "succeeded",
    "failed",
    "unknown",
    "running",
    "known_calls",
    "pending_calls",
    "input_reported",
    "output_reported",
    "duration_reported",
)


def token_value(field):
    value = Call.usage[field]
    return case(
        (
            and_(func.jsonb_typeof(value) == "number", value.astext.op("~")(r"^[0-9]{1,19}$")),
            cast(value.astext, Numeric(38, 0)),
        ),
        else_=None,
    )


def metrics(row):
    return {
        **{field: int(row[field] or 0) for field in COUNTS},
        "unknown_cost_calls": int((row["calls"] or 0) - (row["known_calls"] or 0)),
        "input_tokens": str(row["input_tokens"]) if row["input_tokens"] is not None else None,
        "output_tokens": str(row["output_tokens"]) if row["output_tokens"] is not None else None,
        "avg_duration_ms": float(row["avg_duration_ms"])
        if row["avg_duration_ms"] is not None
        else None,
        "p95_duration_ms": float(row["p95_duration_ms"])
        if row["p95_duration_ms"] is not None
        else None,
    }


def cost(row):
    if row["unit"] is None or row["amount"] is None:
        return None
    with localcontext() as context:
        context.prec = 80
        average = (row["amount"] / row["known_calls"]).quantize(Decimal("1e-18"))
    return {
        "unit": row["unit"],
        "amount": format(row["amount"], "f"),
        "known_calls": int(row["known_calls"]),
        "avg_cost": format(average, "f"),
    }


def drill_filter(dimension, key):
    if key is not None:
        return {DIMENSIONS[dimension][1]: key}
    return {"user_id": "system"} if dimension == "user" else {"missing": dimension}


def group_order(row, query):
    if query.order_by == "cost":
        value = next(
            (Decimal(item["amount"]) for item in row["costs"] if item["unit"] == query.sort_unit),
            None,
        )
        return (value is None, value.copy_negate() if value is not None else 0, row["key"] or "")
    return (False, -row["calls"], row["key"] or "")


class StatisticsService:
    def __init__(self, identity):
        self.identity = identity

    async def labels(self, session, dimension, keys):
        ids = [UUID(key) for key in keys if key is not None]
        if dimension == "user":
            return {
                row["id"]: row["label"] for row in await user_labels_in_transaction(session, ids)
            }
        model = Channel if dimension == "channel" else ModelEntry
        if dimension not in {"channel", "model"}:
            return {}
        rows = await session.execute(select(model).where(model.id.in_(ids)))
        return {
            str(row.id): (row.name if dimension == "channel" else f"{row.name} ({row.key})")
            for row in rows.scalars()
        }

    async def statistics(self, query):
        async with self.identity.access("model_invocation:read") as (session, auth):
            since, until = await history_range(session, query.since, query.until)
            if query.bucket == "hour" and until - since > timedelta(days=31):
                raise ServiceError(422, "invalid_time_range", "按小时统计最多查询 31 天")
            filters = {**query.filters(), "since": since, "until": until}
            conditions = await history_filters(session, auth, **filters)
            key = DIMENSIONS[query.group_by][0]
            duration = case(
                (
                    Call.finished_at >= Call.created_at,
                    func.extract("epoch", Call.finished_at - Call.created_at) * 1000,
                )
            )
            facts = (
                select(
                    key.label("key"),
                    func.date_trunc(query.bucket, Call.created_at, STATISTICS_TIMEZONE).label(
                        "bucket"
                    ),
                    Call.cost_unit.label("unit"),
                    Call.status,
                    Call.cost_check_status,
                    Call.cost_amount,
                    token_value("input_tokens").label("input_tokens"),
                    token_value("output_tokens").label("output_tokens"),
                    duration.label("duration"),
                )
                .where(*conditions)
                .cte("facts")
            )
            f = facts.c
            aggregate = (
                select(
                    f.key,
                    f.bucket,
                    f.unit,
                    func.grouping(f.key, f.bucket, f.unit).label("mask"),
                    func.count().label("calls"),
                    *(
                        func.count().filter(f.status == value).label(value)
                        for value in ("succeeded", "failed", "unknown", "running")
                    ),
                    func.count(f.cost_amount).label("known_calls"),
                    func.count().filter(f.cost_check_status == "pending").label("pending_calls"),
                    func.sum(f.cost_amount).label("amount"),
                    func.sum(f.input_tokens).label("input_tokens"),
                    func.sum(f.output_tokens).label("output_tokens"),
                    func.count(f.input_tokens).label("input_reported"),
                    func.count(f.output_tokens).label("output_reported"),
                    func.count(f.duration).label("duration_reported"),
                    func.avg(f.duration).label("avg_duration_ms"),
                    func.percentile_cont(0.95).within_group(f.duration).label("p95_duration_ms"),
                )
                .group_by(
                    func.grouping_sets(
                        tuple_(),
                        tuple_(f.key),
                        tuple_(f.bucket),
                        tuple_(f.unit),
                        tuple_(f.key, f.unit),
                        tuple_(f.bucket, f.unit),
                    )
                )
                .cte("aggregated")
            )
            a = aggregate.c
            costs = aggregate.alias("group_costs")
            sort_amount = (
                select(costs.c.amount)
                .where(
                    costs.c.mask == 2,
                    costs.c.key.is_not_distinct_from(a.key),
                    costs.c.unit == query.sort_unit,
                )
                .scalar_subquery()
            )
            page = (
                select(a.key)
                .where(a.mask == 3)
                .order_by(
                    sort_amount.desc().nullslast() if query.order_by == "cost" else a.calls.desc(),
                    a.key.asc().nullsfirst(),
                )
                .offset(query.offset)
                .limit(query.limit)
                .cte("page")
            )
            # All metrics, monetary totals, trend and paginated groups share one SQL snapshot.
            rows = (
                (
                    await session.execute(
                        select(
                            aggregate,
                            select(func.count())
                            .select_from(aggregate)
                            .where(a.mask == 3)
                            .scalar_subquery()
                            .label("total_groups"),
                        ).where(
                            or_(
                                a.mask.in_((7, 6, 5, 4)),
                                and_(
                                    a.mask.in_((3, 2)),
                                    exists(
                                        select(1)
                                        .select_from(page)
                                        .where(a.key.is_not_distinct_from(page.c.key))
                                    ),
                                ),
                            )
                        )
                    )
                )
                .mappings()
                .all()
            )
            summary = next(row for row in rows if row["mask"] == 7)
            groups, trend, totals = {}, {}, []
            for row in rows:
                if row["mask"] == 3:
                    groups[row["key"]] = {
                        "key": row["key"],
                        **metrics(row),
                        "costs": [],
                        "filters": drill_filter(query.group_by, row["key"]),
                    }
                if row["mask"] == 5:
                    trend[row["bucket"]] = {
                        "start": row["bucket"].isoformat(),
                        **metrics(row),
                        "costs": [],
                    }
                if row["mask"] == 6 and (value := cost(row)) is not None:
                    totals.append(value)
            for row in rows:
                value = cost(row) if row["mask"] in (2, 4) else None
                if value is not None:
                    (groups[row["key"]] if row["mask"] == 2 else trend[row["bucket"]])[
                        "costs"
                    ].append(value)
            names = (
                await self.labels(session, query.group_by, list(groups))
                if query.group_by in {"channel", "model", "user"}
                else {}
            )
            zone = ZoneInfo(STATISTICS_TIMEZONE)
            for start, item in trend.items():
                local = start.astimezone(zone)
                end = (
                    local.replace(year=local.year + (local.month == 12), month=local.month % 12 + 1)
                    if query.bucket == "month"
                    else start.astimezone(UTC) + timedelta(hours=1)
                    if query.bucket == "hour"
                    else local + timedelta(days=1)
                )
                item["filters"] = {
                    "since": max(start, since).isoformat(),
                    "until": min(end - timedelta(microseconds=1), until).isoformat(),
                }
                item["label"] = local.isoformat()
            for key, item in groups.items():
                item["label"] = (
                    names.get(key, key)
                    if key is not None
                    else ({"user": "系统调用", "error": "无错误码"}.get(query.group_by, "未关联"))
                )
                item["costs"].sort(key=lambda value: value["unit"])
            return {
                "since": since.isoformat(),
                "until": until.isoformat(),
                "timezone": STATISTICS_TIMEZONE,
                "summary": {
                    **metrics(summary),
                    "costs": sorted(totals, key=lambda value: value["unit"]),
                },
                "trend": [trend[key] for key in sorted(trend)],
                "groups": sorted(groups.values(), key=lambda row: group_order(row, query)),
                "total_groups": int(summary["total_groups"]),
            }

    async def options(self, query):
        async with self.identity.access("model_invocation:read") as (session, auth):
            filters = query.filters()
            own_filter = DIMENSIONS[query.dimension][1]
            filters.pop(own_filter, None)
            filters["missing"] = (
                ",".join(
                    value
                    for value in (filters.get("missing") or "").split(",")
                    if value and value != query.dimension
                )
                or None
            )
            conditions = await history_filters(session, auth, **filters)
            column = DIMENSIONS[query.dimension][0]
            if query.dimension == "user":
                has_system = (
                    "系统调用".find(query.q) >= 0 or query.q.lower() in "system"
                ) and await session.scalar(
                    select(Call.id).where(*conditions, Call.user_id.is_(None)).limit(1)
                ) is not None
                rows = await user_labels_in_transaction(
                    session,
                    select(Call.user_id).where(*conditions),
                    q=query.q,
                    offset=max(0, query.offset - int(has_system)),
                    limit=query.limit + 1,
                )
                if has_system and query.offset == 0:
                    rows.insert(0, {"id": "system", "label": "系统调用"})
            else:
                label = column
                statement = select(column.label("id"))
                if query.dimension in {"channel", "model"}:
                    model = Channel if query.dimension == "channel" else ModelEntry
                    statement = statement.join(
                        model,
                        (Call.channel_id if model is Channel else Call.model_entry_id) == model.id,
                    )
                    label = (
                        model.name
                        if model is Channel
                        else model.name + literal(" (") + model.key + literal(")")
                    )
                statement = (
                    statement.add_columns(label.label("label"))
                    .where(
                        *conditions,
                        column.is_not(None),
                        or_(
                            column.icontains(query.q, autoescape=True),
                            label.icontains(query.q, autoescape=True),
                        ),
                    )
                    .distinct()
                    .order_by(label, column)
                    .offset(query.offset)
                    .limit(query.limit + 1)
                )
                rows = [dict(row) for row in (await session.execute(statement)).mappings()]
            return {"items": rows[: query.limit], "has_more": len(rows) > query.limit}
