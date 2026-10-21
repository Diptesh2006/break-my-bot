from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from breakmybot import runner, store
from breakmybot.api import app as api_app
from breakmybot.models import MemoryAssessment, MemoryCase, RunRecord, RunRequest, ScenarioResult
from breakmybot.memoryscope import evaluate as evaluate_memory
from breakmybot.planner import mock_plan, plan
from breakmybot.reports import generate
from breakmybot.redaction import redact_payload, redact_text
from breakmybot.scoring import calculate
from breakmybot.target import identify_endpoints, validate_target


def test_target_policy_rejects_public_literal_ip():
    with pytest.raises(ValueError, match="loopback"):
        validate_target("http://8.8.8.8:8000")


def test_payload_redaction_hides_credentials_recursively():
    assert redact_payload({"messages": [{"content": "safe", "api_key": "do-not-store"}]}) == {
        "messages": [{"content": "safe", "api_key": "[REDACTED]"}]}


def test_response_redaction_hides_echoed_credentials_and_bearer_tokens():
    response = 'echo=private-value Authorization: Bearer abc.def.ghi'
    assert redact_text(response, {"api_key": "private-value"}) == (
        'echo=[REDACTED] Authorization: Bearer [REDACTED]')


def test_openapi_discovery_prefers_chat_and_builds_payload():
    spec = {"paths": {"/api/chat": {"post": {"summary": "Chat with the agent", "requestBody": {
        "content": {"application/json": {"schema": {"type": "object", "required": ["input"],
        "properties": {"input": {"type": "string"}}}}}}}}}}
    candidates = identify_endpoints(spec)
    assert candidates[0].path == "/api/chat"
    assert candidates[0].payload_template == {"input": "Hello. Please reply briefly."}


def test_score_excludes_unsupported_from_passes_and_keeps_coverage_separate():
    run = RunRecord(target_url="http://localhost:8000", endpoint_path="/chat", method="POST", profile="standard")
    run.scenarios = [
        ScenarioResult(scenario_id="a", name="a", category="HTTP", status="PASSED"),
        ScenarioResult(scenario_id="b", name="b", category="HTTP", status="PASSED"),
        ScenarioResult(scenario_id="c", name="c", category="HTTP", status="VERIFIED_FAILURE"),
        ScenarioResult(scenario_id="d", name="d", category="Memory", status="UNSUPPORTED"),
    ]
    score = calculate(run)
    assert score["overall_score"] == 67
    assert score["coverage_percent"] == 75
    assert score["scenario_counts"]["UNSUPPORTED"] == 1


def test_report_outputs_are_portable_and_include_limitations(tmp_path: Path):
    run = RunRecord(target_url="http://localhost:8000", endpoint_path="/chat", method="POST", profile="quick")
    run.scoring = calculate(run)
    paths = generate(run, tmp_path)
    assert set(paths) == {"json", "html", "pdf"}
    assert all(Path(path).is_file() and Path(path).stat().st_size > 0 for path in paths.values())
    assert "not a safety probability" in Path(paths["html"]).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_api_dashboard_and_health_routes():
    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.get("/")
        assert response.status_code == 200
        assert "Find the failure before your users do" in response.text
        health = await client.get("/api/health")
        assert health.json()["product"] == "BreakMyBot"


@pytest.mark.asyncio
async def test_runner_executes_and_persists_observed_scenarios(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BREAKMYBOT_DOCKER_MODE", "false")
    run = RunRecord(target_url="http://127.0.0.1:8001", endpoint_path="/api/chat", method="GET", profile="standard")
    store.save(run)

    async def observed_invocation(*_args, **_kwargs):
        return 200, '{"answer":"ok","api_key":"test-secret"}', 5.5, "application/json"

    monkeypatch.setattr(runner, "invoke", observed_invocation)
    request = RunRequest(target_url="http://127.0.0.1:8001", endpoint_path="/api/chat", method="GET",
        profile="standard", expected_substring="ok", payload={"api_key": "test-secret"})
    result = await runner.execute(run.id, request)
    persisted = store.get(run.id)
    assert result.status == "COMPLETED"
    assert result.scoring["overall_score"] == 100
    assert result.scoring["category_scores"]["Output correctness"]["score"] == 100
    assert persisted is not None and len(persisted.scenarios) == 15
    assert "test-secret" not in str(persisted.scenarios)
    assert all(Path(path).is_file() for path in persisted.report_paths.values())


@pytest.mark.asyncio
async def test_memoryscope_bridge_skips_without_explicit_nrouter_consent():
    request = RunRequest(target_url="http://localhost:8001", endpoint_path="/api/chat", method="POST",
        payload={"message": "hello"})
    config = MemoryAssessment(agent_path="/api/chat/sse", cases=[MemoryCase(name="case", question="q",
        reference="r", expected="e")], send_to_nrouter=False)
    results = await evaluate_memory(request, config)
    assert len(results) == 1
    assert results[0].status == "SKIPPED"
    assert "not authorized" in results[0].limitation


def test_deterministic_planner_marks_unsupported_fault_injection_as_unexecutable():
    request = RunRequest(target_url="http://localhost:8000", endpoint_path="/api/chat", method="POST",
        payload={"message": "hello"})
    result = mock_plan(request)
    assert result["status"] == "mock"
    assert result["scenarios"][0]["eligible_for_current_runner"] is True
    assert result["scenarios"][1]["eligible_for_current_runner"] is False
    assert all(item["executed"] is False for item in result["scenarios"])


@pytest.mark.asyncio
async def test_nrouter_planner_needs_explicit_credentials(monkeypatch):
    monkeypatch.delenv("NROUTER_API_KEY", raising=False)
    monkeypatch.delenv("BREAKMYBOT_PLANNER_MODEL", raising=False)
    request = RunRequest(target_url="http://localhost:8000", endpoint_path="/api/chat", method="POST",
        payload={"message": "hello"})
    result = await plan(request)
    assert result["status"] == "unavailable"
    assert result["scenarios"] == []
