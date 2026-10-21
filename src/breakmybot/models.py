from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, field_validator

ResultState = Literal["PASSED", "VERIFIED_FAILURE", "INCONCLUSIVE", "SETUP_ERROR", "UNSUPPORTED", "SKIPPED"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DiscoveryRequest(BaseModel):
    url: HttpUrl


class EndpointCandidate(BaseModel):
    method: str
    path: str
    description: str = ""
    score: int = 0
    payload_template: dict[str, Any] = Field(default_factory=dict)


class InstrumentedToolFault(BaseModel):
    tool: str = Field(min_length=1, max_length=100)
    kind: Literal["timeout", "exception", "empty_response", "malformed_response", "delay"]
    expected_substring: str | None = Field(default=None, max_length=1000)
    delay_seconds: float = Field(default=0, ge=0, le=30)
    every_n_calls: int = Field(default=1, ge=1, le=100)


class InstrumentedAssessment(BaseModel):
    auth_env: str = Field(pattern=r"^BREAKMYBOT_INSTRUMENTATION_[A-Z0-9_]+$")
    faults: list[InstrumentedToolFault] = Field(min_length=1, max_length=10)


class DiscoveryResult(BaseModel):
    target_url: str
    reachable: bool
    openapi_found: bool = False
    application_name: str | None = None
    endpoints: list[EndpointCandidate] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class RunRequest(BaseModel):
    target_url: HttpUrl
    endpoint_path: str
    method: Literal["GET", "POST", "PUT", "PATCH"] = "POST"
    payload: dict[str, Any] = Field(default_factory=dict)
    profile: Literal["quick", "standard", "deep"] = "standard"
    application_name: str | None = None
    expected_substring: str | None = None
    allow_repeated_post: bool = False
    auth_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{1,100}$")
    auth_header: str = Field(default="Authorization", pattern=r"^[A-Za-z0-9-]{1,64}$")
    auth_scheme: str = Field(default="Bearer", max_length=24, pattern=r"^[A-Za-z0-9_-]*$")
    memory_assessment: "MemoryAssessment | None" = None
    instrumented_assessment: InstrumentedAssessment | None = None
    use_nrouter_planner: bool = False
    max_latency_seconds: float = Field(default=10, gt=0, le=120)


class MemoryCase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=4000)
    reference: str = Field(min_length=1, max_length=12000)
    expected: str = Field(min_length=1, max_length=4000)
    required_literals: list[str] = Field(default_factory=list, max_length=20)
    forbidden_literals: list[str] = Field(default_factory=list, max_length=20)
    require_citations: bool = False


class MemoryAssessment(BaseModel):
    agent_path: str = Field(min_length=1, max_length=2000)
    cases: list[MemoryCase] = Field(min_length=1, max_length=20)
    send_to_nrouter: bool = False
    auth_env: str | None = Field(default=None, pattern=r"^MEMORYSCOPE_AGENT_[A-Z0-9_]+$")

    @field_validator("agent_path")
    @classmethod
    def path_only(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or "?" in value or "#" in value:
            raise ValueError("MemoryScope agent_path must be an absolute path on the selected target origin")
        return value


RunRequest.model_rebuild()


class ScenarioResult(BaseModel):
    scenario_id: str
    name: str
    category: str
    status: ResultState
    severity: str = "info"
    duration_ms: float | None = None
    http_status: int | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    limitation: str | None = None


class RunRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"run-{uuid4().hex[:12]}")
    status: Literal["CREATED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "PARTIAL"] = "CREATED"
    target_url: str
    endpoint_path: str
    method: str
    request_payload: dict[str, Any] = Field(default_factory=dict)
    application_name: str | None = None
    profile: str
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    scenarios: list[ScenarioResult] = Field(default_factory=list)
    scoring: dict[str, Any] = Field(default_factory=dict)
    planning: dict[str, Any] = Field(default_factory=dict)
    report_paths: dict[str, str] = Field(default_factory=dict)
    error: str | None = None


class RunAccepted(BaseModel):
    run_id: str
    status: str
    status_url: str
