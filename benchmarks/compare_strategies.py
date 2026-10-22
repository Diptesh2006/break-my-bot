"""Compare fixed and AI-generated individual/cascade tests on the local demo agent."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
from breakmybot.planner import configured_provider_name, provider_credentials_configured

try:
    from benchmarks.verify_demo import _assessment, _local_http_base, _request
except ModuleNotFoundError:  # Direct `python benchmarks/compare_strategies.py` invocation.
    from verify_demo import _assessment, _local_http_base, _request

INSTRUMENTATION_ENV = "BREAKMYBOT_INSTRUMENTATION_TOKEN"
ORACLE = "contact support"
REPETITION_LIMIT = 10
PLANNER_TOKEN_BUDGET = 768

SINGLE_FAULTS = ["timeout", "exception", "empty_response", "malformed_response"]
CHAIN_FAULTS = [
    ("timeout", "timeout"),
    ("timeout", "exception"),
    ("exception", "timeout"),
    ("exception", "exception"),
]


def _instrumented_single(kind: str) -> dict:
    return {"auth_env": INSTRUMENTATION_ENV, "faults": [{
        "tool": "refund_policy", "kind": kind, "expected_substring": ORACLE}], "chains": []}


def _individual_allowlist() -> dict:
    return {"auth_env": INSTRUMENTATION_ENV,
        "faults": [{"tool": "refund_policy", "kind": kind, "expected_substring": ORACLE}
            for kind in SINGLE_FAULTS], "chains": []}


def _instrumented_chain(customer_kind: str, policy_kind: str) -> dict:
    return {"auth_env": INSTRUMENTATION_ENV, "faults": [], "chains": [{
        "name": f"customer-{customer_kind}-policy-{policy_kind}",
        "faults": [{"tool": "customer_context", "kind": customer_kind},
                   {"tool": "refund_policy", "kind": policy_kind}],
        "expected_substring": ORACLE}]}


def _cascade_allowlist() -> dict:
    return {"auth_env": INSTRUMENTATION_ENV, "faults": [
        {"tool": tool, "kind": kind, "expected_substring": ORACLE}
        for tool in ("customer_context", "refund_policy")
        for kind in ("timeout", "exception")], "chains": []}


def _wall_time_ms(run: dict) -> float | None:
    try:
        start = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(run["completed_at"].replace("Z", "+00:00"))
        return max(0.0, (end - start).total_seconds() * 1000)
    except (KeyError, TypeError, ValueError):
        return None


def _attempt(run: dict, *, generated: bool, expected_status: str) -> dict:
    scenarios = run.get("scenarios", [])
    if generated:
        scenario = next((item for item in scenarios if item.get("scenario_id", "").startswith("planned-")), None)
        proposal = next((item for item in run.get("planning", {}).get("scenarios", [])
                         if item.get("executed")), None)
    else:
        scenario = next((item for item in scenarios if item.get("scenario_id") == "instrumented-tool-1"), None)
        proposal = None
    planning = run.get("planning", {}) if generated else {}
    usage = planning.get("usage") or {}
    tokens = usage.get("total_tokens")
    if tokens is None and ("prompt_tokens" in usage or "completion_tokens" in usage):
        tokens = (usage.get("prompt_tokens", 0) or 0) + (usage.get("completion_tokens", 0) or 0)
    try:
        tokens = int(tokens) if tokens is not None else None
    except (ValueError, TypeError):
        tokens = None
    cost = planning.get("provider_cost")
    try:
        cost = float(cost) if cost is not None else None
    except (ValueError, TypeError):
        cost = None
    evidence = scenario.get("evidence", {}) if scenario else {}
    actual_status = scenario.get("status", "MISSING") if scenario else "MISSING"
    expected_faults = evidence.get("faults", [])
    injected_events = [event for event in evidence.get("injection_events", [])
        if event.get("type") == "fault.injected"]
    observed_faults = [{"tool": event.get("tool"), "kind": event.get("kind")}
        for event in injected_events]
    normalized_expected = [{"tool": item.get("tool"), "kind": item.get("kind")}
        for item in expected_faults if isinstance(item, dict)]
    injection_confirmed = (bool(normalized_expected)
        and evidence.get("fault_injected") is True
        and observed_faults == normalized_expected)
    cleanup_confirmed = evidence.get("cleanup_succeeded") is True
    valid = (run.get("status") == "COMPLETED"
        and actual_status in {"PASSED", "VERIFIED_FAILURE"}
        and injection_confirmed and cleanup_confirmed)
    plan_valid = (not generated or bool(proposal and proposal.get("execution_eligible")
        and proposal.get("executed") and len(proposal.get("execution_scenario_ids", [])) == 1))
    faults = normalized_expected
    return {
        "run_id": run.get("id"), "run_status": run.get("status"),
        "scenario_status": actual_status, "scenario_valid": valid and plan_valid,
        "injection_confirmed": injection_confirmed,
        "cleanup_confirmed": cleanup_confirmed,
        "expected_status": expected_status,
        "outcome_match": valid and plan_valid and actual_status == expected_status,
        "faults": faults, "fault_signature": json.dumps(faults, sort_keys=True),
        "duration_ms": scenario.get("duration_ms") if scenario else None,
        "wall_time_ms": _wall_time_ms(run),
        "injection_count": sum(event.get("type") == "fault.injected"
            for event in evidence.get("injection_events", [])),
        "planning_status": planning.get("status") if generated else "not_requested",
        "requested_model": planning.get("requested_model") if generated else None,
        "actual_model": planning.get("actual_model") if generated else None,
        "proposal_id": proposal.get("scenario_id") if proposal else None,
        "provider_tokens": tokens, "provider_cost_usd": cost,
    }


def _control_attempt(run: dict) -> dict:
    scenario = next((item for item in run.get("scenarios", [])
        if item.get("scenario_id") == "expected-0"), None)
    return {"run_id": run.get("id"), "run_status": run.get("status"),
        "scenario_status": scenario.get("status", "MISSING") if scenario else "MISSING"}


def _recipe_signature(faults: list[tuple[str, str]]) -> str:
    return json.dumps([{"tool": tool, "kind": kind} for tool, kind in faults], sort_keys=True)


def _strategy_summary(name: str, attempts: list[dict], controls: list[dict], *, generated: bool,
                      conditions: list[str]) -> dict:
    verified = [item for item in attempts if item["scenario_status"] == "VERIFIED_FAILURE" and item["scenario_valid"]]
    valid_count = sum(item["scenario_valid"] for item in attempts)
    valid_signatures = {item["fault_signature"] for item in attempts if item["scenario_valid"]}
    signatures = Counter(item["fault_signature"] for item in attempts
        if item["scenario_valid"] and item["scenario_status"] == "VERIFIED_FAILURE")
    reproduced = sum(count >= 2 for count in signatures.values())
    detected_conditions = sum(any(item["fault_signature"] == condition
        and item["scenario_valid"] and item["scenario_status"] == "VERIFIED_FAILURE"
        for item in attempts) for condition in conditions)
    valid_controls = [item for item in controls if item.get("run_status") == "COMPLETED"
        and item["scenario_status"] in {"PASSED", "VERIFIED_FAILURE"}]
    control_count = len(valid_controls)
    control_false_positives = sum(item["scenario_status"] == "VERIFIED_FAILURE" for item in valid_controls)
    token_values = [item["provider_tokens"] for item in attempts if item["provider_tokens"] is not None]
    cost_values = [item["provider_cost_usd"] for item in attempts if item["provider_cost_usd"] is not None]
    times = [item["wall_time_ms"] for item in verified if item["wall_time_ms"] is not None]
    target_times = [item["duration_ms"] for item in verified if item["duration_ms"] is not None]
    models = sorted({item["actual_model"] for item in attempts if item.get("actual_model")})
    return {
        "strategy": name,
        "scenario_budget": len(attempts),
        "scenario_attempts_executed": valid_count,
        "provider_planning_calls_succeeded": sum(item["planning_status"] == "completed" for item in attempts)
            if generated else 0,
        "provider_planning_success_rate": (sum(item["planning_status"] == "completed" for item in attempts)
            / len(attempts) if generated and attempts else None),
        "models_used": models,
        "known_bug_conditions_detected": detected_conditions,
        "known_bug_conditions_total": len(conditions),
        "known_bug_discovery_rate": detected_conditions / len(conditions) if conditions else None,
        "verified_failures": len(verified),
        "verified_failure_rate": len(verified) / len(attempts) if attempts else 0,
        "scenario_validity_rate": valid_count / len(attempts) if attempts else 0,
        "run_execution_success_rate": sum(item["run_status"] == "COMPLETED" for item in attempts) / len(attempts) if attempts else 0,
        "reproduced_fault_signatures": reproduced,
        "fault_signatures_observed": len(valid_signatures),
        "reproduction_rate_on_observed_fault_signatures": (
            reproduced / len(valid_signatures) if valid_signatures else None),
        "false_positive_rate_on_shared_fixed_control": (
            control_false_positives / control_count if control_count else None),
        "control_sample_validity_rate": len(valid_controls) / len(controls) if controls else 0,
        "mean_wall_time_per_verified_finding_ms": sum(times) / len(times) if times else None,
        "mean_target_time_per_verified_finding_ms": (
            sum(target_times) / len(target_times) if target_times else None),
        "total_fault_injection_events": sum(item["injection_count"] for item in attempts),
        "model_tokens": sum(token_values) if generated else 0,
        "model_token_records_complete": (len(token_values) == len(attempts)) if generated else True,
        "provider_cost_usd": (sum(cost_values) if len(cost_values) == len(attempts) else None) if generated else 0,
        "provider_cost_records": len(cost_values) if generated else 0,
        "attempts": attempts,
    }


def run_comparison(api_url: str, target_url: str, *, repetitions: int = 2,
                   planner_token_budget: int = PLANNER_TOKEN_BUDGET,
                   total_planner_token_budget: int | None = None) -> dict:
    generated_call_count = repetitions * len(SINGLE_FAULTS) * 2
    if total_planner_token_budget is None:
        total_planner_token_budget = generated_call_count * planner_token_budget
    if total_planner_token_budget < generated_call_count * 256:
        raise ValueError(f"Total planner token budget must be at least {generated_call_count * 256} "
            "to preserve the minimum per-call budget across all generated arms")
    allocated_call_budget = min(planner_token_budget,
        total_planner_token_budget // generated_call_count)
    token = os.getenv(INSTRUMENTATION_ENV, "")
    if len(token) < 32:
        raise ValueError(f"Set {INSTRUMENTATION_ENV} to a 32+ character test token in API, worker, demo, and benchmark environments")
    if not provider_credentials_configured():
        raise ValueError("Strategy comparison requires the configured provider API key, base URL, and model in the API, worker, and benchmark environments")

    with httpx.Client(timeout=httpx.Timeout(15, connect=3), follow_redirects=False, trust_env=False) as client:
        health = _request(client, "GET", f"{api_url}/api/health")
        if health.get("product") != "BreakMyBot":
            raise ValueError("The API health endpoint did not identify BreakMyBot")
        _request(client, "GET", f"{target_url}/health")

        controls = []
        for attempt in range(repetitions):
            _request(client, "POST", f"{target_url}/api/memory/reset")
            run = _assessment(client, api_url, target_url, name=f"shared-current-memory-control-{attempt + 1}",
                endpoint="/api/chat/sse", payload={"messages": [{"role": "user", "content": "What is the refund window?"}]},
                expected="14 days")
            controls.append(_control_attempt(run))

        fixed_individual, generated_individual = [], []
        individual_allowlist = _individual_allowlist()
        for kind in SINGLE_FAULTS:
            for repetition in range(repetitions):
                run = _assessment(client, api_url, target_url, name=f"fixed-individual-{kind}-{repetition + 1}",
                    endpoint="/api/chat", payload={"message": "refund policy"},
                    instrumented=_instrumented_single(kind))
                fixed_individual.append(_attempt(run, generated=False, expected_status="VERIFIED_FAILURE"))
        for repetition in range(repetitions * len(SINGLE_FAULTS)):
            run = _assessment(client, api_url, target_url, name=f"ai-individual-{repetition + 1}",
                endpoint="/api/chat", payload={"message": "refund policy"},
                instrumented=individual_allowlist, planned_mode="individual", max_planned=1,
                execute_approved=False, planner_token_budget=allocated_call_budget)
            generated_individual.append(_attempt(run, generated=True, expected_status="VERIFIED_FAILURE"))

        fixed_cascading, generated_cascading = [], []
        cascading_allowlist = _cascade_allowlist()
        for customer_kind, policy_kind in CHAIN_FAULTS:
            for repetition in range(repetitions):
                run = _assessment(client, api_url, target_url, name=f"fixed-cascade-{customer_kind}-{policy_kind}-{repetition + 1}",
                    endpoint="/api/chat/chain", payload={}, instrumented=_instrumented_chain(customer_kind, policy_kind))
                fixed_cascading.append(_attempt(run, generated=False, expected_status="VERIFIED_FAILURE"))
        for repetition in range(repetitions * len(CHAIN_FAULTS)):
            run = _assessment(client, api_url, target_url, name=f"ai-cascade-{repetition + 1}",
                endpoint="/api/chat/chain", payload={}, instrumented=cascading_allowlist,
                planned_mode="cascading", max_planned=1, execute_approved=False,
                planner_token_budget=allocated_call_budget)
            generated_cascading.append(_attempt(run, generated=True, expected_status="VERIFIED_FAILURE"))

        strategies = [
            _strategy_summary("fixed_individual", fixed_individual, controls, generated=False,
                conditions=[_recipe_signature([("refund_policy", kind)]) for kind in SINGLE_FAULTS]),
            _strategy_summary("ai_generated_individual", generated_individual, controls, generated=True,
                conditions=[_recipe_signature([("refund_policy", kind)]) for kind in SINGLE_FAULTS]),
            _strategy_summary("fixed_cascading", fixed_cascading, controls, generated=False,
                conditions=[_recipe_signature([("customer_context", first), ("refund_policy", second)])
                    for first, second in CHAIN_FAULTS]),
            _strategy_summary("ai_generated_cascading", generated_cascading, controls, generated=True,
                conditions=[_recipe_signature([("customer_context", first), ("refund_policy", second)])
                    for first, second in CHAIN_FAULTS]),
        ]
    strategy_budget = repetitions * len(SINGLE_FAULTS)
    generated_execution_counts = [item["scenario_attempts_executed"] for item in strategies
        if item["strategy"].startswith("ai_generated_")]
    return {
        "schema_version": "breakmybot-strategy-comparison-v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "target_url": target_url,
        "comparison_status": "completed" if all(generated_execution_counts) else "incomplete_generated_execution",
        "planner_provider": configured_provider_name(),
        "methodology": (
            "Loopback deterministic demo-agent pilot. Each strategy arm receives the same number of scenario "
            "executions per family (4 fault recipes × repetitions); each assessment also includes the same one "
            "baseline invocation. Generated arms make one configured-provider planning call per candidate attempt, execute at "
            "most one bounded proposal per call, and use the same approved fault atoms and response oracle as the "
            "fixed arm. Cascading recipes compose two distinct approved tool-fault atoms in target call order."
        ),
        "budget": {"repetitions_per_recipe": repetitions,
            "individual_scenario_executions_per_arm": strategy_budget,
            "cascading_scenario_executions_per_arm": strategy_budget,
            "planned_scenarios_per_generated_call": 1,
            "planner_completion_token_cap_per_call": allocated_call_budget,
            "generated_planning_calls_per_arm": strategy_budget,
            "generated_planning_calls_total": generated_call_count,
            "total_planner_completion_token_budget": total_planner_token_budget,
            "shared_fixed_control_attempts": repetitions},
        "controls": controls,
        "strategies": strategies,
        "limitations": [
            "This is a small deterministic local pilot, not a powered study and not evidence of superiority.",
            "The testbed contains one known single-tool fallback defect and one known cascading fallback defect; findings do not generalize to arbitrary agents.",
            "The generated arms use a constrained menu of operator-approved fault atoms and oracle text; they are not unrestricted natural-language test generation.",
            "Provider cost is reported only when the provider returns it; missing cost records remain null rather than being treated as free.",
            "Wall time is per assessment run and includes provider planning for generated strategies; analyst effort is not measured.",
            "The separate deterministic benchmark covers stale memory; this comparison isolates tool-fault scenario strategies.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.getenv("BREAKMYBOT_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--target-url", default="http://127.0.0.1:8001")
    parser.add_argument("--output", type=Path, default=Path("benchmark-results/strategy-comparison.json"))
    parser.add_argument("--repetitions", type=int, default=2, help=f"Repetitions per recipe (1-{REPETITION_LIMIT}; pilot default: 2)")
    parser.add_argument("--planner-token-budget", type=int, default=PLANNER_TOKEN_BUDGET, help="Completion-token cap per configured AI-provider call (256-6000)")
    parser.add_argument("--total-planner-token-budget", type=int, default=None,
        help="Aggregate completion-token budget across all generated calls (default: per-call cap × call count)")
    args = parser.parse_args()
    if not 1 <= args.repetitions <= REPETITION_LIMIT:
        parser.error(f"--repetitions must be between 1 and {REPETITION_LIMIT}")
    if not 256 <= args.planner_token_budget <= 6000:
        parser.error("--planner-token-budget must be between 256 and 6000")
    try:
        api_url = _local_http_base(args.api_url, "--api-url")
        target_url = _local_http_base(args.target_url, "--target-url")
        result = run_comparison(api_url, target_url, repetitions=args.repetitions,
            planner_token_budget=args.planner_token_budget,
            total_planner_token_budget=args.total_planner_token_budget)
    except (ValueError, httpx.HTTPError, TimeoutError, KeyError) as exc:
        print(f"Strategy comparison failed: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "budget": result["budget"],
        "strategies": [{key: value for key, value in strategy.items() if key != "attempts"}
            for strategy in result["strategies"]]}, indent=2))
    return 0 if result["comparison_status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
