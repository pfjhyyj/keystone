from pydantic import BaseModel, Field


class MaintenanceSettings(BaseModel):
    history_cleanup_enabled: bool = False
    session_retention_days: int = Field(default=30, ge=1, le=3650)
    pat_retention_days: int = Field(default=90, ge=1, le=3650)
    audit_retention_days: int = Field(default=365, ge=1, le=3650)
    maintenance_retention_days: int = Field(default=30, ge=1, le=3650)
