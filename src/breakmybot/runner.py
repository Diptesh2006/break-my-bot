from __future__ import annotations

import asyncio
import os
import json
from datetime import datetime, timezone

import httpx

from . import memoryscope, planner, reports, scoring, store
from .models import RunRecord, RunRequest, ScenarioResult
from .redaction import redact_text
from .target import invoke, validate_target


async def execute(run_id: str, request: RunRequest, cancel: asyncio.Event | None = None) -> RunRecord:
    run = store.get(run_id)
    if not run:
        raise KeyError(run_id)
    run.status = "RUNNING"
    store.save(run)
    store.append_event(run.id, {"type": "run.started", "phase": "DISCOVERING"})
    try:
        run.planning = await planner.plan(request) if request.use_nrouter_planner else {
            "status": "not_requested", "provider": "none", "scenarios": []}
    except Exception:
        run.planning = {"status": "error", "provider": "nRouter", "reason": "Planner setup failed.", "scenarios": []}
    store.save(run)
    store.append_event(run.id, {"type": "planning.completed", "status": run.planning.get("status"),
        "scenario_count": len(run.planning.get("scenarios", []))})
    try:
        scenario_limit = max(1, int(os.getenv("BREAKMYBOT_MAX_SCENARIOS", "12")))
    except ValueError:
        scenario_limit = 12
    try:
        timeout = max(1, float(os.getenv("BREAKMYBOT_REQUEST_TIMEOUT_SECONDS", "15")))
    except ValueError:
        timeout = 15
    attempts = min({"quick": 1, "standard": 3, "deep": 8}[request.profile], scenario_limit)
    if request.method != "GET" and attempts > 1 and not request.allow_repeated_post:
        run.scenarios.append(ScenarioResult(scenario_id="repeat-safety", name="Repeat invocation safety gate",
            category="Safety", status="UNSUPPORTED", limitation="Repeated non-GET calls were not authorized because the endpoint may create side effects."))
        attempts = 1
    try:
        validate_target(str(request.target_url))
        headers = None
        if request.auth_env:
            token = os.getenv(request.auth_env)
            if not token:
                raise ValueError("Configured target credential environment variable is missing")
            value = f"{request.auth_scheme} {token}".strip()
            headers = {request.auth_header: value}
        for index in range(attempts):
            if cancel and cancel.is_set():
                run.status = "CANCELLED"
                break
            scenario_id = "baseline" if index == 0 else f"repeat-{index}"
            try:
                status, raw_body, duration, content_type = await invoke(str(request.target_url), request.endpoint_path,
                    request.method, request.payload, min(timeout, request.max_latency_seconds),
                    docker_mode=os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true", headers=headers)
                body = redact_text(raw_body, request.payload, (token,) if request.auth_env else ())
                success = 200 <= status < 300
                state = "PASSED" if success else "VERIFIED_FAILURE"
                severity = "high" if status >= 500 else "medium" if status >= 400 else "info"
                evidence = {"response_excerpt": body[:1200], "content_type": content_type,
                    "assertion": "HTTP status is 2xx", "assertion_passed": success}
                if request.expected_substring:
                    matched = request.expected_substring.casefold() in raw_body.casefold()
                    evidence["expected_substring"] = request.expected_substring
                    evidence["expected_substring_passed"] = matched
                    if not matched:
                        state = "VERIFIED_FAILURE"
                        severity = "high"
                run.scenarios.append(ScenarioResult(scenario_id=scenario_id,
                    name="Baseline invocation" if index == 0 else f"Read-only repeat {index}",
                    category="HTTP reliability", status=state, severity=severity,
                    duration_ms=round(duration, 2), http_status=status, evidence=evidence))
                store.append_event(run.id, {"type": "scenario.completed", "scenario_id": scenario_id, "status": state, "http_status": status})
                if "[response body truncated after 1 MB]" in raw_body:
                    contract_ok = None
                    contract_evidence = {"contract": "response body capped at 1 MB", "truncated": True}
                elif status == 204:
                    contract_ok = True
                    contract_evidence = {"contract": "HTTP 204 may have an empty body"}
                elif "json" in content_type.casefold():
                    try:
                        json.loads(raw_body)
                        contract_ok = bool(raw_body.strip())
                        contract_evidence = {"contract": "response declares JSON and parses as JSON", "parse_passed": True}
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        contract_ok = False
                        contract_evidence = {"contract": "response declares JSON and parses as JSON", "parse_passed": False}
                else:
                    contract_ok = bool(raw_body.strip())
                    contract_evidence = {"contract": "non-empty response body unless HTTP 204", "body_nonempty": contract_ok}
                run.scenarios.append(ScenarioResult(scenario_id=f"contract-{index}", name=f"Response contract {index+1}",
                    category="Response validity", status="INCONCLUSIVE" if contract_ok is None else "PASSED" if contract_ok else "VERIFIED_FAILURE",
                    severity="info" if contract_ok else "medium", duration_ms=round(duration, 2), http_status=status,
                    evidence=contract_evidence))
                if request.expected_substring:
                    expected_ok = request.expected_substring.casefold() in raw_body.casefold()
                    run.scenarios.append(ScenarioResult(scenario_id=f"expected-{index}", name=f"Expected response content {index+1}",
                        category="Output correctness", status="PASSED" if expected_ok else "VERIFIED_FAILURE",
                        severity="info" if expected_ok else "high", duration_ms=round(duration, 2), http_status=status,
                        evidence={"expected_substring": request.expected_substring, "matched": expected_ok}))
                if duration > request.max_latency_seconds * 1000:
                    run.scenarios.append(ScenarioResult(scenario_id=f"latency-{index}", name=f"Latency threshold {index+1}",
                        category="Latency", status="VERIFIED_FAILURE", severity="medium", duration_ms=round(duration, 2),
                        http_status=status, evidence={"threshold_ms": request.max_latency_seconds * 1000}))
                else:
                    run.scenarios.append(ScenarioResult(scenario_id=f"latency-{index}", name=f"Latency threshold {index+1}",
                        category="Latency", status="PASSED", duration_ms=round(duration, 2), http_status=status,
                        evidence={"threshold_ms": request.max_latency_seconds * 1000}))
            except httpx.TimeoutException:
                timeout_ms = min(timeout, request.max_latency_seconds) * 1000
                run.scenarios.append(ScenarioResult(scenario_id=scenario_id, name="Target invocation timeout",
                    category="HTTP reliability", status="VERIFIED_FAILURE", severity="high",
                    duration_ms=timeout_ms, evidence={"timeout_ms": timeout_ms,
                    "assertion": "target responded before timeout", "assertion_passed": False}))
                run.scenarios.append(ScenarioResult(scenario_id=f"latency-{index}", name=f"Latency threshold {index+1}",
                    category="Latency", status="VERIFIED_FAILURE", severity="high", duration_ms=timeout_ms,
                    evidence={"threshold_ms": request.max_latency_seconds * 1000}))
                store.append_event(run.id, {"type": "scenario.completed", "scenario_id": scenario_id, "status": "VERIFIED_FAILURE", "failure": "timeout"})
            except (httpx.HTTPError, ValueError) as exc:
                run.scenarios.append(ScenarioResult(scenario_id=scenario_id, name="Target invocation",
                    category="HTTP reliability", status="SETUP_ERROR", evidence={"error_type": type(exc).__name__},
                    limitation="Invocation failed before a reliable application result was captured."))
                store.append_event(run.id, {"type": "scenario.completed", "scenario_id": scenario_id, "status": "SETUP_ERROR"})
                run.status = "PARTIAL"
                break
        for name, category in [("Internal tool fault injection", "Tool reliability"),
             ("Memory lifecycle and access tests", "Memory integrity"), ("Security boundary invariants", "Security")]:
            run.scenarios.append(ScenarioResult(scenario_id=f"unsupported-{category.lower().replace(' ', '-')}",
                name=name, category=category, status="UNSUPPORTED",
                limitation="Black-box endpoint does not expose the required instrumentation or explicit test fixtures."))
        if request.memory_assessment:
            run.scenarios.extend(await memoryscope.evaluate(request, request.memory_assessment, cancel))
        elif not request.expected_substring:
            run.scenarios.append(ScenarioResult(scenario_id="unsupported-output-oracle", name="Semantic output correctness",
                category="Output correctness", status="UNSUPPORTED",
                limitation="No verified expected answer or grounded reference cases were supplied."))
        if cancel and cancel.is_set():
            run.status = "CANCELLED"
        run.scoring = scoring.calculate(run)
        if run.status not in {"CANCELLED", "PARTIAL"}:
            run.status = "COMPLETED"
        run.completed_at = datetime.now(timezone.utc)
        run.report_paths = reports.generate(run, store.data_dir() / "reports" / run.id)
        event_type = "run.cancelled" if run.status == "CANCELLED" else "run.completed"
        store.append_event(run.id, {"type": event_type, "phase": run.status, "status": run.status})
        store.save(run)
        return run
    except Exception as exc:
        run.status = "FAILED"
        run.error = f"{type(exc).__name__}: assessment execution failed"
        run.completed_at = datetime.now(timezone.utc)
        store.append_event(run.id, {"type": "run.failed", "phase": "FAILED"})
        store.save(run)
        return run
