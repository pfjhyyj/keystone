from pydantic import BaseModel, Field


class MaintenanceSettings(BaseModel):
    history_cleanup_enabled: bool = False
    audit_cleanup_enabled: bool = True
    flow_diagnostic_cleanup_enabled: bool = True
    maintenance_cleanup_enabled: bool = True
    session_retention_days: int = Field(default=30, ge=1, le=3650)
    pat_retention_days: int = Field(default=90, ge=1, le=3650)
    audit_retention_days: int = Field(default=30, ge=1, le=3650)
    flow_diagnostic_retention_days: int = Field(default=14, ge=1, le=3650)
    maintenance_retention_days: int = Field(default=14, ge=1, le=3650)
    message_delivery_retention_days: int = Field(default=30, ge=1, le=3650)
