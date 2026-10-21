from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError

from breakmybot import planner, runner, store
from breakmybot.instrumentation import instrument
from breakmybot.models import RunRecord, RunRequest
from breakmybot.planner import ScenarioProposal, _gate, gate_generated_proposal


def _request(**overrides) -> RunRequest:
    values = {"target_url": "http://127.0.0.1:8001", "endpoint_path": "/chat",
        "method": "POST", "allow_repeated_post": True, "use_nrouter_planner": True,
        "execute_planned_scenarios": True,
        "instrumented_assessment": {"auth_env": "BREAKMYBOT_INSTRUMENTATION_TOKEN", "faults": [{
            "tool": "catalog", "kind": "timeout", "expected_substring": "contact support"}]}}
    values.update(overrides)
    return RunRequest(**values)


def _proposal(**execution_overrides) -> ScenarioProposal:
    execution = {"mode": "individual", "faults": [{"tool": "catalog", "kind": "timeout"}],
        "expected_substring": "contact support"}
    execution.update(execution_overrides.pop("execution", execution_overrides))
    return ScenarioProposal(scenario_id="generated_timeout", title="Timeout recovery",
        hypothesis="A dependency timeout may produce an unsafe answer.",
        required_capabilities=["instrumented_tool_adapter"],
        validation_strategy="Inject the approved timeout and check the approved response invariant.",
        execution=execution)


def test_generated_execution_requires_both_planner_and_instrumentation_opt_ins():
    with pytest.raises(ValidationError, match="requires the configured AI planner"):
        _request(use_nrouter_planner=False)
    with pytest.raises(ValidationError, match="instrumentation allowlist"):
        _request(instrumented_assessment=None)
    with pytest.raises(ValidationError, match="repeated-call authorization"):
        _request(allow_repeated_post=False)


@pytest.mark.parametrize("provider,base_url,key_name", [
    ("openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    ("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    ("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    ("mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    ("deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
])
def test_named_provider_presets_use_provider_key_and_openai_compatible_endpoint(
        monkeypatch, provider, base_url, key_name):
    monkeypatch.setenv("BREAKMYBOT_LLM_PROVIDER", provider)
    monkeypatch.setenv("BREAKMYBOT_PLANNER_MODEL", "operator-selected-model")
    monkeypatch.setenv(key_name, "provider-test-key")
    for name in ("BREAKMYBOT_LLM_API_KEY", "BREAKMYBOT_LLM_API_KEY_ENV", "BREAKMYBOT_LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    settings = planner._provider_settings()

    assert settings["api_key"] == "provider-test-key"
    assert settings["base_url"] == base_url
    assert settings["model"] == "operator-selected-model"
    assert planner._chat_completions_url(settings) == base_url + "/chat/completions"


def test_generated_execution_gate_only_accepts_approved_fault_and_oracle():
    request = _request()
    approved = _proposal()
    assert _gate(approved, request)["execution_eligible"] is True
    assert gate_generated_proposal(approved.model_dump(), request)[0] is not None
    wrong_strategy = _request(planned_scenario_mode="cascading")
    candidate, reason = gate_generated_proposal(approved.model_dump(), wrong_strategy)
    assert candidate is None
    assert "requested benchmark strategy" in reason

    unapproved_tool = _proposal(execution={"mode": "individual", "faults": [
        {"tool": "admin_delete", "kind": "timeout"}], "expected_substring": "contact support"})
    candidate, reason = gate_generated_proposal(unapproved_tool.model_dump(), request)
    assert candidate is None
    assert "approved individual scenario" in reason

    unapproved_oracle = _proposal(execution={"mode": "individual", "faults": [
        {"tool": "catalog", "kind": "timeout"}], "expected_substring": "refund guaranteed"})
    candidate, reason = gate_generated_proposal(unapproved_oracle.model_dump(), request)
    assert candidate is None
    assert "individual scenario" in reason

    call_bounded_request = _request(instrumented_assessment={"auth_env": "BREAKMYBOT_INSTRUMENTATION_TOKEN",
        "faults": [{"tool": "catalog", "kind": "timeout", "on_call": 1,
            "expected_substring": "contact support"}]})
    candidate, reason = gate_generated_proposal(approved.model_dump(), call_bounded_request)
    assert candidate is None
    assert "individual scenario" in reason
    bounded = _proposal(execution={"mode": "individual", "faults": [
        {"tool": "catalog", "kind": "timeout", "on_call": 1}],
        "expected_substring": "contact support"})
    assert gate_generated_proposal(bounded.model_dump(), call_bounded_request)[0] is not None

    chain_request = _request(instrumented_assessment={"auth_env": "BREAKMYBOT_INSTRUMENTATION_TOKEN",
        "chains": [{"name": "approved-chain", "faults": [
            {"tool": "customer_context", "kind": "exception"},
            {"tool": "refund_policy", "kind": "timeout"}], "expected_substring": "contact support"}]})
    approved_chain = _proposal(execution={"mode": "cascading", "faults": [
        {"tool": "customer_context", "kind": "exception"},
        {"tool": "refund_policy", "kind": "timeout"}], "expected_substring": "contact support"})
    assert gate_generated_proposal(approved_chain.model_dump(), chain_request)[0] is not None
    reordered_chain = _proposal(execution={"mode": "cascading", "faults": [
        {"tool": "refund_policy", "kind": "timeout"},
        {"tool": "customer_context", "kind": "exception"}], "expected_substring": "contact support"})
    assert gate_generated_proposal(reordered_chain.model_dump(), chain_request)[0] is not None

    duplicate_tool_chain = _proposal(execution={"mode": "cascading", "faults": [
        {"tool": "customer_context", "kind": "exception"},
        {"tool": "customer_context", "kind": "exception"}], "expected_substring": "contact support"})
    candidate, reason = gate_generated_proposal(duplicate_tool_chain.model_dump(), chain_request)
    assert candidate is None
    assert "each tool only once" in reason


@pytest.mark.asyncio
async def test_nrouter_plan_receives_only_execution_allowlists_and_bounds(monkeypatch):
    request = _request(max_planned_scenarios=2, planner_token_budget=512)
    model_plan = {"scenarios": [{"scenario_id": "generated_timeout", "title": "Timeout recovery",
        "hypothesis": "A timeout could produce an unsafe fallback.",
        "required_capabilities": ["instrumented_tool_adapter"],
        "expected_invariants": ["Contact support on dependency failure."],
        "validation_strategy": "Inject the approved timeout and check the approved oracle.",
        "execution": {"mode": "individual", "faults": [
            {"tool": "catalog", "kind": "timeout"}], "expected_substring": "contact support"}}]}
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"model": "test-model", "usage": {"total_tokens": 20}, "cost": 0.001,
                "choices": [{"message": {"content": json.dumps(model_plan)}}]}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **kwargs):
            captured.update(kwargs["json"])
            return FakeResponse()

    monkeypatch.setenv("NROUTER_API_KEY", "test-provider-key")
    monkeypatch.setenv("BREAKMYBOT_LLM_PROVIDER", "nrouter")
    monkeypatch.setenv("BREAKMYBOT_PLANNER_MODEL", "test-model")
    monkeypatch.setenv("BREAKMYBOT_MAX_TOKENS_PER_RUN", "12000")
    monkeypatch.setattr(planner.httpx, "AsyncClient", FakeClient)

    result = await planner.plan(request, baseline_profile={"http_status": 200,
        "http_success": True, "latency_ms": 4.2, "content_type": "application/json",
        "response_size_bytes": 18, "response_contract": "PASSED"})

    assert result["provider"] == "nRouter"
    metadata = json.loads(captured["messages"][1]["content"])
    assert metadata["baseline_profile"] == {"http_status": 200, "http_success": True,
        "latency_ms": 4.2, "content_type": "application/json", "response_size_bytes": 18,
        "response_contract": "PASSED"}
    assert "hello" not in captured["messages"][1]["content"]
    assert "14 days" not in captured["messages"][1]["content"]
    assert metadata["max_scenarios"] == 2
    assert captured["max_completion_tokens"] == 512
    assert metadata["planner_token_budget"] == 512
    assert metadata["execute_planned_scenarios"] is True
    assert metadata["approved_individual_scenarios"] == [{
        "fault": {"tool": "catalog", "kind": "timeout", "delay_seconds": 0.0,
            "every_n_calls": 1, "on_call": None},
        "expected_substring": "contact support"}]
    assert metadata["approved_fault_atoms"] == [{"tool": "catalog", "kind": "timeout",
        "delay_seconds": 0.0, "every_n_calls": 1, "on_call": None}]
    assert result["scenarios"][0]["execution_eligible"] is True
    assert result["scenarios"][0]["executed"] is False
    assert result["usage"]["total_tokens"] == 20
    assert result["provider_cost"] == 0.001


@pytest.mark.asyncio
async def test_configurable_openai_compatible_provider_uses_external_key_env(monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("BREAKMYBOT_LLM_API_KEY_ENV", "OPENAI_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret-value")
    monkeypatch.setenv("BREAKMYBOT_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("BREAKMYBOT_LLM_AUTH_HEADER", "x-api-key")
    monkeypatch.setenv("BREAKMYBOT_LLM_AUTH_SCHEME", "")
    monkeypatch.setenv("BREAKMYBOT_LLM_TOKEN_PARAMETER", "max_tokens")
    monkeypatch.setenv("BREAKMYBOT_LLM_CHAT_COMPLETIONS_PATH", "chat/completions")
    monkeypatch.setenv("BREAKMYBOT_LLM_MODEL", "compatible-model")
    monkeypatch.delenv("BREAKMYBOT_PLANNER_MODEL", raising=False)
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"model": "compatible-model", "usage": {"total_tokens": 10},
                "choices": [{"message": {"content": '{"scenarios": []}'}}]}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, body=json)
            return FakeResponse()

    monkeypatch.setattr(planner.httpx, "AsyncClient", FakeClient)
    result = await planner.plan(_request(execute_planned_scenarios=False,
        instrumented_assessment=None))

    assert result["status"] == "completed"
    assert result["provider"] == "openai-compatible"
    assert result["actual_model"] == "compatible-model"
    assert captured["url"] == "https://llm.example.test/v1/chat/completions"
    assert captured["headers"] == {"x-api-key": "provider-secret-value"}
    assert captured["body"]["max_tokens"] == 6000
    assert "provider-secret-value" not in json.dumps(result)


@pytest.mark.asyncio
async def test_provider_retries_only_transient_http_once():
    calls = []

    class FakeClient:
        async def post(self, _url, *, headers, json):
            calls.append(json["model"])
            return httpx.Response(503 if len(calls) == 1 else 200,
                request=httpx.Request("POST", "https://provider.example/v1/chat/completions"), json={"ok": True})

    provider = {"model": "primary", "fallback_model": "fallback", "api_key": "secret",
        "auth_header": "Authorization", "auth_scheme": "Bearer"}
    response, model, attempts = await planner.post_chat_completion(
        FakeClient(), "https://provider.example/v1/chat/completions", provider, {"messages": []})
    assert response.status_code == 200
    assert model == "primary"
    assert attempts == 2
    assert calls == ["primary", "primary"]


@pytest.mark.asyncio
async def test_provider_uses_fallback_only_when_primary_model_is_not_found():
    calls = []

    class FakeClient:
        async def post(self, _url, *, headers, json):
            calls.append(json["model"])
            return httpx.Response(404 if len(calls) == 1 else 200,
                request=httpx.Request("POST", "https://provider.example/v1/chat/completions"), json={"ok": True})

    provider = {"model": "primary", "fallback_model": "fallback", "api_key": "secret",
        "auth_header": "Authorization", "auth_scheme": "Bearer"}
    response, model, attempts = await planner.post_chat_completion(
        FakeClient(), "https://provider.example/v1/chat/completions", provider, {"messages": []})
    assert response.status_code == 200
    assert calls == ["primary", "fallback"]
    assert model == "fallback"
    assert attempts == 2


@pytest.mark.asyncio
async def test_provider_does_not_retry_ambiguous_network_timeout():
    calls = 0

    class FakeClient:
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("provider response timed out")

    provider = {"model": "primary", "fallback_model": "fallback", "api_key": "secret",
        "auth_header": "Authorization", "auth_scheme": "Bearer"}
    with pytest.raises(httpx.ReadTimeout):
        await planner.post_chat_completion(FakeClient(), "https://provider.example/v1/chat/completions",
            provider, {"messages": []})
    assert calls == 1


@pytest.mark.asyncio
async def test_configured_provider_rejects_insecure_remote_url_without_sending_key(monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_LLM_PROVIDER", "custom")
    monkeypatch.setenv("BREAKMYBOT_LLM_API_KEY", "must-not-leak")
    monkeypatch.setenv("BREAKMYBOT_LLM_BASE_URL", "http://provider.example.test/v1")
    monkeypatch.setenv("BREAKMYBOT_PLANNER_MODEL", "some-model")

    class ForbiddenClient:
        def __init__(self, **_kwargs):
            pytest.fail("insecure remote provider URL must be rejected before network access")

    monkeypatch.setattr(planner.httpx, "AsyncClient", ForbiddenClient)
    result = await planner.plan(_request(execute_planned_scenarios=False,
        instrumented_assessment=None))
    assert result["status"] == "error"
    assert result["provider"] == "custom"
    assert "must-not-leak" not in json.dumps(result)


@pytest.mark.asyncio
async def test_approved_generated_scenario_executes_against_instrumented_target(tmp_path, monkeypatch):
    target = FastAPI()
    token = "generated-plan-test-token-123456789"
    controller = instrument(target, token=token, enabled=True)

    @target.post("/chat")
    async def chat():
        try:
            answer = await controller.invoke("catalog", lambda: "normal response")
        except TimeoutError:
            answer = "Please contact support."
        return {"answer": answer}

    original_client = httpx.AsyncClient
    execution_order = []

    def control_client(*args, **kwargs):
        return original_client(*args, **kwargs, transport=ASGITransport(app=target))

    async def invoke_target(url, path, method, payload, timeout, *, docker_mode=False, headers=None):
        async with original_client(transport=ASGITransport(app=target), base_url=url) as client:
            response = await client.request(method, path, json=payload, headers=headers)
        execution_order.append("baseline")
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    monkeypatch.setattr("breakmybot.instrumentation.httpx.AsyncClient", control_client)
    monkeypatch.setattr(runner, "invoke", invoke_target)
    monkeypatch.setattr(runner, "_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BREAKMYBOT_DOCKER_MODE", "false")
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", token)
    request = _request(execute_approved_instrumented_scenarios=False, profile="quick")
    proposal = _proposal()

    async def generated_plan(_request, *, baseline_profile=None):
        execution_order.append("planner")
        assert baseline_profile["http_status"] == 200
        return {"status": "completed", "provider": "test-fixture", "scenarios": [
            {**proposal.model_dump(), **_gate(proposal, request)}]}

    monkeypatch.setattr(runner.planner, "plan", generated_plan)
    run = RunRecord(target_url=str(request.target_url), endpoint_path=request.endpoint_path,
        method=request.method, profile="quick")
    store.save(run)

    result = await runner.execute(run.id, request)

    planned = [item for item in result.scenarios if item.scenario_id.startswith("planned-")]
    assert result.status == "COMPLETED"
    assert execution_order == ["baseline", "planner", "baseline"]
    assert len(planned) == 1
    assert not any(item.scenario_id.startswith("instrumented-tool-") for item in result.scenarios)
    assert planned[0].status == "PASSED"
    assert planned[0].evidence["fault_injected"] is True
    assert planned[0].evidence["cleanup_succeeded"] is True
    assert result.planning["scenarios"][0]["executed"] is True
    assert result.planning["scenarios"][0]["execution_statuses"] == ["PASSED"]
    assert controller._sessions == {}


@pytest.mark.parametrize("kind", ["timeout", "exception", "empty_response", "malformed_response"])
@pytest.mark.asyncio
async def test_comparison_individual_fault_recipes_are_observed_and_verified(kind, tmp_path, monkeypatch):
    target = FastAPI()
    token = "comparison-fault-test-token-123456789"
    controller = instrument(target, token=token, enabled=True)

    @target.post("/chat")
    async def chat():
        try:
            answer = await controller.invoke("refund_policy", lambda: "14 days")
        except TimeoutError:
            answer = "Your refund is guaranteed for any purchase date."
        return {"answer": answer}

    original_client = httpx.AsyncClient

    def control_client(*args, **kwargs):
        return original_client(*args, **kwargs, transport=ASGITransport(app=target))

    async def invoke_target(url, path, method, payload, timeout, *, docker_mode=False, headers=None):
        async with original_client(transport=ASGITransport(app=target, raise_app_exceptions=False),
                                  base_url=url) as client:
            response = await client.request(method, path, json=payload, headers=headers)
        return response.status_code, response.text, 1.0, response.headers.get("content-type", "")

    monkeypatch.setattr("breakmybot.instrumentation.httpx.AsyncClient", control_client)
    monkeypatch.setattr(runner, "invoke", invoke_target)
    monkeypatch.setattr(runner, "_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BREAKMYBOT_DOCKER_MODE", "false")
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", token)
    request = RunRequest(target_url="http://127.0.0.1:8001", endpoint_path="/chat", method="POST",
        allow_repeated_post=True, instrumented_assessment={"auth_env": "BREAKMYBOT_INSTRUMENTATION_TOKEN",
            "faults": [{"tool": "refund_policy", "kind": kind, "expected_substring": "contact support"}]})
    run = RunRecord(target_url=str(request.target_url), endpoint_path=request.endpoint_path,
        method=request.method, profile="quick")
    store.save(run)

    result = await runner._run_instrumented_scenarios(run, request, 10, None, None)

    assert len(result) == 1
    assert result[0].status == "VERIFIED_FAILURE"
    assert result[0].evidence["fault_injected"] is True
    assert result[0].evidence["cleanup_succeeded"] is True
    assert result[0].evidence["faults"] == [{"tool": "refund_policy", "kind": kind}]
    assert controller._sessions == {}
