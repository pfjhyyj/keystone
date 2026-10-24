from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base
from keystone.platform.flow.models.flow_run_budget import COUNTER_FIELDS, nonnegative


class FlowBudgetClaim(Base):
    __tablename__ = "flow_budget_claims"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('model','tokens','tool','iteration','file')", name="flow_claim_kind"
        ),
        CheckConstraint(
            "key ~ '^[0-9a-f]{64}$' AND request_digest ~ '^[0-9a-f]{64}$'",
            name="flow_claim_identity",
        ),
        CheckConstraint(
            nonnegative("amounts", COUNTER_FIELDS),
            name="flow_claim_amounts",
        ),
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("keystone.flow_run_budgets.run_id"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    amounts: Mapped[dict] = mapped_column(JSONB)
    request_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
