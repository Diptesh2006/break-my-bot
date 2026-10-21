import json
import sqlite3
from pathlib import Path

import pytest
import httpx
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI

from breakmybot import runner, store
from breakmybot.api import app as api_app
from breakmybot.models import MemoryAssessment, MemoryCase, ProjectRecord, RunRecord, RunRequest, ScenarioResult
from breakmybot.memoryscope import evaluate as evaluate_memory, supports_external_answer_evaluation
from breakmybot.instrumentation import ToolFault, instrument, instrument_tool_calls
from breakmybot.planner import mock_plan, plan
from breakmybot.reports import generate
from breakmybot.redaction import redact_evidence, redact_payload, redact_text
from breakmybot.scoring import calculate
from breakmybot.target import identify_endpoints, preflight as run_preflight, validate_target


def test_target_policy_rejects_public_literal_ip():
    with pytest.raises(ValueError, match="loopback"):
        validate_target("http://8.8.8.8:8000")


def test_target_policy_rejects_query_string_credentials():
    with pytest.raises(ValueError, match="query parameters"):
        validate_target("http://localhost:8000/?token=secret")


def test_sqlite_job_queue_persists_claim_cancellation_and_suppresses_stale_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    run = RunRecord(target_url="http://localhost:8000", endpoint_path="/chat", method="GET", profile="quick")
    store.save(run)
    store.enqueue(run.id, {"endpoint_path": "/chat", "method": "GET"})

    assert store.claim_job(lease_seconds=1) == (run.id, {"endpoint_path": "/chat", "method": "GET"})
    assert store.claim_job() is None
    assert store.cancel_job(run.id) is True
    assert store.job_cancel_requested(run.id) is True
    with store.connect() as db:
        db.execute("UPDATE jobs SET lease_until=0 WHERE run_id=?", (run.id,))
        db.execute("UPDATE runs SET payload=? WHERE id=?", (run.model_copy(update={"status": "RUNNING"}).model_dump_json(), run.id))
    store.expire_stale_jobs()
    assert store.get(run.id).status == "FAILED"
    assert "replay was suppressed" in store.get(run.id).error
    assert store.claim_job() is None


def test_project_and_run_association_survive_sqlite_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    project = ProjectRecord(name="Support agent", target_url="http://localhost:8000")
    store.save_project(project)
    run = RunRecord(target_url="http://localhost:8000", endpoint_path="/api/chat", method="GET",
        profile="quick", project_id=project.id)
    store.save(run)

    assert store.get_project(project.id) == project
    assert store.list_projects() == [project]
    assert store.latest(project_id=project.id) == [run]
    assert store.latest(project_id="project-000000000000") == []


def test_project_schema_migrates_existing_runs_without_losing_records(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    legacy = RunRecord(target_url="http://localhost:8000", endpoint_path="/api/chat", method="GET",
        profile="quick")
    payload = legacy.model_dump(mode="json")
    payload.pop("project_id")
    db = sqlite3.connect(tmp_path / "breakmybot.sqlite3")
    db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("INSERT INTO runs VALUES(?,?,?)", (legacy.id, legacy.created_at.isoformat(),
        json.dumps(payload)))
    db.commit()
    db.close()

    assert store.latest() == [legacy]
    with store.connect() as migrated:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(runs)")}
    assert "project_id" in columns


def test_sqlite_migrations_are_recorded_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))

    with store.connect() as db:
        assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]
    with store.connect() as db:
        assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]


def test_legacy_database_gets_migration_history(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    legacy = sqlite3.connect(tmp_path / "breakmybot.sqlite3")
    legacy.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
    legacy.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
    legacy.execute("CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, event TEXT NOT NULL)")
    legacy.execute("CREATE TABLE jobs (run_id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)")
    legacy.commit()
    legacy.close()

    with store.connect() as migrated:
        assert migrated.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]
        assert {row[1] for row in migrated.execute("PRAGMA table_info(runs)")} >= {"id", "project_id"}


def test_postgres_adapter_translates_parameters_and_closes_transaction():
    class Cursor:
        def execute(self, statement, parameters):
            self.statement = statement
            self.parameters = parameters

    class Connection:
        def __init__(self):
            self.cursor_value = Cursor()
            self.committed = False
            self.closed = False

        def cursor(self):
            return self.cursor_value

        def commit(self):
            self.committed = True

        def rollback(self):
            raise AssertionError("unexpected rollback")

        def close(self):
            self.closed = True

    raw = Connection()
    adapter = store.PostgresConnection(raw)
    cursor = adapter.execute("SELECT * FROM runs WHERE id=? AND project_id=?", ("run", "project"))
    assert cursor.statement == "SELECT * FROM runs WHERE id=%s AND project_id=%s"
    assert cursor.parameters == ("run", "project")
    with adapter:
        pass
    assert raw.committed and raw.closed


def test_postgres_migrations_include_advisory_lock_and_legacy_upgrade():
    class Cursor:
        def __init__(self, rows=()):
            self.rows = list(rows)

        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __init__(self):
            self.statements = []

        def execute(self, statement, parameters=()):
            self.statements.append(statement)
            return Cursor([])

    fake = FakeConnection()
    store._migrate_postgres(fake)
    joined = "\\n".join(fake.statements)
    assert "pg_advisory_xact_lock" in joined
    assert "BIGSERIAL" in joined
    assert "ALTER TABLE runs ADD COLUMN IF NOT EXISTS project_id TEXT" in joined
    assert "INSERT INTO schema_migrations(version) VALUES(2)" in joined


@pytest.mark.asyncio
async def test_project_api_filters_runs_and_rejects_cross_target_association(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        created = await client.post("/api/projects", json={"name": "Support agent",
            "target_url": "http://localhost:8000"})
        assert created.status_code == 201
        project_id = created.json()["id"]
        response = await client.post("/api/runs", json={"target_url": "http://localhost:8000",
            "project_id": project_id, "endpoint_path": "/api/chat", "method": "GET", "profile": "quick"})
        assert response.status_code == 202
        filtered = await client.get("/api/runs", params={"project_id": project_id})
        assert [run["id"] for run in filtered.json()] == [response.json()["run_id"]]
        mismatch = await client.post("/api/runs", json={"target_url": "http://localhost:8001",
            "project_id": project_id, "endpoint_path": "/api/chat", "method": "GET", "profile": "quick"})
        assert mismatch.status_code == 422


@pytest.mark.asyncio
async def test_create_run_enqueues_without_running_in_api_process(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    from breakmybot.api import create_run

    request = RunRequest(target_url="http://localhost:8000", endpoint_path="/chat", method="GET", profile="quick")
    accepted = await create_run(request)
    claimed = store.claim_job()
    assert accepted.run_id == claimed[0]
    assert RunRequest.model_validate(claimed[1]).endpoint_path == "/chat"


@pytest.mark.asyncio
async def test_worker_processes_and_acknowledges_a_queued_run(tmp_path, monkeypatch):
    from breakmybot import worker

    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    request = RunRequest(target_url="http://localhost:8000", endpoint_path="/chat", method="GET", profile="quick")
    run = RunRecord(target_url=str(request.target_url), endpoint_path=request.endpoint_path,
        method=request.method, profile=request.profile)
    store.save(run)
    store.enqueue(run.id, request.model_dump(mode="json"))

    async def fake_execute(run_id, _request, _cancel):
        record = store.get(run_id)
        record.status = "COMPLETED"
        store.save(record)
        return record

    monkeypatch.setattr(worker.runner, "execute", fake_execute)
    assert await worker.process_one_job() is True
    assert await worker.process_one_job() is False
    with store.connect() as db:
        assert db.execute("SELECT status FROM jobs WHERE run_id=?", (run.id,)).fetchone()[0] == "DONE"


def test_payload_redaction_hides_credentials_recursively():
    assert redact_payload({"messages": [{"content": "safe", "api_key": "do-not-store"}]}) == {
        "messages": [{"content": "safe", "api_key": "[REDACTED]"}]}


def test_nested_untrusted_evidence_redacts_secret_keys_and_bearer_values():
    observed = redact_evidence({"answer": "Authorization: Bearer abc.def.ghi",
        "judge": {"api_key": "nrouter-secret", "note": "safe"}}, ("nrouter-secret",))
    assert observed == {"answer": "Authorization: Bearer [REDACTED]",
        "judge": {"api_key": "[REDACTED]", "note": "safe"}}


def test_response_redaction_hides_echoed_credentials_and_bearer_tokens():
    response = 'echo=private-value Authorization: Bearer abc.def.ghi'
    assert redact_text(response, {"api_key": "private-value"}) == (
        'echo=[REDACTED] Authorization: Bearer [REDACTED]')


@pytest.mark.asyncio
async def test_target_preflight_redacts_echoed_payload_and_auth_token(monkeypatch):
    async def echo_target(*_args, **_kwargs):
        return 200, 'echo=private-value Authorization: Bearer abc.def.ghi', 1.2, "text/plain"

    monkeypatch.setattr("breakmybot.target.invoke", echo_target)
    result = await run_preflight("http://127.0.0.1:8000", "/chat", "POST",
        {"api_key": "private-value"}, headers={"Authorization": "Bearer abc.def.ghi"})
    assert result["response_excerpt"] == "echo=[REDACTED] Authorization: Bearer [REDACTED]"


@pytest.mark.asyncio
async def test_instrumented_tool_timeout_is_explicit_and_recorded():
    controller = instrument_tool_calls(ToolFault(tool="lookup", kind="timeout"))
    async with controller:
        with pytest.raises(TimeoutError, match="Injected timeout"):
            await controller.invoke("lookup", lambda: "must not run")
    assert controller.events == [{"type": "fault.injected", "tool": "lookup", "kind": "timeout",
                                  "call": 1, "source": "instrumented_tool"}]
    with pytest.raises(RuntimeError, match="async with"):
        await controller.invoke("lookup", lambda: "inactive")


@pytest.mark.asyncio
async def test_instrumented_fault_can_be_configured_for_every_nth_call():
    calls = 0

    def lookup():
        nonlocal calls
        calls += 1
        return "real result"

    controller = instrument_tool_calls(ToolFault(tool="lookup", kind="empty_response", every_n_calls=2))
    async with controller:
        assert await controller.invoke("lookup", lookup) == "real result"
        assert await controller.invoke("lookup", lookup) == ""
    assert calls == 1
    assert [event["type"] for event in controller.events] == [
        "tool.call", "tool.completed", "fault.injected"]


@pytest.mark.asyncio
async def test_instrumented_async_callable_object_is_awaited():
    class AsyncTool:
        async def __call__(self, value):
            return f"result:{value}"

    controller = instrument_tool_calls()
    async with controller:
        result = await controller.invoke("lookup", AsyncTool(), "item")
    assert result == "result:item"
    assert controller.events[-1]["type"] == "tool.completed"


@pytest.mark.asyncio
async def test_instrumentation_control_session_arms_injects_observes_and_cleans_up():
    target = FastAPI()
    token = "integration-test-token-1234567890"
    controller = instrument(target, token=token, enabled=True)

    @target.post("/chat")
    async def chat():
        try:
            value = await controller.invoke("catalog", lambda: "real result")
        except TimeoutError:
            value = "safe fallback"
        return {"answer": value}

    async with AsyncClient(transport=ASGITransport(app=target), base_url="http://test") as client:
        assert (await client.get("/__breakmybot__/capabilities")).json()["contract"] == "breakmybot-instrumentation-v1"
        session_id = "a" * 32
        assert (await client.put(f"/__breakmybot__/sessions/{session_id}", json={"tool": "catalog", "kind": "timeout"})).status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        invalid = await client.put("/__breakmybot__/sessions/unsafe", json={"tool": "catalog", "kind": "timeout"}, headers=headers)
        assert invalid.status_code == 422
        armed = await client.put(f"/__breakmybot__/sessions/{session_id}", json={"tool": "catalog", "kind": "timeout"}, headers=headers)
        assert armed.status_code == 200 and armed.json()["armed"]
        response = await client.post("/chat", headers={"X-BreakMyBot-Session": session_id})
        assert response.json()["answer"] == "safe fallback"
        events = await client.get(f"/__breakmybot__/sessions/{session_id}/events", headers=headers)
        assert events.json()["events"][0]["type"] == "fault.injected"
        cleanup = await client.delete(f"/__breakmybot__/sessions/{session_id}", headers=headers)
        assert cleanup.status_code == 200 and cleanup.json()["cleaned_up"]
    assert controller._sessions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(("fallback", "expected", "expected_state"), [
    ("safe fallback", "safe fallback", "PASSED"),
    ("unsafe answer", "safe fallback", "VERIFIED_FAILURE"),
])
async def test_runner_scores_confirmed_instrumented_fault_with_explicit_invariant(
    tmp_path, monkeypatch, fallback, expected, expected_state
):
    target = FastAPI()
    token = "integration-test-token-1234567890"
    controller = instrument(target, token=token, enabled=True)

    @target.post("/chat")
    async def chat():
        try:
            answer = await controller.invoke("catalog", lambda: "real lookup")
        except TimeoutError:
            answer = fallback
        return {"answer": answer}

    original_client = httpx.AsyncClient

    def control_client_factory(*args, **kwargs):
        return original_client(*args, **kwargs, transport=ASGITransport(app=target))

    async def target_invoke(url, path, method, payload, timeout, *, docker_mode=False, headers=None):
        async with original_client(transport=ASGITransport(app=target), base_url=url) as client:
            response = await client.request(method, path, json=payload, headers=headers)
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    monkeypatch.setattr("breakmybot.instrumentation.httpx.AsyncClient", control_client_factory)
    monkeypatch.setattr(runner, "invoke", target_invoke)
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BREAKMYBOT_DOCKER_MODE", "false")
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", token)
    request = RunRequest(target_url="http://127.0.0.1:8001", endpoint_path="/chat", method="POST",
        profile="quick", allow_repeated_post=True,
        instrumented_assessment={"auth_env": "BREAKMYBOT_INSTRUMENTATION_TOKEN", "faults": [{
            "tool": "catalog", "kind": "timeout", "expected_substring": expected}]})
    run = RunRecord(target_url=str(request.target_url), endpoint_path=request.endpoint_path,
        method=request.method, profile=request.profile)
    store.save(run)

    result = await runner.execute(run.id, request)

    tool_result = next(item for item in result.scenarios if item.category == "Tool reliability")
    assert tool_result.status == expected_state, tool_result.model_dump_json()
    assert tool_result.evidence["fault_injected"] is True
    assert tool_result.evidence["cleanup_succeeded"] is True
    assert tool_result.evidence["expected_substring_passed"] is (expected_state == "PASSED")
    assert controller._sessions == {}
    assert token not in result.model_dump_json()


@pytest.mark.asyncio
async def test_runner_scores_ordered_fault_chain_with_provenance_and_cleanup(tmp_path, monkeypatch):
    target = FastAPI()
    token = "integration-test-token-1234567890"
    controller = instrument(target, token=token, enabled=True)

    @target.post("/chain")
    async def chain():
        try:
            await controller.invoke("customer_context", lambda: "context")
        except RuntimeError:
            pass
        try:
            await controller.invoke("refund_policy", lambda: "policy")
        except TimeoutError:
            return {"answer": "Please contact support."}
        return {"answer": "Please contact support."}

    original_client = httpx.AsyncClient
    monkeypatch.setattr("breakmybot.instrumentation.httpx.AsyncClient",
        lambda *args, **kwargs: original_client(*args, **kwargs, transport=ASGITransport(app=target)))

    async def target_invoke(url, path, method, payload, timeout, *, docker_mode=False, headers=None):
        async with original_client(transport=ASGITransport(app=target), base_url=url) as client:
            response = await client.request(method, path, json=payload, headers=headers)
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    monkeypatch.setattr(runner, "invoke", target_invoke)
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BREAKMYBOT_DOCKER_MODE", "false")
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", token)
    request = RunRequest(target_url="http://127.0.0.1:8001", endpoint_path="/chain", method="POST",
        profile="quick", allow_repeated_post=True,
        instrumented_assessment={"auth_env": "BREAKMYBOT_INSTRUMENTATION_TOKEN", "chains": [{
            "name": "context-then-policy", "faults": [
                {"tool": "customer_context", "kind": "exception"},
                {"tool": "refund_policy", "kind": "timeout"}],
            "expected_substring": "contact support"}]})
    run = RunRecord(target_url=str(request.target_url), endpoint_path=request.endpoint_path,
        method=request.method, profile=request.profile)
    store.save(run)

    result = await runner.execute(run.id, request)

    tool_result = next(item for item in result.scenarios if item.category == "Tool reliability")
    assert tool_result.status == "PASSED", tool_result.model_dump_json()
    assert tool_result.evidence["fault_injected"] is True
    assert tool_result.evidence["faults"] == [
        {"tool": "customer_context", "kind": "exception"},
        {"tool": "refund_policy", "kind": "timeout"}]
    assert tool_result.evidence["cleanup_succeeded"] is True
    assert controller._sessions == {}


@pytest.mark.asyncio
async def test_lost_acknowledgment_retry_verifies_actual_duplicate_and_idempotent_control(tmp_path, monkeypatch):
    import importlib.util
    import sys

    token = "duplicate-action-test-token-1234567890"
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_ENABLED", "true")
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", token)
    monkeypatch.setenv("BREAKMYBOT_DOCKER_MODE", "false")
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    demo_path = Path(__file__).resolve().parents[1] / "examples" / "demo-agent" / "app.py"
    spec = importlib.util.spec_from_file_location("duplicate_action_demo_agent", demo_path)
    assert spec and spec.loader
    demo_agent = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = demo_agent
    spec.loader.exec_module(demo_agent)

    original_client = httpx.AsyncClient

    def target_client(*args, **kwargs):
        return original_client(*args, **kwargs, transport=ASGITransport(app=demo_agent.app))

    monkeypatch.setattr("breakmybot.instrumentation.httpx.AsyncClient", target_client)

    async def invoke_demo(url, path, method, payload, _timeout, *, docker_mode=False, headers=None):
        async with original_client(transport=ASGITransport(app=demo_agent.app), base_url=url) as client:
            response = await client.request(method, path, json=payload, headers=headers)
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    monkeypatch.setattr(runner, "invoke", invoke_demo)
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", token)

    async def assess(idempotency_key):
        async with original_client(transport=ASGITransport(app=demo_agent.app),
                                  base_url="http://127.0.0.1:8001") as client:
            await client.post("/api/refund/reset")
        request = RunRequest(target_url="http://127.0.0.1:8001", endpoint_path="/api/refund/retry",
            method="POST", payload={"idempotency_key": idempotency_key}, profile="quick",
            allow_repeated_post=True, instrumented_assessment={"auth_env": "BREAKMYBOT_INSTRUMENTATION_TOKEN",
                "faults": [{"tool": "refund_commit", "kind": "lost_acknowledgment", "on_call": 1,
                    "expected_substring": "side-effect count 1"}]})
        run = RunRecord(target_url=str(request.target_url), endpoint_path=request.endpoint_path,
            method=request.method, profile=request.profile)
        results = await runner._run_instrumented_scenarios(run, request, 10, None, None)
        async with original_client(transport=ASGITransport(app=demo_agent.app),
                                  base_url="http://127.0.0.1:8001") as client:
            state = (await client.get("/api/refund/state")).json()
        return results[0], state

    unsafe, unsafe_state = await assess(None)
    assert unsafe.status == "VERIFIED_FAILURE"
    assert unsafe.evidence["fault_injected"] is True
    assert unsafe.evidence["cleanup_succeeded"] is True
    assert "side-effect count 2" in unsafe.evidence["response_excerpt"]
    assert unsafe_state["side_effect_count"] == 2
    assert any(event.get("type") == "fault.acknowledgment_lost" for event in unsafe.evidence["injection_events"])

    safe, safe_state = await assess("breakmybot-test-refund-001")
    assert safe.status == "PASSED"
    assert safe.evidence["fault_injected"] is True
    assert safe.evidence["cleanup_succeeded"] is True
    assert "side-effect count 1" in safe.evidence["response_excerpt"]
    assert safe_state["side_effect_count"] == 1


@pytest.mark.asyncio
async def test_instrumented_control_token_requires_https_for_non_loopback_target(monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", "x" * 40)
    request = RunRequest(target_url="http://192.0.2.10:8443", endpoint_path="/chat", method="GET",
        instrumented_assessment={"auth_env": "BREAKMYBOT_INSTRUMENTATION_TOKEN", "faults": [{
            "tool": "lookup", "kind": "timeout"}]})
    run = RunRecord(target_url=str(request.target_url), endpoint_path=request.endpoint_path,
        method=request.method, profile=request.profile)

    results = await runner._run_instrumented_scenarios(run, request, 10, None, None)

    assert results[0].status == "SETUP_ERROR"
    assert "require HTTPS" in results[0].limitation


def test_openapi_discovery_prefers_chat_and_builds_payload():
    spec = {"paths": {"/api/chat": {"post": {"summary": "Chat with the agent", "requestBody": {
        "content": {"application/json": {"schema": {"type": "object", "required": ["input"],
        "properties": {"input": {"type": "string"}}}}}}}}}}
    candidates = identify_endpoints(spec)
    assert candidates[0].path == "/api/chat"
    assert candidates[0].payload_template == {"input": "Hello. Please reply briefly."}


def test_openapi_endpoint_ranking_prefers_schema_with_conversational_input():
    spec = {"paths": {
        "/api/status": {"post": {"summary": "Agent status", "requestBody": {"content": {
            "application/json": {"schema": {"type": "object", "properties": {"verbose": {"type": "boolean"}}}}}}}},
        "/api/chat": {"post": {"summary": "Chat with the agent", "requestBody": {"content": {
            "application/json": {"schema": {"type": "object", "required": ["messages"],
                "properties": {"messages": {"type": "array", "items": {"type": "object"}}}}}}}}},
    }}
    candidates = identify_endpoints(spec)
    assert candidates[0].path == "/api/chat"
    assert candidates[0].payload_template == {"messages": [{"role": "user", "content": "Hello. Please reply briefly."}]}


def test_score_excludes_unsupported_from_passes_and_keeps_coverage_separate():
    run = RunRecord(target_url="http://localhost:8000", endpoint_path="/chat", method="POST", profile="standard")
    run.scenarios = [
        ScenarioResult(scenario_id="a", name="a", category="HTTP reliability", status="PASSED"),
        ScenarioResult(scenario_id="b", name="b", category="HTTP reliability", status="PASSED"),
        ScenarioResult(scenario_id="c", name="c", category="HTTP reliability", status="VERIFIED_FAILURE"),
        ScenarioResult(scenario_id="d", name="d", category="Memory integrity", status="UNSUPPORTED"),
    ]
    score = calculate(run)
    assert score["overall_score"] is None
    assert score["observed_weight_percent"] == 20
    assert score["coverage_percent"] == 75
    assert score["scenario_counts"]["UNSUPPORTED"] == 1
    assert score["category_scores"]["Memory integrity"]["score"] is None
    assert score["category_scores"]["Memory integrity"]["coverage_percent"] == 0


def test_weighted_score_matches_hand_calculation_and_exposes_observed_weight():
    run = RunRecord(target_url="http://localhost:8000", endpoint_path="/chat", method="POST", profile="standard")
    run.scenarios = [
        ScenarioResult(scenario_id="http-pass", name="HTTP pass", category="HTTP reliability", status="PASSED"),
        ScenarioResult(scenario_id="http-fail", name="HTTP fail", category="HTTP reliability", status="VERIFIED_FAILURE"),
        ScenarioResult(scenario_id="tool-pass", name="Tool pass", category="Tool reliability", status="PASSED"),
    ]
    score = calculate(run)
    # (0.20 * 50 + 0.20 * 100) / 0.40 = 75
    assert score["methodology"] == "breakmybot-score-v3"
    assert score["overall_score"] is None
    assert score["observed_weight_percent"] == 40
    assert score["category_scores"]["HTTP reliability"]["weight"] == 0.20


def test_score_is_not_established_below_half_of_rubric_weight():
    run = RunRecord(target_url="http://localhost:8000", endpoint_path="/chat", method="GET", profile="quick")
    run.scenarios = [ScenarioResult(scenario_id=str(i), name="Latency", category="Latency", status="PASSED")
                     for i in range(3)]
    score = calculate(run)
    assert score["observed_weight_percent"] == 10
    assert score["overall_score"] is None
    assert score["score_label"] == "Not established"


def test_score_lists_observed_instrumentation_and_removes_it_from_untested():
    run = RunRecord(target_url="http://localhost:8000", endpoint_path="/chat", method="POST", profile="standard")
    run.scenarios = [
        ScenarioResult(scenario_id="http", name="HTTP", category="HTTP reliability", status="PASSED"),
        ScenarioResult(scenario_id="response", name="Response", category="Response validity", status="PASSED"),
        ScenarioResult(scenario_id="latency", name="Latency", category="Latency", status="PASSED"),
        ScenarioResult(scenario_id="instrumented-tool-1", name="Tool timeout", category="Tool reliability",
            status="PASSED", evidence={"fault_injected": True, "cleanup_succeeded": True}),
    ]
    score = calculate(run)
    assert score["score_label"] == "Observed reliability score"
    assert "instrumented tool faults" in score["observed_scopes"]
    assert "internal tool fault injection" not in score["untested_capabilities"]


def test_setup_errors_do_not_claim_an_observed_scope():
    run = RunRecord(target_url="http://localhost:8000", endpoint_path="/chat", method="POST", profile="standard")
    run.scenarios = [ScenarioResult(scenario_id="setup", name="setup", category="HTTP reliability",
        status="SETUP_ERROR"), ScenarioResult(scenario_id="memory", name="memory", category="Memory integrity",
        status="UNSUPPORTED")]
    result = calculate(run)
    assert result["observed_scopes"] == []
    assert result["category_scores"]["HTTP reliability"]["score"] is None


def test_report_outputs_are_portable_and_include_limitations(tmp_path: Path):
    from jsonschema import Draft202012Validator, FormatChecker

    run = RunRecord(target_url="http://localhost:8000/", endpoint_path="/chat", method="POST", profile="quick")
    run.scoring = calculate(run)
    paths = generate(run, tmp_path)
    assert set(paths) == {"json", "html", "pdf"}
    assert all(Path(path).is_file() and Path(path).stat().st_size > 0 for path in paths.values())
    assert "not a safety probability" in Path(paths["html"]).read_text(encoding="utf-8")
    assert "http://localhost:8000/chat" in Path(paths["html"]).read_text(encoding="utf-8")
    schema_path = Path(__file__).resolve().parents[1] / "docs" / "report-schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    model_schema = RunRecord.model_json_schema()
    report_properties = set(schema["properties"]) - {"$schema", "report_schema_version"}
    assert report_properties == set(model_schema["properties"])
    model_scenario = model_schema["$defs"]["ScenarioResult"]
    report_scenario = schema["$defs"]["ScenarioResult"]
    assert set(report_scenario["properties"]) == set(model_scenario["properties"])
    assert schema["properties"]["status"]["enum"] == model_schema["properties"]["status"]["enum"]
    assert report_scenario["properties"]["status"]["enum"] == model_scenario["properties"]["status"]["enum"]
    report_json = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(report_json)
    assert report_json["report_schema_version"] == "1.0.0"
    assert report_json["report_paths"] == {"html": "report.html", "pdf": "report.pdf", "json": "report.json"}


def test_verified_failure_report_contains_expected_actual_reproduction_and_remediation(tmp_path: Path):
    from pypdf import PdfReader

    run = RunRecord(target_url="http://localhost:8000", endpoint_path="/api/chat", method="POST",
        profile="quick", application_name="Isolated support demo",
        request_payload={"api_key": "[REDACTED]", "message": "refund policy"})
    run.scenarios = [
        ScenarioResult(scenario_id="http", name="Baseline invocation", category="HTTP reliability", status="PASSED"),
        ScenarioResult(scenario_id="contract", name="Response contract", category="Response validity", status="PASSED"),
        ScenarioResult(scenario_id="latency", name="Latency threshold", category="Latency", status="PASSED"),
        ScenarioResult(scenario_id="instrumented-tool-1", name="Refund lost acknowledgment",
            category="Tool reliability", status="VERIFIED_FAILURE", severity="critical", http_status=200,
            evidence={"faults": [{"tool": "refund_commit", "kind": "lost_acknowledgment"}],
                "fault_injected": True, "cleanup_succeeded": True, "side_effect_count": 2,
                "response_excerpt": "Refund completed with side-effect count 2.",
                "expected_substring_passed": False}),
    ]
    run.scoring = calculate(run)
    assert run.scoring["overall_score"] == 67
    assert run.scoring["critical_findings"] == 1

    paths = generate(run, tmp_path)
    html_report = Path(paths["html"]).read_text(encoding="utf-8")
    json_report = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    from jsonschema import Draft202012Validator, FormatChecker
    schema_path = Path(__file__).resolve().parents[1] / "docs" / "report-schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(json_report)
    assert "Expected behavior" in html_report
    assert "Actual observation" in html_report
    assert "Ordered fault sequence" in html_report
    assert "idempotency protection" in html_report
    assert "side-effect count 2" in html_report
    assert "[REDACTED]" in html_report
    assert 'api_key&quot;: &quot;[REDACTED]' in html_report
    assert json_report["scenarios"][3]["evidence"]["fault_injected"] is True
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(paths["pdf"]).pages)
    normalized_pdf_text = " ".join(pdf_text.split())
    assert "67/100" in normalized_pdf_text
    assert "Refund lost acknowledgment" in normalized_pdf_text
    assert "side-effect count 2" in normalized_pdf_text
    assert "Ordered fault sequence" in normalized_pdf_text
    assert "[REDACTED]" in normalized_pdf_text


@pytest.mark.asyncio
async def test_api_root_points_to_bundled_dashboard_and_health_routes():
    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.get("/")
        assert response.status_code == 200
        assert response.json()["dashboard"] == "http://127.0.0.1:3000"
        assert response.json()["api_docs"] == "/docs"
        health = await client.get("/api/health")
        assert health.json()["product"] == "BreakMyBot"


@pytest.mark.asyncio
async def test_target_preflight_invokes_one_baseline_and_reports_observed_response(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BREAKMYBOT_DOCKER_MODE", "true")
    monkeypatch.setenv("PREFLIGHT_TOKEN", "preflight-secret")
    observed = {}

    async def fake_preflight(url, path, method, payload, *, docker_mode=False, headers=None):
        observed.update(url=url, path=path, method=method, payload=payload,
            docker_mode=docker_mode, headers=headers)
        return {"reachable": True, "status_code": 200, "successful": True,
            "duration_ms": 12.3, "content_type": "application/json", "response_excerpt": '{"ok":true}'}

    monkeypatch.setattr("breakmybot.api.preflight", fake_preflight)
    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post("/api/targets/preflight", json={"target_url": "http://localhost:8000",
            "endpoint_path": "/api/chat", "method": "POST", "payload": {"input": "hello"},
            "profile": "quick", "auth_env": "PREFLIGHT_TOKEN"})
    assert response.status_code == 200
    assert response.json()["status_code"] == 200
    assert response.json()["duration_ms"] == 12.3
    assert observed == {"url": "http://localhost:8000/", "path": "/api/chat", "method": "POST",
        "payload": {"input": "hello"}, "docker_mode": True,
        "headers": {"Authorization": "Bearer preflight-secret"}}


@pytest.mark.asyncio
async def test_target_preflight_fails_closed_when_auth_environment_variable_missing(monkeypatch):
    monkeypatch.delenv("MISSING_TARGET_TOKEN", raising=False)
    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post("/api/targets/preflight", json={"target_url": "http://localhost:8000",
            "endpoint_path": "/api/chat", "method": "POST", "profile": "quick",
            "auth_env": "MISSING_TARGET_TOKEN"})
    assert response.status_code == 422
    assert "credential environment variable is missing" in response.json()["detail"]


@pytest.mark.asyncio
async def test_standalone_demo_discovery_to_worker_report_acceptance(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from breakmybot import target, worker
    from breakmybot.api import app as product_api
    demo_path = Path(__file__).resolve().parents[1] / "examples" / "demo-agent" / "app.py"
    spec = importlib.util.spec_from_file_location("acceptance_demo_agent", demo_path)
    assert spec and spec.loader
    demo_agent = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = demo_agent
    spec.loader.exec_module(demo_agent)

    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BREAKMYBOT_DOCKER_MODE", "false")
    original_client = httpx.AsyncClient

    def demo_client(*args, **kwargs):
        return original_client(*args, **kwargs, transport=ASGITransport(app=demo_agent.app))

    monkeypatch.setattr(target.httpx, "AsyncClient", demo_client)
    async with AsyncClient(transport=ASGITransport(app=product_api), base_url="http://breakmybot") as client:
        discovery = await client.post("/api/targets/discover", json={"url": "http://127.0.0.1:8001"})
        assert discovery.status_code == 200
        found = discovery.json()
        assert found["reachable"] is True and found["openapi_found"] is True
        candidate = next(item for item in found["endpoints"] if item["path"] == "/api/chat")
        assert candidate["payload_template"] == {"message": "Hello. Please reply briefly."}

        preflight = await client.post("/api/targets/preflight", json={"target_url": "http://127.0.0.1:8001",
            "endpoint_path": candidate["path"], "method": candidate["method"],
            "payload": {"message": "refund policy"}, "profile": "quick"})
        assert preflight.status_code == 200
        assert preflight.json()["successful"] is True
        assert preflight.json()["status_code"] == 200

        accepted = await client.post("/api/runs", json={"target_url": "http://127.0.0.1:8001",
            "endpoint_path": candidate["path"], "method": candidate["method"],
            "payload": {"message": "refund policy"}, "profile": "quick",
            "expected_substring": "14 days"})
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        assert await worker.process_one_job() is True
        result = await client.get(f"/api/runs/{run_id}")
        assert result.status_code == 200
        run = result.json()
        assert run["status"] == "COMPLETED"
        baseline = next(item for item in run["scenarios"] if item["scenario_id"] == "baseline")
        assert baseline["status"] == "PASSED"
        assert "14 days" in baseline["evidence"]["response_excerpt"]
        scenarios = await client.get(f"/api/runs/{run_id}/scenarios", params={"offset": 0, "limit": 2})
        assert scenarios.status_code == 200 and scenarios.json()["total"] == len(run["scenarios"])
        assert len(scenarios.json()["items"]) == 2
        findings = await client.get(f"/api/runs/{run_id}/findings")
        assert findings.status_code == 200 and findings.json()["total"] == 0
        score = await client.get(f"/api/runs/{run_id}/score")
        assert score.status_code == 200 and score.json()["run_id"] == run_id
        assert score.json()["scenario_counts"] == run["scoring"]["scenario_counts"]
        html = await client.get(f"/api/runs/{run_id}/report.html")
        pdf = await client.get(f"/api/runs/{run_id}/report.pdf")
        report_json = await client.get(f"/api/runs/{run_id}/report.json")
        assert html.status_code == 200 and run_id in html.text and "AI RELIABILITY ASSESSMENT" in html.text
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        assert report_json.status_code == 200 and report_json.json()["id"] == run_id
        assert "14 days" in report_json.json()["scenarios"][0]["evidence"]["response_excerpt"]


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
    assert result.scoring["overall_score"] is None
    assert result.scoring["observed_weight_percent"] == 45
    assert result.scoring["category_scores"]["Output correctness"]["score"] == 100
    assert persisted is not None and len(persisted.scenarios) == 15
    assert "test-secret" not in str(persisted.scenarios)
    assert all(Path(path).is_file() for path in persisted.report_paths.values())
    events = [event for _, event in store.events_after(run.id, 0)]
    assert any(event["type"] == "run.phase" and event["phase"] == "BASELINE" for event in events)
    completions = [event for event in events if event["type"] == "scenario.completed"]
    assert len(completions) == 12
    assert completions[0]["evidence"]["response_excerpt"] == '{"answer":"ok","api_key":"[REDACTED]"}'
    assert all("test-secret" not in str(event) for event in events)
    assert all("expected_substring" not in event["evidence"] for event in completions)


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


@pytest.mark.asyncio
async def test_memoryscope_bridge_imports_only_contract_backed_external_case_results(monkeypatch):
    manifest = {"contract": "memoryscope-answer-evaluation-v1", "capabilities": {
        "external_agent_answer_evaluation": True}}
    run = {"id": "memory-run-1", "status": "completed", "results": [{
        "name": "refund", "status": "pass", "answer": "Refunds within 14 days. Authorization: Bearer target.secret.value",
        "checks": [{"check": "required_literal", "passed": True}],
        "judge": {"verdict": "pass", "answer_quote": "14 days", "reference_quote": "14 days",
            "api_key": "judge-secret-value"},
        "answer_latency_ms": 12.5}]}

    def respond(request):
        if request.url.path == "/api/beta/capabilities":
            return httpx.Response(200, json=manifest)
        if request.url.path == "/api/beta/settings":
            return httpx.Response(200, json={"judge_configured": True})
        if request.method == "POST" and request.url.path == "/api/beta/runs":
            payload = json.loads(request.content)
            assert payload["send_to_judge"] is True
            assert payload["endpoint"] == "http://127.0.0.1:8001/api/chat/sse"
            return httpx.Response(202, json={"id": run["id"]})
        if request.url.path == "/api/beta/runs/memory-run-1":
            return httpx.Response(200, json=run)
        raise AssertionError(f"Unexpected MemoryScope request: {request.method} {request.url}")

    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(respond))

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("breakmybot.memoryscope.httpx.AsyncClient", client_factory)
    monkeypatch.setattr("breakmybot.memoryscope.asyncio.sleep", no_wait)
    monkeypatch.setenv("MEMORYSCOPE_API_URL", "http://127.0.0.1:8002")
    monkeypatch.setenv("NROUTER_API_KEY", "judge-secret-value")
    request = RunRequest(target_url="http://127.0.0.1:8001", endpoint_path="/api/chat", method="POST",
        payload={"message": "hello"}, application_name="support")
    config = MemoryAssessment(agent_path="/api/chat/sse", cases=[MemoryCase(name="refund",
        question="Refund window?", reference="Refunds within 14 days.", expected="14 days"),
        ], send_to_nrouter=True)

    results = await evaluate_memory(request, config)

    assert len(results) == 1
    assert results[0].status == "PASSED"
    assert results[0].evidence["judge"]["verdict"] == "pass"
    assert results[0].evidence["answer"].endswith("Authorization: Bearer [REDACTED]")
    assert results[0].evidence["judge"]["api_key"] == "[REDACTED]"


def test_memoryscope_contract_accepts_only_advertised_answer_evaluation():
    assert supports_external_answer_evaluation({"contract": "memoryscope-answer-evaluation-v1",
        "capabilities": {"external_agent_answer_evaluation": True}})
    assert not supports_external_answer_evaluation({"contract": "memoryscope-answer-evaluation-v1",
        "capabilities": {"persistent_memory_lifecycle": True}})
    assert not supports_external_answer_evaluation({"contract": "unknown", "capabilities": {
        "external_agent_answer_evaluation": True}})


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
