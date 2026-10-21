import json
from pathlib import Path

import httpx
import pytest

from breakmybot import runner, store
from breakmybot.models import RunRecord, RunRequest, SemanticJudgeAssessment
from breakmybot.semantic_judge import extract_answer, judge_answers


def _case(**updates):
    value = {"id": "refund-case", "name": "Refund policy", "question": "What is the refund period?",
        "reference": "Refunds are available within 14 days.", "expected": "Say that refunds are available within 14 days.",
        "payload": {"message": "What is the refund period?"}}
    value.update(updates)
    return value


def _configured_provider(monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_LLM_PROVIDER", "nrouter")
    monkeypatch.setenv("NROUTER_API_KEY", "judge-provider-secret")
    monkeypatch.setenv("NROUTER_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("BREAKMYBOT_PLANNER_MODEL", "planner-model")
    monkeypatch.setenv("BREAKMYBOT_JUDGE_MODEL", "judge-model")
    monkeypatch.setenv("BREAKMYBOT_JUDGE_FALLBACK_MODEL", "judge-fallback")


@pytest.mark.asyncio
async def test_judge_uses_role_model_and_returns_grounded_structured_verdict(monkeypatch):
    _configured_provider(monkeypatch)
    calls = []
    real_async_client = httpx.AsyncClient

    def handler(request):
        body = json.loads(request.content)
        calls.append((str(request.url), request.headers["authorization"], body))
        judgment = {"case_id": "refund-case", "verdict": "pass", "confidence": 0.91,
            "explanation": "The answer matches the supplied policy.",
            "answer_quote": "Refunds are available within 14 days.",
            "reference_quote": "Refunds are available within 14 days."}
        return httpx.Response(200, json={"model": "judge-model-live", "usage": {"total_tokens": 123},
            "cost": 0.002, "choices": [{"message": {"content": json.dumps({"judgments": [judgment]})}}]})

    monkeypatch.setattr("breakmybot.semantic_judge.httpx.AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs))
    result = await judge_answers([{**_case(), "case_id": "refund-case", "answer": "Refunds are available within 14 days."}])
    assert result["status"] == "completed"
    assert result["actual_model"] == "judge-model-live"
    assert result["usage"]["total_tokens"] == 123
    assert result["judgments"][0]["verdict"] == "pass"
    assert calls[0][0] == "https://provider.example/v1/chat/completions"
    assert calls[0][2]["model"] == "judge-model"
    assert "judge-provider-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_judge_downgrades_low_confidence_and_rejects_ungrounded_quotes(monkeypatch):
    _configured_provider(monkeypatch)
    judgment = {"case_id": "refund-case", "verdict": "pass", "confidence": 0.4,
        "explanation": "Likely matches.", "answer_quote": "Refunds are available within 14 days.",
        "reference_quote": "Refunds are available within 14 days."}

    real_async_client = httpx.AsyncClient

    def make_client(quote):
        def handler(_request):
            item = {**judgment, "answer_quote": quote}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"judgments": [item]})}}]})
        return lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("breakmybot.semantic_judge.httpx.AsyncClient", make_client(judgment["answer_quote"]))
    result = await judge_answers([{**_case(), "case_id": "refund-case", "answer": "Refunds are available within 14 days."}])
    assert result["judgments"][0]["verdict"] == "inconclusive"
    monkeypatch.setattr("breakmybot.semantic_judge.httpx.AsyncClient", make_client("not in the answer"))
    rejected = await judge_answers([{**_case(), "case_id": "refund-case", "answer": "Refunds are available within 14 days."}])
    assert rejected["status"] == "error"


def test_semantic_judge_requires_consent_and_non_get_repeat_authorization():
    assessment = SemanticJudgeAssessment(cases=[_case()])
    common = {"target_url": "http://localhost:8000", "endpoint_path": "/chat", "method": "POST",
        "semantic_judge": assessment}
    with pytest.raises(ValueError, match="explicit provider-data consent"):
        RunRequest(**common)
    with pytest.raises(ValueError, match="repeated-call authorization"):
        RunRequest(**common, semantic_judge_consent=True)
    request = RunRequest(**common, semantic_judge_consent=True, allow_repeated_post=True)
    assert request.semantic_judge is not None


def test_semantic_judge_extracts_plain_json_and_openai_sse_answers():
    assert extract_answer('{"answer":"Fourteen days"}', "application/json") == "Fourteen days"
    stream = 'data: {"choices":[{"delta":{"content":"14 days"}}]}\n\ndata: [DONE]\n\n'
    assert extract_answer(stream, "text/event-stream") == "14 days"


def test_provider_status_exposes_judge_readiness_without_credentials(monkeypatch):
    _configured_provider(monkeypatch)
    from breakmybot.planner import public_provider_status

    status = public_provider_status()
    assert status["judge_configured"] is True
    assert status["judge_model"] == "judge-model"
    assert status["judge_fallback_model"] == "judge-fallback"
    assert "judge-provider-secret" not in json.dumps(status)


@pytest.mark.asyncio
async def test_runner_invokes_operator_cases_and_persists_judgment_separately(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BREAKMYBOT_LLM_PROVIDER", "nrouter")
    monkeypatch.setenv("NROUTER_API_KEY", "test-judge-key")
    monkeypatch.setenv("BREAKMYBOT_JUDGE_MODEL", "judge-model")
    calls = []

    async def target_invoke(_url, _path, _method, payload, _timeout, *, docker_mode=False, headers=None):
        calls.append(payload)
        return 200, '{"answer":"Refunds are available within 14 days."}', 12.5, "application/json"

    async def fake_judge(cases):
        assert cases[0]["answer"] == "Refunds are available within 14 days."
        return {"status": "completed", "provider": "nRouter", "requested_model": "judge-model",
            "actual_model": "judge-model-live", "usage": {"total_tokens": 50}, "provider_cost": 0.001,
            "judged_cases": 1, "judgments": [{"case_id": "refund-case", "verdict": "pass",
                "confidence": 0.9, "confidence_threshold": 0.65, "explanation": "Matches reference.",
                "answer_quote": "Refunds are available within 14 days.",
                "reference_quote": "Refunds are available within 14 days."}]}

    monkeypatch.setattr(runner, "invoke", target_invoke)
    monkeypatch.setattr(runner.semantic_judge, "judge_answers", fake_judge)
    request = RunRequest(target_url="http://localhost:8000", endpoint_path="/chat", method="GET", profile="quick",
        semantic_judge={"cases": [_case()]}, semantic_judge_consent=True)
    run = RunRecord(target_url=str(request.target_url), endpoint_path=request.endpoint_path,
        method=request.method, profile=request.profile)
    store.save(run)
    result = await runner.execute(run.id, request)
    judged = next(item for item in result.scenarios if item.scenario_id == "judge-refund-case")
    assert judged.status == "PASSED"
    assert judged.evidence["judge"]["confidence"] == 0.9
    assert len(calls) == 2  # one baseline plus exactly one explicit case invocation
    assert result.semantic_judging["actual_model"] == "judge-model-live"
    assert result.semantic_judging["usage"]["total_tokens"] == 50
    report = Path(result.report_paths["html"]).read_text(encoding="utf-8")
    assert "Reference-grounded semantic judgments" in report
    assert "Matches reference." in report


@pytest.mark.asyncio
async def test_unconfigured_judge_does_not_make_extra_target_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BREAKMYBOT_LLM_PROVIDER", "nrouter")
    monkeypatch.setenv("NROUTER_API_KEY", "")
    monkeypatch.setenv("BREAKMYBOT_JUDGE_MODEL", "")
    monkeypatch.setenv("BREAKMYBOT_PLANNER_MODEL", "")
    monkeypatch.setenv("BREAKMYBOT_LLM_MODEL", "")
    calls = []

    async def target_invoke(*args, **_kwargs):
        calls.append(args)
        return 200, '{"answer":"A valid answer"}', 1.0, "application/json"

    monkeypatch.setattr(runner, "invoke", target_invoke)
    request = RunRequest(target_url="http://localhost:8000", endpoint_path="/chat", method="GET", profile="quick",
        semantic_judge={"cases": [_case()]}, semantic_judge_consent=True)
    run = RunRecord(target_url=str(request.target_url), endpoint_path=request.endpoint_path,
        method=request.method, profile=request.profile)
    store.save(run)
    result = await runner.execute(run.id, request)
    assert len(calls) == 1  # baseline only; the judge case was not sent to the target
    assert result.semantic_judging["status"] == "unavailable"
    assert next(item for item in result.scenarios if item.scenario_id == "judge-refund-case").status == "SETUP_ERROR"
