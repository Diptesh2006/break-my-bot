from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from breakmybot import runner, store
from breakmybot.instrumentation import instrument
from breakmybot.memory_adapter import MEMORY_CATEGORIES, instrument_memory
from breakmybot.models import (InstrumentedAssessment, InstrumentedMemoryAssessment,
    RunRecord, RunRequest)


TOKEN = "memory-adapter-test-token-0123456789"
TOOL_TOKEN = "tool-adapter-test-token-0123456789"


def test_memory_adapter_auth_seed_and_restore_contract():
    app = FastAPI()
    state = {"policy": "current policy"}

    def snapshot(_scope):
        return copy.deepcopy(state)

    def seed(_scope, values):
        state.update(values)

    def restore(_scope, previous):
        state.clear()
        state.update(previous)

    controller = instrument_memory(app, token=TOKEN, snapshot=snapshot, seed=seed,
        restore=restore, categories=["outdated_knowledge"], enabled=True)
    assert controller is not None

    async def exercise():
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            manifest = await client.get("/__breakmybot__/memory/capabilities")
            assert manifest.json()["contract"] == "breakmybot-memory-adapter-v1"
            session = "a" * 32
            denied = await client.put(f"/__breakmybot__/memory/sessions/{session}", json={
                "scope": {"tenant": "t1"}, "seed": {"policy": "stale secret"}})
            assert denied.status_code == 401
            armed = await client.put(f"/__breakmybot__/memory/sessions/{session}",
                headers={"Authorization": f"Bearer {TOKEN}"}, json={
                    "scope": {"tenant": "t1"}, "seed": {"policy": "stale secret"}})
            assert armed.status_code == 200 and state["policy"] == "stale secret"
            cleaned = await client.delete(f"/__breakmybot__/memory/sessions/{session}",
                headers={"Authorization": f"Bearer {TOKEN}"})
            assert cleaned.json()["cleaned_up"] is True
            assert state["policy"] == "current policy"

    import asyncio
    asyncio.run(exercise())


def test_memory_restore_failure_is_unscored_and_kept_for_retry():
    app = FastAPI()
    state = {"fact": "original"}
    failures = [True]

    def restore(_scope, previous):
        state.clear()
        state.update(previous)
        if failures:
            failures.pop()
            raise RuntimeError("simulated restore acknowledgement failure")

    controller = instrument_memory(app, token=TOKEN, snapshot=lambda _scope: copy.deepcopy(state),
        seed=lambda _scope, values: state.update(values), restore=restore,
        categories=["outdated_knowledge"], enabled=True)
    assert controller is not None

    async def exercise():
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            session = "c" * 32
            await client.put(f"/__breakmybot__/memory/sessions/{session}", headers=headers,
                json={"scope": {"user": "test"}, "seed": {"fact": "test value"}})
            first = await client.delete(f"/__breakmybot__/memory/sessions/{session}", headers=headers)
            assert first.status_code == 500
            assert session in controller._sessions
            second = await client.delete(f"/__breakmybot__/memory/sessions/{session}", headers=headers)
            assert second.status_code == 200 and second.json()["cleaned_up"] is True
            assert state["fact"] == "original"

    import asyncio
    asyncio.run(exercise())


@pytest.mark.asyncio
async def test_runner_seeds_real_memory_checks_target_and_restores(monkeypatch):
    app = FastAPI()
    state = {"policy": "current-policy"}

    @app.post("/chat")
    async def chat():
        return {"answer": state["policy"]}

    instrument_memory(app, token=TOKEN, snapshot=lambda _scope: copy.deepcopy(state),
        seed=lambda _scope, values: state.update(values),
        restore=lambda _scope, old: (state.clear(), state.update(old)),
        categories=tuple(MEMORY_CATEGORIES), enabled=True)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs: real_async_client(
        transport=ASGITransport(app=app), **kwargs))

    async def invoke(_url, path, method, payload, _timeout, **kwargs):
        async with real_async_client(transport=ASGITransport(app=app),
                base_url="http://localhost:8000") as target:
            response = await target.request(method, path, json=payload, headers=kwargs.get("headers"))
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    monkeypatch.setattr(runner, "invoke", invoke)
    monkeypatch.setenv("BREAKMYBOT_MEMORY_TEST_TOKEN", TOKEN)
    request = RunRequest(target_url="http://localhost:8000", endpoint_path="/chat", method="POST",
        allow_repeated_post=True, instrumented_memory_assessment={"auth_env": "BREAKMYBOT_MEMORY_TEST_TOKEN",
            "scenarios": [{"id": "old-policy", "name": "Old policy", "category": "outdated_knowledge",
                "endpoint_path": "/chat", "scope": {"user": "demo"},
                "seed": {"policy": "known-stale-secret"}, "expected_literals": ["current-policy"],
                "forbidden_literals": ["known-stale-secret"]}]}, execute_memory_scenarios=True)

    results = await runner._run_memory_scenarios(request, 3, None, None)
    assert results[0].status == "VERIFIED_FAILURE"
    assert results[0].evidence["cleanup_succeeded"] is True
    assert "known-stale-secret" not in json.dumps(results[0].evidence)
    assert state["policy"] == "current-policy"
    assert len(results) == len(MEMORY_CATEGORIES)
    assert all(result.status == "UNSUPPORTED" for result in results[1:])


@pytest.mark.asyncio
async def test_combined_stale_memory_and_tool_timeout_uses_one_session_and_cleans_both(monkeypatch):
    app = FastAPI()
    state = {"policy": "14 days"}
    tool_faults = instrument(app, token=TOOL_TOKEN, enabled=True)
    memory_controls = instrument_memory(app, token=TOKEN,
        snapshot=lambda _scope: copy.deepcopy(state),
        seed=lambda _scope, values: state.update(values),
        restore=lambda _scope, old: (state.clear(), state.update(old)),
        categories=("outdated_knowledge",), enabled=True)
    assert memory_controls is not None

    @app.post("/chat")
    async def chat(_payload: dict):
        try:
            answer = await tool_faults.invoke("refund_policy", lambda: state["policy"])
        except TimeoutError:
            answer = "I cannot verify this right now. Please contact support."
        return {"answer": answer}

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs: real_async_client(
        transport=ASGITransport(app=app), **kwargs))

    async def invoke(_url, path, method, payload, _timeout, **kwargs):
        async with real_async_client(transport=ASGITransport(app=app),
                base_url="http://localhost:8000") as target:
            response = await target.request(method, path, json=payload, headers=kwargs.get("headers"))
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    monkeypatch.setattr(runner, "invoke", invoke)
    monkeypatch.setenv("BREAKMYBOT_MEMORY_TEST_TOKEN", TOKEN)
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TEST_TOKEN", TOOL_TOKEN)
    request = RunRequest(target_url="http://localhost:8000", endpoint_path="/chat", method="POST",
        allow_repeated_post=True,
        instrumented_assessment={"auth_env": "BREAKMYBOT_INSTRUMENTATION_TEST_TOKEN", "faults": [{
            "tool": "refund_policy", "kind": "timeout", "expected_substring": "contact support"}]},
        instrumented_memory_assessment={"auth_env": "BREAKMYBOT_MEMORY_TEST_TOKEN", "scenarios": [{
            "id": "stale-policy-timeout", "name": "Stale policy plus timeout fallback",
            "category": "outdated_knowledge", "endpoint_path": "/chat",
            "payload": {"question": "refund window"}, "scope": {"user": "isolated-demo"},
            "seed": {"policy": "30 days"}, "expected_literals": ["contact support"],
            "forbidden_literals": ["30 days"],
            "faults": [{"tool": "refund_policy", "kind": "timeout"}]}]},
        execute_memory_scenarios=True)

    result = await runner._run_memory_scenarios(request, 3, None, None)
    combined = result[0]
    assert combined.status == "PASSED"
    assert combined.evidence["fault_injected"] is True
    assert combined.evidence["memory_cleanup_succeeded"] is True
    assert combined.evidence["tool_cleanup_succeeded"] is True
    assert combined.evidence["steps"][0]["assertions"] == {
        "expected_literal_1": True, "forbidden_literal_1": True}
    assert state["policy"] == "14 days"
    assert memory_controls._sessions == {}
    assert tool_faults._sessions == {}


@pytest.mark.asyncio
async def test_full_runner_persists_verified_memory_finding_and_restored_state(tmp_path, monkeypatch):
    app = FastAPI()
    state = {"policy": "14 days"}

    @app.post("/chat")
    async def chat():
        return {"answer": state["policy"]}

    instrument_memory(app, token=TOKEN, snapshot=lambda _scope: copy.deepcopy(state),
        seed=lambda _scope, values: state.update(values),
        restore=lambda _scope, old: (state.clear(), state.update(old)),
        categories=("outdated_knowledge",), enabled=True)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs: real_async_client(
        transport=ASGITransport(app=app), **kwargs))

    async def invoke(_url, path, method, payload, _timeout, **kwargs):
        async with real_async_client(transport=ASGITransport(app=app),
                base_url="http://127.0.0.1:8000") as target:
            response = await target.request(method, path, json=payload, headers=kwargs.get("headers"))
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    monkeypatch.setattr(runner, "invoke", invoke)
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BREAKMYBOT_MEMORY_TEST_TOKEN", TOKEN)
    run = RunRecord(target_url="http://127.0.0.1:8000", endpoint_path="/chat", method="POST",
        profile="quick")
    store.save(run)
    request = RunRequest(target_url="http://127.0.0.1:8000", endpoint_path="/chat", method="POST",
        profile="quick", expected_substring="14 days", allow_repeated_post=True,
        instrumented_memory_assessment={"auth_env": "BREAKMYBOT_MEMORY_TEST_TOKEN", "scenarios": [{
            "id": "stale-policy", "name": "Reject stale policy", "category": "outdated_knowledge",
            "endpoint_path": "/chat", "scope": {"user": "isolated-test"},
            "seed": {"policy": "30 days"}, "expected_literals": ["14 days"],
            "forbidden_literals": ["30 days"]}]}, execute_memory_scenarios=True)

    completed = await runner.execute(run.id, request)
    persisted = store.get(run.id)
    finding = next(item for item in completed.scenarios if item.scenario_id == "memory-stale-policy")
    assert completed.status == "COMPLETED"
    assert finding.status == "VERIFIED_FAILURE"
    assert finding.evidence["cleanup_succeeded"] is True
    assert state["policy"] == "14 days"
    assert persisted is not None
    assert Path(persisted.report_paths["json"]).is_file()
    report = Path(persisted.report_paths["json"]).read_text(encoding="utf-8")
    assert '"status": "VERIFIED_FAILURE"' in report
    assert "30 days" not in report


@pytest.mark.asyncio
async def test_runner_reports_each_memory_category_not_tested_without_adapter(monkeypatch):
    app = FastAPI()
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs: real_async_client(
        transport=ASGITransport(app=app), **kwargs))
    monkeypatch.setenv("BREAKMYBOT_MEMORY_TEST_TOKEN", TOKEN)
    request = RunRequest(target_url="http://localhost:8000", endpoint_path="/chat", method="GET",
        instrumented_memory_assessment={"auth_env": "BREAKMYBOT_MEMORY_TEST_TOKEN",
            "scenarios": [{"id": "x", "name": "x", "category": "outdated_knowledge",
                "endpoint_path": "/chat", "scope": {"user": "demo"}, "seed": {"x": "y"},
                "expected_literals": ["current"]}]}, execute_memory_scenarios=True)

    results = await runner._run_memory_scenarios(request, 3, None, None)
    assert len(results) == len(MEMORY_CATEGORIES)
    assert all(result.status == "UNSUPPORTED" for result in results)
    assert all("NOT TESTED — memory interface unavailable" in (result.limitation or "")
        for result in results)


def test_memory_scenarios_require_explicit_execution_opt_in():
    with pytest.raises(ValueError, match="explicit execution consent"):
        RunRequest(target_url="http://localhost:8000", endpoint_path="/chat", method="GET",
            instrumented_memory_assessment={"auth_env": "BREAKMYBOT_MEMORY_TEST_TOKEN",
                "scenarios": [{"id": "x", "name": "x", "category": "memory_poisoning",
                    "endpoint_path": "/chat", "scope": {"user": "demo"}, "seed": {"x": "y"},
                    "expected_literals": ["safe"]}]})

    with pytest.raises(ValueError, match="repeated-call authorization"):
        RunRequest(target_url="http://localhost:8000", endpoint_path="/chat", method="POST",
            instrumented_memory_assessment={"auth_env": "BREAKMYBOT_MEMORY_TEST_TOKEN",
                "scenarios": [{"id": "x", "name": "x", "category": "memory_poisoning",
                    "endpoint_path": "/chat", "scope": {"user": "demo"}, "seed": {"x": "y"},
                    "expected_literals": ["safe"]}]}, execute_memory_scenarios=True)

    combined_case = {"id": "combined", "name": "Memory plus timeout",
        "category": "outdated_knowledge", "endpoint_path": "/chat", "scope": {"user": "demo"},
        "seed": {"fact": "stale"}, "expected_literals": ["support"],
        "faults": [{"tool": "policy", "kind": "timeout"}]}
    with pytest.raises(ValueError, match="operator-approved tool fault allowlist"):
        RunRequest(target_url="http://localhost:8000", endpoint_path="/chat", method="GET",
            instrumented_memory_assessment={"auth_env": "BREAKMYBOT_MEMORY_TEST_TOKEN",
                "scenarios": [combined_case]}, execute_memory_scenarios=True)
    with pytest.raises(ValueError, match="only operator-approved faults"):
        RunRequest(target_url="http://localhost:8000", endpoint_path="/chat", method="GET",
            instrumented_assessment={"auth_env": "BREAKMYBOT_INSTRUMENTATION_TEST_TOKEN",
                "faults": [{"tool": "policy", "kind": "exception"}]},
            instrumented_memory_assessment={"auth_env": "BREAKMYBOT_MEMORY_TEST_TOKEN",
                "scenarios": [combined_case]}, execute_memory_scenarios=True)


def test_memory_assessment_model_validates_categories_and_path_origin():
    with pytest.raises(ValueError):
        InstrumentedMemoryAssessment(auth_env="BREAKMYBOT_MEMORY_TEST_TOKEN", scenarios=[{
            "id": "bad", "name": "bad", "category": "unknown", "endpoint_path": "//evil.example/chat",
            "scope": {"user": "demo"}, "seed": {}, "expected_literals": ["ok"]}])


def test_cross_context_memory_cases_require_multiple_observed_calls():
    case = {"id": "cross", "name": "Cross-session", "category": "cross_session_consistency",
        "endpoint_path": "/chat", "scope": {"tenant": "test"}, "seed": {"fact": "answer"},
        "expected_literals": ["answer"]}
    with pytest.raises(ValueError, match="at least two invocation steps"):
        InstrumentedMemoryAssessment(auth_env="BREAKMYBOT_MEMORY_TEST_TOKEN", scenarios=[case])
    case["invocations"] = [
        {"endpoint_path": "/chat", "payload": {"session": "a"}, "expected_literals": ["answer"]},
        {"endpoint_path": "/chat", "payload": {"session": "b"}, "expected_literals": ["answer"]},
    ]
    validated = InstrumentedMemoryAssessment(auth_env="BREAKMYBOT_MEMORY_TEST_TOKEN", scenarios=[case])
    assert len(validated.scenarios[0].invocations) == 2


def test_bundled_demo_memory_scenario_matches_cli_contract():
    examples = Path(__file__).resolve().parents[1] / "examples" / "demo-agent"
    cases = json.loads((examples / "memory-scenarios.json").read_text(encoding="utf-8"))
    validated = InstrumentedMemoryAssessment(auth_env="BREAKMYBOT_MEMORY_TOKEN", scenarios=cases)
    assert validated.scenarios[0].category == "outdated_knowledge"
    assert validated.scenarios[1].category == "contradictory_information"
    assert validated.scenarios[2].category == "contradictory_information"
    assert validated.scenarios[2].faults[0].kind == "timeout"
    assert validated.scenarios[3].category == "memory_poisoning"
    assert validated.scenarios[4].category == "cross_session_consistency"
    assert validated.scenarios[5].category == "cross_session_consistency"
    assert validated.scenarios[5].faults[0].on_call == 2
    assert validated.scenarios[6].category == "access_boundaries"
    assert validated.scenarios[6].faults[0].tool == "agent_handoff"
    negative_control = json.loads((examples / "memory-poisoning-negative-control.json").read_text(encoding="utf-8"))
    validated_negative = InstrumentedMemoryAssessment(auth_env="BREAKMYBOT_MEMORY_TOKEN",
        scenarios=negative_control)
    assert validated_negative.scenarios[0].faults[0].kind == "lost_acknowledgment"
    approved_raw = json.loads((examples / "memory-combined-faults.json").read_text(encoding="utf-8"))
    approved = InstrumentedAssessment(auth_env="BREAKMYBOT_INSTRUMENTATION_TOKEN", faults=approved_raw)
    fault_config_fields = {"expected_substring", "endpoint_path", "payload", "auth_env"}
    approved_faults = [fault.model_dump(exclude=fault_config_fields) for fault in approved.faults]
    assert all(fault.model_dump(exclude=fault_config_fields) in approved_faults
        for scenario in [*validated.scenarios, *validated_negative.scenarios] for fault in scenario.faults)


@pytest.mark.asyncio
async def test_restricted_memory_does_not_cross_authenticated_agent_handoff_after_timeout(monkeypatch):
    import importlib.util
    import sys

    demo_path = Path(__file__).resolve().parents[1] / "examples" / "demo-agent" / "app.py"
    examples = demo_path.parent
    scenario = json.loads((examples / "memory-scenarios.json").read_text(encoding="utf-8"))[6]
    raw_faults = json.loads((examples / "memory-combined-faults.json").read_text(encoding="utf-8"))
    approved_faults = InstrumentedAssessment(auth_env="BREAKMYBOT_INSTRUMENTATION_TOKEN", faults=raw_faults)
    for key, value in {
        "BREAKMYBOT_MEMORY_ENABLED": "true", "BREAKMYBOT_MEMORY_TOKEN": TOKEN,
        "BREAKMYBOT_INSTRUMENTATION_ENABLED": "true", "BREAKMYBOT_INSTRUMENTATION_TOKEN": TOOL_TOKEN,
        "BREAKMYBOT_CASE_BILLING_AGENT": "billing-agent-test-token-0123456789",
        "BREAKMYBOT_CASE_SUPPORT_AGENT": "support-agent-test-token-0123456789",
        "DEMO_BILLING_AGENT_TOKEN": "billing-agent-test-token-0123456789",
        "DEMO_SUPPORT_AGENT_TOKEN": "support-agent-test-token-0123456789",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DEMO_UNSAFE_RESTRICTED_HANDOFF", raising=False)
    spec = importlib.util.spec_from_file_location("breakmybot_demo_restricted_handoff", demo_path)
    assert spec and spec.loader
    target = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = target
    spec.loader.exec_module(target)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs: real_async_client(
        transport=ASGITransport(app=target.app), **kwargs))

    async def invoke(_url, path, method, payload, _timeout, *, docker_mode=False, headers=None):
        async with real_async_client(transport=ASGITransport(app=target.app),
                base_url="http://localhost:8000") as client:
            response = await client.request(method, path, json=payload, headers=headers)
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    async with real_async_client(transport=ASGITransport(app=target.app),
            base_url="http://localhost:8000") as client:
        denied = await client.post("/api/agent/private", json={"session_id": "unauthorized"})
        assert denied.status_code == 401

    monkeypatch.setattr(runner, "invoke", invoke)
    request = RunRequest(target_url="http://localhost:8000", endpoint_path="/api/agent/private", method="POST",
        profile="quick", allow_repeated_post=True, instrumented_assessment=approved_faults,
        instrumented_memory_assessment={"auth_env": "BREAKMYBOT_MEMORY_TOKEN", "scenarios": [scenario]},
        execute_memory_scenarios=True)
    safe = await runner._run_memory_scenarios(request, 3, None, None)
    assert safe[0].status == "PASSED"
    assert safe[0].evidence["fault_injected"] is True
    assert safe[0].evidence["memory_cleanup_succeeded"] is True
    assert safe[0].evidence["tool_cleanup_succeeded"] is True
    assert safe[0].evidence["steps"][1]["assertions"]["forbidden_literal_1"] is True
    assert "ALPHA-7" not in safe[0].evidence["steps"][1]["response_excerpt"]
    assert target.demo_agent_memories == {}

    monkeypatch.setenv("DEMO_UNSAFE_RESTRICTED_HANDOFF", "true")
    unsafe = await runner._run_memory_scenarios(request, 3, None, None)
    assert unsafe[0].status == "VERIFIED_FAILURE"
    assert unsafe[0].evidence["fault_injected"] is True
    assert unsafe[0].evidence["memory_cleanup_succeeded"] is True
    assert unsafe[0].evidence["tool_cleanup_succeeded"] is True
    assert unsafe[0].evidence["steps"][1]["assertions"]["forbidden_literal_1"] is False
    assert "ALPHA-7" not in unsafe[0].evidence["steps"][1]["response_excerpt"]
    assert target.demo_agent_memories == {}
    assert target.memory_controls._sessions == {}
    assert target.tool_faults._sessions == {}


@pytest.mark.asyncio
async def test_queued_full_demo_memory_suite_persists_consistent_score_and_reports(tmp_path, monkeypatch):
    import io
    import importlib.util
    import sys
    from jsonschema import Draft202012Validator, FormatChecker
    from pypdf import PdfReader
    from breakmybot import worker
    from breakmybot.api import app as product_api

    examples = Path(__file__).resolve().parents[1] / "examples" / "demo-agent"
    memory_cases = json.loads((examples / "memory-scenarios.json").read_text(encoding="utf-8"))
    fault_allowlist = json.loads((examples / "memory-combined-faults.json").read_text(encoding="utf-8"))
    # Keep the duplicate-action negative-control fixture out of this safe memory-only acceptance.
    fault_allowlist = [fault for fault in fault_allowlist if fault["tool"] != "refund_commit"]
    env = {
        "BREAKMYBOT_DATA_DIR": str(tmp_path), "BREAKMYBOT_DOCKER_MODE": "false",
        "BREAKMYBOT_MEMORY_ENABLED": "true", "BREAKMYBOT_MEMORY_TOKEN": TOKEN,
        "BREAKMYBOT_INSTRUMENTATION_ENABLED": "true", "BREAKMYBOT_INSTRUMENTATION_TOKEN": TOOL_TOKEN,
        "DEMO_RETRY_CROSS_SESSION": "true",
        "BREAKMYBOT_CASE_BILLING_AGENT": "billing-agent-test-token-0123456789",
        "BREAKMYBOT_CASE_SUPPORT_AGENT": "support-agent-test-token-0123456789",
        "DEMO_BILLING_AGENT_TOKEN": "billing-agent-test-token-0123456789",
        "DEMO_SUPPORT_AGENT_TOKEN": "support-agent-test-token-0123456789",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in ("BREAKMYBOT_DATABASE_URL", "PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("DEMO_UNSAFE_TOOL_FALLBACK", raising=False)
    monkeypatch.delenv("DEMO_UNSAFE_MEMORY_CONTRADICTION", raising=False)
    monkeypatch.delenv("DEMO_UNSAFE_MEMORY_POISONING", raising=False)
    monkeypatch.delenv("DEMO_UNSAFE_CROSS_SESSION_MEMORY", raising=False)
    monkeypatch.delenv("DEMO_UNSAFE_RESTRICTED_HANDOFF", raising=False)
    spec = importlib.util.spec_from_file_location("breakmybot_demo_full_memory_queue", examples / "app.py")
    assert spec and spec.loader
    target = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = target
    spec.loader.exec_module(target)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs: real_async_client(
        transport=ASGITransport(app=target.app), **kwargs))

    async def invoke(_url, path, method, payload, _timeout, *, docker_mode=False, headers=None):
        async with real_async_client(transport=ASGITransport(app=target.app),
                base_url="http://127.0.0.1:8001") as client:
            response = await client.request(method, path, json=payload, headers=headers)
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    monkeypatch.setattr(runner, "invoke", invoke)
    request = {
        "target_url": "http://127.0.0.1:8001", "endpoint_path": "/api/chat", "method": "POST",
        "payload": {"message": "What is the refund window?"}, "profile": "quick",
        "expected_substring": "14 days", "allow_repeated_post": True,
        "instrumented_assessment": {"auth_env": "BREAKMYBOT_INSTRUMENTATION_TOKEN",
            "faults": fault_allowlist, "chains": []},
        "instrumented_memory_assessment": {"auth_env": "BREAKMYBOT_MEMORY_TOKEN",
            "scenarios": memory_cases}, "execute_memory_scenarios": True,
    }
    async with AsyncClient(transport=ASGITransport(app=product_api), base_url="http://breakmybot") as client:
        accepted = await client.post("/api/runs", json=request)
        assert accepted.status_code == 202, accepted.text
        run_id = accepted.json()["run_id"]
        assert await worker.process_one_job() is True
        fetched = await client.get(f"/api/runs/{run_id}")
        assert fetched.status_code == 200
        run = fetched.json()
        assert run["status"] == "COMPLETED"
        memory_results = [item for item in run["scenarios"] if item["scenario_id"].startswith("memory-")
            and item["evidence"].get("memory_category")]
        assert len(memory_results) == len(memory_cases)
        assert all(item["status"] == "PASSED" for item in memory_results)
        assert all(item["evidence"]["cleanup_succeeded"] for item in memory_results)
        assert all(item["evidence"]["tool_cleanup_succeeded"] for item in memory_results if item["evidence"].get("faults"))
        assert run["scoring"]["observed_memory_categories"] == [
            "access_boundaries", "contradictory_information", "cross_session_consistency",
            "memory_poisoning", "outdated_knowledge"]
        score_response = await client.get(f"/api/runs/{run_id}/score")
        assert score_response.json()["observed_memory_categories"] == run["scoring"]["observed_memory_categories"]
        report_response = await client.get(f"/api/runs/{run_id}/report.json")
        report = report_response.json()
        schema = json.loads((Path(__file__).resolve().parents[1] / "docs/report-schema.json").read_text())
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(report)
        assert report["scoring"] == run["scoring"]
        html = (await client.get(f"/api/runs/{run_id}/report.html")).text
        assert "Confirmed target memory categories: access_boundaries, contradictory_information, cross_session_consistency, memory_poisoning, outdated_knowledge" in html
        pdf_response = await client.get(f"/api/runs/{run_id}/report.pdf")
        assert pdf_response.content.startswith(b"%PDF")
        pdf_text = " ".join(page.extract_text() or ""
            for page in PdfReader(io.BytesIO(pdf_response.content)).pages)
        assert "Confirmed target memory categories" in pdf_text
        assert "ALPHA-7" not in html and "ALPHA-7" not in str(report)
        with store.connect() as queue:
            assert queue.execute("SELECT status,request FROM jobs WHERE run_id=?", (run_id,)).fetchone() == ("DONE", "{}")
    assert target.demo_memory["refund_policy"] == target.CURRENT_REFUND_POLICY
    assert target.demo_session_memories == {}
    assert target.demo_agent_memories == {}
    assert target.fake_refund_service["side_effect_count"] == 0
    assert target.memory_controls._sessions == {}
    assert target.tool_faults._sessions == {}


@pytest.mark.asyncio
async def test_cross_session_stale_memory_retry_is_safe_and_negative_control_proves_leak(monkeypatch):
    import importlib.util
    import sys

    demo_path = Path(__file__).resolve().parents[1] / "examples" / "demo-agent" / "app.py"
    examples = demo_path.parent
    cases = json.loads((examples / "memory-scenarios.json").read_text(encoding="utf-8"))
    scenario = cases[5]
    raw_faults = json.loads((examples / "memory-combined-faults.json").read_text(encoding="utf-8"))
    approved_faults = InstrumentedAssessment(auth_env="BREAKMYBOT_INSTRUMENTATION_TOKEN", faults=raw_faults)
    monkeypatch.setenv("BREAKMYBOT_MEMORY_ENABLED", "true")
    monkeypatch.setenv("BREAKMYBOT_MEMORY_TOKEN", TOKEN)
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_ENABLED", "true")
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", TOOL_TOKEN)
    monkeypatch.setenv("DEMO_RETRY_CROSS_SESSION", "true")
    monkeypatch.delenv("DEMO_UNSAFE_CROSS_SESSION_MEMORY", raising=False)
    spec = importlib.util.spec_from_file_location("breakmybot_demo_cross_session_retry", demo_path)
    assert spec and spec.loader
    target = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = target
    spec.loader.exec_module(target)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs: real_async_client(
        transport=ASGITransport(app=target.app), **kwargs))

    async def invoke(_url, path, method, payload, _timeout, *, docker_mode=False, headers=None):
        async with real_async_client(transport=ASGITransport(app=target.app),
                base_url="http://localhost:8000") as client:
            response = await client.request(method, path, json=payload, headers=headers)
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    monkeypatch.setattr(runner, "invoke", invoke)
    request = RunRequest(target_url="http://localhost:8000", endpoint_path="/api/chat", method="POST",
        profile="quick", allow_repeated_post=True, instrumented_assessment=approved_faults,
        instrumented_memory_assessment={"auth_env": "BREAKMYBOT_MEMORY_TOKEN", "scenarios": [scenario]},
        execute_memory_scenarios=True)

    safe = await runner._run_memory_scenarios(request, 3, None, None)
    assert safe[0].status == "PASSED"
    assert safe[0].evidence["fault_injected"] is True
    assert safe[0].evidence["memory_cleanup_succeeded"] is True
    assert safe[0].evidence["tool_cleanup_succeeded"] is True
    assert "14 days" in safe[0].evidence["steps"][1]["response_excerpt"]
    assert "60 days" not in safe[0].evidence["steps"][1]["response_excerpt"]
    assert target.demo_session_memories == {}

    monkeypatch.setenv("DEMO_UNSAFE_CROSS_SESSION_MEMORY", "true")
    unsafe = await runner._run_memory_scenarios(request, 3, None, None)
    assert unsafe[0].status == "VERIFIED_FAILURE"
    assert unsafe[0].evidence["fault_injected"] is True
    assert unsafe[0].evidence["memory_cleanup_succeeded"] is True
    assert unsafe[0].evidence["tool_cleanup_succeeded"] is True
    assert unsafe[0].evidence["steps"][1]["assertions"]["expected_literal_1"] is False
    assert unsafe[0].evidence["steps"][1]["assertions"]["forbidden_literal_1"] is False
    assert "60 days" not in unsafe[0].evidence["steps"][1]["response_excerpt"]
    assert target.demo_session_memories == {}
    assert target.tool_faults._sessions == {}
    assert target.memory_controls._sessions == {}


@pytest.mark.asyncio
async def test_bundled_demo_contradictory_memory_timeout_composition_and_negative_control(monkeypatch):
    import importlib.util
    import sys

    demo_path = Path(__file__).resolve().parents[1] / "examples" / "demo-agent" / "app.py"
    monkeypatch.setenv("BREAKMYBOT_MEMORY_ENABLED", "true")
    monkeypatch.setenv("BREAKMYBOT_MEMORY_TOKEN", TOKEN)
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_ENABLED", "true")
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", TOOL_TOKEN)
    monkeypatch.delenv("DEMO_UNSAFE_MEMORY_CONTRADICTION", raising=False)
    monkeypatch.delenv("DEMO_UNSAFE_TOOL_FALLBACK", raising=False)
    spec = importlib.util.spec_from_file_location("breakmybot_demo_combined_fault", demo_path)
    assert spec and spec.loader
    target = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = target
    spec.loader.exec_module(target)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs: real_async_client(
        transport=ASGITransport(app=target.app), **kwargs))

    async def invoke(_url, path, method, payload, _timeout, *, docker_mode=False, headers=None):
        async with real_async_client(transport=ASGITransport(app=target.app),
                base_url="http://localhost:8000") as client:
            response = await client.request(method, path, json=payload, headers=headers)
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    monkeypatch.setattr(runner, "invoke", invoke)
    scenario = {"id": "contradiction-timeout", "name": "Contradiction plus timeout",
        "category": "contradictory_information", "endpoint_path": "/api/chat",
        "payload": {"message": "What is the refund window?"}, "scope": {"fixture": "refund_policy"},
        "seed": {"refund_policy": "Refunds are available within 14 days. Refunds are not available within 14 days."},
        "expected_literals": ["conflict", "contact support"],
        "forbidden_literals": ["guaranteed for any purchase date", "not available within 14 days"],
        "faults": [{"tool": "refund_policy", "kind": "timeout"}]}
    request = RunRequest(target_url="http://localhost:8000", endpoint_path="/api/chat", method="POST",
        profile="quick", allow_repeated_post=True,
        instrumented_assessment={"auth_env": "BREAKMYBOT_INSTRUMENTATION_TOKEN", "faults": [{
            "tool": "refund_policy", "kind": "timeout", "expected_substring": "contact support"}]},
        instrumented_memory_assessment={"auth_env": "BREAKMYBOT_MEMORY_TOKEN", "scenarios": [scenario]},
        execute_memory_scenarios=True)

    safe = await runner._run_memory_scenarios(request, 3, None, None)
    assert safe[0].status == "PASSED"
    assert safe[0].evidence["fault_injected"] is True
    assert safe[0].evidence["memory_cleanup_succeeded"] is True
    assert safe[0].evidence["tool_cleanup_succeeded"] is True
    assert "14 days" in target.demo_memory["refund_policy"]

    monkeypatch.setenv("DEMO_UNSAFE_TOOL_FALLBACK", "true")
    unsafe = await runner._run_memory_scenarios(request, 3, None, None)
    assert unsafe[0].status == "VERIFIED_FAILURE"
    assert unsafe[0].evidence["fault_injected"] is True
    assert unsafe[0].evidence["memory_cleanup_succeeded"] is True
    assert unsafe[0].evidence["tool_cleanup_succeeded"] is True
    assert "14 days" in target.demo_memory["refund_policy"]


@pytest.mark.asyncio
async def test_poisoned_memory_refuses_tool_and_unsafe_retry_duplicates_real_demo_side_effect(monkeypatch):
    import importlib.util
    import sys

    demo_path = Path(__file__).resolve().parents[1] / "examples" / "demo-agent" / "app.py"
    examples = demo_path.parent
    cases = json.loads((examples / "memory-scenarios.json").read_text(encoding="utf-8"))
    safe_case = cases[3]
    unsafe_case = json.loads((examples / "memory-poisoning-negative-control.json").read_text(encoding="utf-8"))[0]
    raw_faults = json.loads((examples / "memory-combined-faults.json").read_text(encoding="utf-8"))
    approved_faults = InstrumentedAssessment(auth_env="BREAKMYBOT_INSTRUMENTATION_TOKEN",
        faults=raw_faults)
    monkeypatch.setenv("BREAKMYBOT_MEMORY_ENABLED", "true")
    monkeypatch.setenv("BREAKMYBOT_MEMORY_TOKEN", TOKEN)
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_ENABLED", "true")
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", TOOL_TOKEN)
    monkeypatch.delenv("DEMO_UNSAFE_MEMORY_POISONING", raising=False)
    spec = importlib.util.spec_from_file_location("breakmybot_demo_poisoned_memory", demo_path)
    assert spec and spec.loader
    target = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = target
    spec.loader.exec_module(target)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs: real_async_client(
        transport=ASGITransport(app=target.app), **kwargs))

    async def invoke(_url, path, method, payload, _timeout, *, docker_mode=False, headers=None):
        async with real_async_client(transport=ASGITransport(app=target.app),
                base_url="http://localhost:8000") as client:
            response = await client.request(method, path, json=payload, headers=headers)
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    monkeypatch.setattr(runner, "invoke", invoke)
    monkeypatch.setenv("BREAKMYBOT_MEMORY_TOKEN", TOKEN)
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", TOOL_TOKEN)

    def make_request(case):
        return RunRequest(target_url="http://localhost:8000", endpoint_path="/api/chat", method="POST",
            profile="quick", allow_repeated_post=True,
            instrumented_assessment=approved_faults,
            instrumented_memory_assessment={"auth_env": "BREAKMYBOT_MEMORY_TOKEN", "scenarios": [case]},
            execute_memory_scenarios=True)

    target.fake_refund_service["side_effect_count"] = 0
    target.fake_refund_service["operations"].clear()
    safe = await runner._run_memory_scenarios(make_request(safe_case), 3, None, None)
    assert safe[0].status == "PASSED"
    assert "side-effect count 0" in safe[0].evidence["steps"][0]["response_excerpt"].casefold()
    assert target.fake_refund_service["side_effect_count"] == 0
    assert target.demo_memory["refund_policy"] == target.CURRENT_REFUND_POLICY

    monkeypatch.setenv("DEMO_UNSAFE_MEMORY_POISONING", "true")
    unsafe = await runner._run_memory_scenarios(make_request(unsafe_case), 3, None, None)
    assert unsafe[0].status == "VERIFIED_FAILURE"
    assert unsafe[0].evidence["fault_injected"] is True
    assert unsafe[0].evidence["memory_cleanup_succeeded"] is True
    assert unsafe[0].evidence["tool_cleanup_succeeded"] is True
    assert "side-effect count 2" in unsafe[0].evidence["steps"][0]["response_excerpt"]
    assert target.fake_refund_service["side_effect_count"] == 2
    assert target.demo_memory["refund_policy"] == target.CURRENT_REFUND_POLICY
    assert target.tool_faults._sessions == {}
    assert target.memory_controls._sessions == {}
