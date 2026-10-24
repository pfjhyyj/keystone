"""Only normalized request allowances cross the admission boundary."""

from pydantic import Field, model_validator

from keystone.core.schemas import Input
from keystone.platform.model_access.schemas.operations import Operation

TEXT_OPERATIONS = frozenset({"text.generate", "text.structured", "image.understand"})


class ModelRequestBudget(Input):
    operation: Operation
    max_output_tokens: int = Field(strict=True, ge=0, le=4096)

    @model_validator(mode="after")
    def matches_operation(self):
        if (self.operation in TEXT_OPERATIONS) != (self.max_output_tokens > 0):
            raise ValueError("Token reservation must match the operation")
        return self
