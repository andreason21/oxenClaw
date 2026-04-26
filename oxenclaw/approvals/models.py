"""Approval request + result data models."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict)
    requested_at: float = Field(default_factory=time.time)


class ApprovalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: ApprovalStatus
    reason: str | None = None
    resolved_at: float = Field(default_factory=time.time)

    @property
    def approved(self) -> bool:
        return self.status is ApprovalStatus.APPROVED
