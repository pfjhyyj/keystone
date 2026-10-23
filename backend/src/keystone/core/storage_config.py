"""Validated configuration for one named storage source."""

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr, StringConstraints, model_validator

StorageKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    driver: Literal["filesystem", "s3"]
    root: Path | None = None
    endpoint: str = ""
    bucket: str = ""
    region: str = "us-east-1"
    prefix: str = ""
    access_key: SecretStr | None = None
    secret_key: SecretStr | None = None

    @model_validator(mode="after")
    def validate_location(self):
        if self.driver == "filesystem":
            if self.root is None:
                raise ValueError("Filesystem storage requires a root")
            self.root = self.root.resolve()
            if self.endpoint or self.bucket or self.prefix or self.access_key or self.secret_key:
                raise ValueError("Filesystem storage does not accept S3 configuration")
        else:
            parsed = urlsplit(self.endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or self.root is not None
                or not self.bucket
                or "/" in self.bucket
                or "\\" in self.bucket
                or not self.region
                or not self.access_key
                or not self.access_key.get_secret_value()
                or not self.secret_key
                or not self.secret_key.get_secret_value()
            ):
                raise ValueError("S3 storage requires an endpoint, bucket, region and credentials")
            # Validate the port without echoing a potentially secret input.
            try:
                _ = parsed.port
            except ValueError:
                raise ValueError("Invalid S3 endpoint port") from None
            self.endpoint = self.endpoint.rstrip("/")
            self.prefix = self.prefix.strip("/")
            if self.prefix and any(
                part in {"", ".", ".."} or "\\" in part for part in self.prefix.split("/")
            ):
                raise ValueError("Invalid storage prefix")
        return self
