from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

ResultState = Literal["PASSED", "VERIFIED_FAILURE", "INCONCLUSIVE", "SETUP_ERROR", "UNSUPPORTED", "SKIPPED"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DiscoveryRequest(BaseModel):
    url: HttpUrl


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    target_url: HttpUrl


class ProjectRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"project-{uuid4().hex[:12]}")
    name: str = Field(min_length=1, max_length=120)
    target_url: str
    created_at: datetime = Field(default_factory=utc_now)


class EndpointCandidate(BaseModel):
    method: str
    path: str
    description: str = ""
    score: int = 0
    payload_template: dict[str, Any] = Field(default_factory=dict)
    response_schemas: dict[str, dict[str, Any]] = Field(default_factory=dict)
    streaming: bool = False


class InstrumentedToolFault(BaseModel):
    tool: str = Field(min_length=1, max_length=100)
    kind: Literal["timeout", "exception", "empty_response", "malformed_response", "delay", "lost_acknowledgment"]
    expected_substring: str | None = Field(default=None, max_length=1000)
    delay_seconds: float = Field(default=0, ge=0, le=30)
    every_n_calls: int = Field(default=1, ge=1, le=100)
    on_call: int | None = Field(default=None, ge=1, le=100)


class InstrumentedFaultChain(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    faults: list[InstrumentedToolFault] = Field(min_length=2, max_length=5)
    expected_substring: str | None = Field(default=None, max_length=1000)


class InstrumentedAssessment(BaseModel):
    auth_env: str = Field(pattern=r"^BREAKMYBOT_INSTRUMENTATION_[A-Z0-9_]+$")
    faults: list[InstrumentedToolFault] = Field(default_factory=list, max_length=10)
    chains: list[InstrumentedFaultChain] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def require_scenario(self):
        if not self.faults and not self.chains:
            raise ValueError("instrumented assessment requires at least one fault or fault chain")
        return self


MemoryTestCategory = Literal[
    "outdated_knowledge", "contradictory_information", "cross_session_consistency",
    "cross_agent_memory", "access_boundaries", "memory_poisoning",
]


class MemoryInvocation(BaseModel):
    endpoint_path: str = Field(min_length=1, max_length=2000)
    payload: dict[str, Any] = Field(default_factory=dict)
    expected_literals: list[str] = Field(min_length=1, max_length=20)
    forbidden_literals: list[str] = Field(default_factory=list, max_length=20)
    auth_env: str | None = Field(default=None, pattern=r"^BREAKMYBOT_CASE_[A-Z0-9_]+$")
    auth_header: str = Field(default="Authorization", pattern=r"^[A-Za-z0-9-]{1,64}$")
    auth_scheme: str = Field(default="Bearer", max_length=24, pattern=r"^[A-Za-z0-9_-]*$")

    @field_validator("endpoint_path")
    @classmethod
    def path_only(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or "?" in value or "#" in value:
            raise ValueError("memory invocation endpoint_path must be a path on the selected target origin")
        return value

    @field_validator("expected_literals", "forbidden_literals")
    @classmethod
    def nonempty_literals(cls, values: list[str]) -> list[str]:
        if any(not value or not value.strip() or len(value) > 1000 for value in values):
            raise ValueError("memory invariants must be non-empty literals of at most 1000 characters")
        return values


class InstrumentedMemoryScenario(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    category: MemoryTestCategory
    endpoint_path: str = Field(min_length=1, max_length=2000)
    payload: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, str] = Field(min_length=1, max_length=10)
    seed: dict[str, Any]
    expected_literals: list[str] = Field(min_length=1, max_length=20)
    forbidden_literals: list[str] = Field(default_factory=list, max_length=20)
    invocations: list[MemoryInvocation] = Field(default_factory=list, max_length=5)
    faults: list[InstrumentedToolFault] = Field(default_factory=list, max_length=5)

    @field_validator("expected_literals", "forbidden_literals")
    @classmethod
    def nonempty_literals(cls, values: list[str]) -> list[str]:
        if any(not value or not value.strip() or len(value) > 1000 for value in values):
            raise ValueError("memory invariants must be non-empty literals of at most 1000 characters")
        return values

    @model_validator(mode="after")
    def require_sequence_for_cross_context_checks(self):
        if self.category in {"cross_session_consistency", "cross_agent_memory"} and len(self.invocations) < 2:
            raise ValueError("cross-session and cross-agent scenarios require at least two invocation steps")
        return self

    @field_validator("endpoint_path")
    @classmethod
    def path_only(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or "?" in value or "#" in value:
            raise ValueError("memory scenario endpoint_path must be a path on the selected target origin")
        return value


class InstrumentedMemoryAssessment(BaseModel):
    auth_env: str = Field(pattern=r"^BREAKMYBOT_MEMORY_[A-Z0-9_]+$")
    scenarios: list[InstrumentedMemoryScenario] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_unique_cases_and_seed_size(self):
        ids = [scenario.id for scenario in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("memory scenario identifiers must be unique")
        if any(len(json.dumps(scenario.seed, ensure_ascii=False).encode("utf-8")) > 16384
               for scenario in self.scenarios):
            raise ValueError("each memory seed must be at most 16 KiB")
        return self


class DiscoveryResult(BaseModel):
    target_url: str
    reachable: bool
    openapi_found: bool = False
    application_name: str | None = None
    endpoints: list[EndpointCandidate] = Field(default_factory=list)
    health_endpoints: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class RunRequest(BaseModel):
    target_url: HttpUrl
    project_id: str | None = Field(default=None, pattern=r"^project-[a-f0-9]{12}$")
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
    instrumented_memory_assessment: InstrumentedMemoryAssessment | None = None
    execute_memory_scenarios: bool = False
    instrumented_assessment: InstrumentedAssessment | None = None
    use_nrouter_planner: bool = False
    execute_planned_scenarios: bool = False
    max_planned_scenarios: int = Field(default=3, ge=1, le=5)
    planned_scenario_mode: Literal["individual", "cascading"] | None = None
    execute_approved_instrumented_scenarios: bool = True
    planner_token_budget: int = Field(default=6000, ge=256, le=6000)
    max_latency_seconds: float = Field(default=10, gt=0, le=120)

    @model_validator(mode="after")
    def validate_planned_execution_opt_in(self):
        if self.instrumented_memory_assessment is not None and not self.execute_memory_scenarios:
            raise ValueError("instrumented memory scenarios require explicit execution consent")
        if self.execute_memory_scenarios and self.instrumented_memory_assessment is None:
            raise ValueError("memory scenario execution requires an operator-supplied scenario file")
        if self.execute_memory_scenarios and self.method != "GET" and not self.allow_repeated_post:
            raise ValueError("memory scenario execution requires explicit repeated-call authorization")
        combined_faults = [fault for scenario in (self.instrumented_memory_assessment.scenarios
            if self.instrumented_memory_assessment else []) for fault in scenario.faults]
        if combined_faults and self.instrumented_assessment is None:
            raise ValueError("combined memory/tool experiments require an operator-approved tool fault allowlist")
        if combined_faults:
            approved = [fault.model_dump(exclude={"expected_substring"})
                for fault in self.instrumented_assessment.faults]
            approved.extend(fault.model_dump(exclude={"expected_substring"})
                for chain in self.instrumented_assessment.chains for fault in chain.faults)
            if any(fault.model_dump(exclude={"expected_substring"}) not in approved
                   for fault in combined_faults):
                raise ValueError("combined memory/tool experiments may use only operator-approved faults")
        if self.execute_planned_scenarios:
            if not self.use_nrouter_planner:
                raise ValueError("planned scenario execution requires the nRouter planner")
            if self.instrumented_assessment is None:
                raise ValueError("planned scenario execution requires an approved instrumentation allowlist")
            if self.method != "GET" and not self.allow_repeated_post:
                raise ValueError("planned scenario execution requires explicit repeated-call authorization")
        elif not self.execute_approved_instrumented_scenarios:
            raise ValueError("approved instrumentation cannot be skipped without generated scenario execution")
        return self


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
    project_id: str | None = None
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
