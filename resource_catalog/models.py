from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ResourceType = Literal["workspace_agent", "plugin", "skill"]


def _non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


def _http_url(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError("url must be a non-empty URL without surrounding whitespace")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("url must not contain whitespace or control characters")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must use http or https and include a host")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("url contains an invalid port") from exc
    return value


def _timestamp(value: str, field_name: str) -> str:
    value = _non_empty(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a UTC offset")
    return value


class ResourceSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=5000)
    type: ResourceType
    author: str = Field(min_length=1, max_length=300)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _http_url(value)

    @field_validator("name", "description", "author")
    @classmethod
    def validate_text(cls, value: str, info) -> str:
        return _non_empty(value, info.field_name)


class ResourceRecord(ResourceSubmission):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=500)
    submitted_at: str = Field(min_length=1, max_length=100)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "id")

    @field_validator("submitted_at")
    @classmethod
    def validate_submitted_at(cls, value: str) -> str:
        return _timestamp(value, "submitted_at")


class ResourceReference(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=4096)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "id")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _http_url(value)


class ResourceCatalogue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: Literal[1]
    updated_at: str = Field(min_length=1, max_length=100)
    resource_count: int = Field(ge=0)
    resources: list[ResourceRecord]

    @field_validator("updated_at")
    @classmethod
    def validate_updated_at(cls, value: str) -> str:
        return _timestamp(value, "updated_at")

    @model_validator(mode="after")
    def validate_resource_count(self) -> "ResourceCatalogue":
        if self.resource_count != len(self.resources):
            raise ValueError("resource_count does not match the resources array")
        return self


class ResourceCreated(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["success"]
    resource: ResourceRecord
    perma_url: str | None = None
    feed_url: str


class ResourceExists(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["exists"]
    resource: ResourceReference
    feed_url: str


class ResourceRejected(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["failed"]
    error: str = Field(min_length=1, max_length=5000)

    @field_validator("error")
    @classmethod
    def validate_error(cls, value: str) -> str:
        return _non_empty(value, "error")


ResourceSubmissionResult = Annotated[
    ResourceCreated | ResourceExists | ResourceRejected,
    Field(discriminator="status"),
]
