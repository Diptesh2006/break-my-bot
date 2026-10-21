"""Run a reproducible, local-only BreakMyBot benchmark against the demo agent."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "PARTIAL"}
INSTRUMENTATION_ENV = "BREAKMYBOT_INSTRUMENTATION_TOKEN"


def _local_http_base(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{label} must be an http:// loopback base URL")
    try:
        local = parsed.hostname.casefold() == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = False
    if not local or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"{label} must be an http:// loopback base URL without a path or query")
    return value.rstrip("/")


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    response = client.request(method, url, **kwargs)
    response.raise_for_status()
    return response.json()


def _assessment(client: httpx.Client, api_url: str, target_url: str, *, name: str,
                endpoint: str, payload: dict, expected: str | None = None,
                instrumented: dict | None = None) -> dict:
    body = {"target_url": target_url, "endpoint_path": endpoint, "method": "POST",
        "payload": payload, "profile": "quick", "expected_substring": expected,
        "allow_repeated_post": instrumented is not None,
        "instrumented_assessment": instrumented}
    accepted = _request(client, "POST", f"{api_url}/api/runs", json=body)
    run_id = accepted["run_id"]
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        run = _request(client, "GET", f"{api_url}/api/runs/{run_id}")
        if run["status"] in TERMINAL:
            run["benchmark_case"] = name
            return run
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for benchmark run {run_id}")


def _case_summary(name: str, runs: list[dict], scenario_id: str, expected_status: str) -> dict:
    attempts = []
    for run in runs:
        scenario = next((item for item in run["scenarios"] if item["scenario_id"] == scenario_id), None)
        attempts.append({"run_id": run["id"], "run_status": run["status"],
            "scenario_status": scenario["status"] if scenario else "MISSING",
            "evidence": scenario["evidence"] if scenario else {},
            "duration_ms": scenario["duration_ms"] if scenario else None})
    successful_execution = all(run["status"] == "COMPLETED" and row["scenario_status"] != "MISSING"
                               for run, row in zip(runs, attempts))
    expected_outcome = successful_execution and all(row["scenario_status"] == expected_status for row in attempts)
    return {"name": name, "expected_scenario_status": expected_status,
        "execution_success": successful_execution, "outcome_match": expected_outcome,
        "reproduced_on_both_attempts": len(attempts) == 2 and expected_outcome,
        "attempts": attempts}


def run_benchmark(api_url: str, target_url: str) -> dict:
    token = os.getenv(INSTRUMENTATION_ENV, "")
    if len(token) < 32:
        raise ValueError(f"Set {INSTRUMENTATION_ENV} to a 32+ character test token in the API, worker, and demo environments")
    timeout = httpx.Timeout(15, connect=3)
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        health = _request(client, "GET", f"{api_url}/api/health")
        if health.get("product") != "BreakMyBot":
            raise ValueError("The API health endpoint did not identify BreakMyBot")
        _request(client, "GET", f"{target_url}/health")

        memory_payload = {"messages": [{"role": "user", "content": "What is the refund window?"}]}
        fixed_runs = []
        for attempt in range(2):
            _request(client, "POST", f"{target_url}/api/memory/reset")
            fixed_runs.append(_assessment(client, api_url, target_url, name=f"fixed-current-memory-{attempt + 1}",
                endpoint="/api/chat/sse", payload=memory_payload, expected="14 days"))
        fixed_control = _case_summary("fixed suite: current-memory control", fixed_runs,
            "expected-0", "PASSED")

        stale_runs = []
        try:
            for attempt in range(2):
                _request(client, "POST", f"{target_url}/api/memory/seed", json={
                    "value": "Customers may request a refund within 30 days of purchase."})
                stale_runs.append(_assessment(client, api_url, target_url, name=f"stale-memory-{attempt + 1}",
                    endpoint="/api/chat/sse", payload=memory_payload, expected="14 days"))
        finally:
            _request(client, "POST", f"{target_url}/api/memory/reset")
        stale_memory = _case_summary("known bug: stale memory", stale_runs, "expected-0", "VERIFIED_FAILURE")

        single_fault = {"auth_env": INSTRUMENTATION_ENV, "faults": [{"tool": "refund_policy",
            "kind": "timeout", "expected_substring": "contact support"}], "chains": []}
        single_runs = [_assessment(client, api_url, target_url, name=f"single-tool-timeout-{attempt + 1}",
            endpoint="/api/chat", payload={"message": "refund policy"}, instrumented=single_fault)
            for attempt in range(2)]
        single_case = _case_summary("known bug: unsafe single-tool fallback", single_runs,
            "instrumented-tool-1", "VERIFIED_FAILURE")

        chain = {"auth_env": INSTRUMENTATION_ENV, "faults": [], "chains": [{
            "name": "customer-context-then-policy-timeout", "faults": [
                {"tool": "customer_context", "kind": "exception"},
                {"tool": "refund_policy", "kind": "timeout"}],
            "expected_substring": "contact support"}]}
        chain_runs = [_assessment(client, api_url, target_url, name=f"fault-chain-{attempt + 1}",
            endpoint="/api/chat/chain", payload={}, instrumented=chain) for attempt in range(2)]
        chain_case = _case_summary("known bug: unsafe cascading-fault fallback", chain_runs,
            "instrumented-tool-1", "VERIFIED_FAILURE")

    cases = [fixed_control, stale_memory, single_case, chain_case]
    known_bugs = [stale_memory, single_case, chain_case]
    execution_rows = [attempt for case in cases for attempt in case["attempts"]]
    verified_findings = [attempt for case in known_bugs for attempt in case["attempts"]
        if attempt["scenario_status"] == "VERIFIED_FAILURE"]
    response_durations = [attempt["duration_ms"] for attempt in verified_findings
        if attempt["duration_ms"] is not None]
    total_experiments = len(execution_rows)
    passed_controls = sum(attempt["scenario_status"] == "PASSED" for attempt in fixed_control["attempts"])
    return {
        "schema_version": "breakmybot-demo-benchmark-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_url": target_url,
        "methodology": "Deterministic local fixture; 2 repetitions per condition; one fixed control and three seeded known-bug conditions.",
        "metrics": {
            "known_bug_conditions_detected": sum(case["outcome_match"] for case in known_bugs),
            "known_bug_conditions_total": len(known_bugs),
            "known_bug_attempts_verified": len(verified_findings),
            "known_bug_attempts_total": sum(len(case["attempts"]) for case in known_bugs),
            "reproducible_known_bug_conditions": sum(case["reproduced_on_both_attempts"] for case in known_bugs),
            "false_positive_rate_on_fixed_control": (2 - passed_controls) / 2,
            "run_execution_success_rate": (sum(row["run_status"] == "COMPLETED" for row in execution_rows) / total_experiments
                if total_experiments else 0),
            "mean_target_time_per_verified_finding_ms": (sum(response_durations) / len(response_durations)
                if response_durations else None),
            "model_tokens": 0,
            "provider_cost_usd": 0,
        },
        "cases": cases,
        "limitations": [
            "This validates only the listed deterministic demo fixtures; it is not a comparative research result.",
            "No LLM-generated or nRouter planner hypotheses are executed by the current planner, so strategy comparison is not measured.",
            "The fixed control is one case and cannot estimate general false-positive rates.",
            "Timing is local target request duration, not end-to-end analyst time or cost.",
            "The instrumented tool cases require the demo started with DEMO_UNSAFE_TOOL_FALLBACK=true and target instrumentation enabled.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.getenv("BREAKMYBOT_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--target-url", default="http://127.0.0.1:8001")
    parser.add_argument("--output", type=Path, default=Path("benchmark-results/demo-benchmark.json"))
    args = parser.parse_args()
    try:
        api_url = _local_http_base(args.api_url, "--api-url")
        target_url = _local_http_base(args.target_url, "--target-url")
        result = run_benchmark(api_url, target_url)
    except (ValueError, httpx.HTTPError, TimeoutError, KeyError) as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), **result["metrics"]}, indent=2))
    return 0 if all(case["outcome_match"] for case in result["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
