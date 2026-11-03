from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunStatus(str, Enum):
    created = "created"
    running = "running"
    awaiting_approval = "awaiting_approval"
    awaiting_input = "awaiting_input"
    paused = "paused"
    resolved = "resolved"
    failed = "failed"


class Event(BaseModel):
    id: str
    at: str
    kind: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class Hypothesis(BaseModel):
    id: str
    claim: str
    confidence: float
    status: Literal["open", "supported", "rejected"] = "open"
    suggested_action: str | None = None
    action_args: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    id: str
    title: str
    status: Literal["pending", "active", "done"] = "pending"


class Proposal(BaseModel):
    action: str
    service: str
    args: dict[str, Any] = Field(default_factory=dict)
    rationale: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float
    outcome: Literal["pending", "approved", "rejected", "failed", "verified"] = "pending"


class Run(BaseModel):
    id: str
    incident_id: str
    status: RunStatus = RunStatus.created
    policy: str
    model: str | None = None
    created_at: str
    updated_at: str
    step_count: int = 0
    max_steps: int = 20
    summary: str = ""
    plan: list[PlanStep] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    proposal: Proposal | None = None
    memory: dict[str, Any] = Field(default_factory=dict)
    overlay: dict[str, Any] = Field(default_factory=dict)


class AdvanceResult(BaseModel):
    applied: bool
    reason: str
    run: Run
