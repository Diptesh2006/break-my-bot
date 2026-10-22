from benchmarks.compare_strategies import _recipe_signature, _strategy_summary
from benchmarks.compare_strategies import run_comparison
import pytest


def _attempt(signature: str, status: str, *, valid: bool = True, tokens: int | None = 10,
             cost: float | None = 0.001) -> dict:
    return {"scenario_status": status, "scenario_valid": valid,
        "run_status": "COMPLETED" if valid else "FAILED", "fault_signature": signature,
        "wall_time_ms": 20.0 if status == "VERIFIED_FAILURE" else None,
        "duration_ms": 8.0 if status == "VERIFIED_FAILURE" else None,
        "injection_count": 1, "provider_tokens": tokens, "provider_cost_usd": cost,
        "planning_status": "completed"}


def test_strategy_summary_counts_known_recipe_detection_and_reproduction():
    timeout = _recipe_signature([("refund_policy", "timeout")])
    exception = _recipe_signature([("refund_policy", "exception")])
    attempts = [
        _attempt(timeout, "VERIFIED_FAILURE"),
        _attempt(timeout, "VERIFIED_FAILURE"),
        _attempt(exception, "PASSED"),
        _attempt(exception, "MISSING", valid=False, tokens=None, cost=None),
    ]
    controls = [{"run_status": "COMPLETED", "scenario_status": "PASSED"},
        {"run_status": "COMPLETED", "scenario_status": "VERIFIED_FAILURE"},
        {"run_status": "FAILED", "scenario_status": "VERIFIED_FAILURE"}]

    summary = _strategy_summary("nrouter-generated", attempts, controls, generated=True,
        conditions=[timeout, exception])

    assert summary["scenario_budget"] == 4
    assert summary["scenario_attempts_executed"] == 3
    assert summary["known_bug_conditions_detected"] == 1
    assert summary["known_bug_conditions_total"] == 2
    assert summary["known_bug_discovery_rate"] == 0.5
    assert summary["verified_failure_rate"] == 0.5
    assert summary["scenario_validity_rate"] == 0.75
    assert summary["run_execution_success_rate"] == 0.75
    assert summary["reproduced_fault_signatures"] == 1
    assert summary["false_positive_rate_on_shared_fixed_control"] == 0.5
    assert summary["control_sample_validity_rate"] == 2 / 3
    assert summary["reproduction_rate_on_observed_fault_signatures"] == 0.5
    assert summary["model_tokens"] == 30
    assert summary["model_token_records_complete"] is False
    assert summary["provider_cost_usd"] is None
    assert summary["mean_wall_time_per_verified_finding_ms"] == 20
    assert summary["mean_target_time_per_verified_finding_ms"] == 8


def test_strategy_comparison_budget_is_zero_cost_for_fixed_arm():
    signature = _recipe_signature([("refund_policy", "timeout")])
    summary = _strategy_summary("fixed", [_attempt(signature, "VERIFIED_FAILURE", tokens=None, cost=None)],
        [{"run_status": "COMPLETED", "scenario_status": "PASSED"}], generated=False,
        conditions=[signature])

    assert summary["known_bug_conditions_detected"] == 1
    assert summary["model_tokens"] == 0
    assert summary["provider_cost_usd"] == 0
    assert summary["provider_cost_records"] == 0


def test_live_strategy_comparison_requires_real_provider_configuration(monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", "test-token-long-enough-for-local-tests")
    monkeypatch.setenv("BREAKMYBOT_LLM_PROVIDER", "nrouter")
    monkeypatch.delenv("NROUTER_API_KEY", raising=False)
    monkeypatch.delenv("BREAKMYBOT_PLANNER_MODEL", raising=False)
    monkeypatch.delenv("BREAKMYBOT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("BREAKMYBOT_LLM_API_KEY_ENV", raising=False)
    monkeypatch.delenv("BREAKMYBOT_LLM_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="requires the configured provider API key"):
        run_comparison("http://127.0.0.1:1", "http://127.0.0.1:2", repetitions=1)


def test_total_token_budget_is_checked_before_provider_or_target_calls(monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", "test-token-long-enough-for-local-tests")
    monkeypatch.setenv("BREAKMYBOT_LLM_PROVIDER", "nrouter")
    monkeypatch.setenv("NROUTER_API_KEY", "test-key")
    monkeypatch.setenv("BREAKMYBOT_PLANNER_MODEL", "test-model")
    for budget in (0, 2047):
        with pytest.raises(ValueError, match="Total planner token budget must be at least 2048"):
            run_comparison("http://127.0.0.1:1", "http://127.0.0.1:2", repetitions=1,
                planner_token_budget=768, total_planner_token_budget=budget)


def test_total_token_budget_caps_each_generated_call():
    from benchmarks.compare_strategies import CHAIN_FAULTS, SINGLE_FAULTS

    repetitions = 2
    calls = repetitions * (len(SINGLE_FAULTS) + len(CHAIN_FAULTS))
    allocated = min(768, 10_000 // calls)
    assert calls == 16
    assert allocated == 625
