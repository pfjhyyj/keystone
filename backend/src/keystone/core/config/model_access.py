from pydantic import BaseModel, Field, model_validator


class ModelAccessSettings(BaseModel):
    model_cost_concurrency: int = Field(default=2, ge=1, le=8)
    model_inference_concurrency: int = Field(default=4, ge=1, le=32)
    model_scope_concurrency: int = Field(default=4, ge=1, le=16)
    model_send_interval_ms: int = Field(default=0, ge=0, le=60000)
    model_cooldown_seconds: int = Field(default=5, ge=1, le=300)
    model_cooldown_max_seconds: int = Field(default=300, ge=1, le=3600)

    @model_validator(mode="after")
    def valid_model_limits(self):
        if self.model_scope_concurrency > self.model_inference_concurrency:
            raise ValueError("Model scope concurrency must not exceed inference concurrency")
        if self.model_cooldown_seconds > self.model_cooldown_max_seconds:
            raise ValueError("Model cooldown must not exceed its configured maximum")
        return self
