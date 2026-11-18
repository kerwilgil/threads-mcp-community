"""Typed public models returned by the MCP tools."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Profile(BaseModel):
    id: str
    username: str
    name: str = ""
    profile_picture_url: str = ""
    biography: str = ""


class ThreadPost(BaseModel):
    id: str
    text: str = ""
    media_type: str = ""
    media_url: str = ""
    thumbnail_url: str = ""
    permalink: str = ""
    timestamp: datetime | None = None


class PublishResult(BaseModel):
    status: Literal["published", "failed", "unknown"]
    idempotency_key: str
    post_id: str = ""
    permalink: str = ""
    recovered: bool = False
    message: str = ""


class TokenRecord(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    access_token: str = Field(repr=False, min_length=1)
    expires_at: datetime | None = None
    account_id: str = ""
    username: str = ""

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Token expiry must include a timezone.")
        return value
