from __future__ import annotations

import asyncio
import ipaddress
import os
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from urllib.parse import urlsplit, urlunsplit

from . import instrumentation, memoryscope, planner, reports, scoring, store
from .models import RunRecord, RunRequest, ScenarioResult
from .redaction import redact_text
from .target import docker_resolve_url, invoke, validate_target


async def _run_instrumented_scenarios(run: RunRecord, request: RunRequest, timeout: float,
                                      headers: dict[str, str] | None,
                                      cancel: asyncio.Event | None) -> list[ScenarioResult]:
    config = request.instrumented_assessment
    if config is None:
        return []
    token = os.getenv(config.auth_env)
    if not token:
        return [ScenarioResult(scenario_id="instrumentation-credentials", name="Instrumented target credentials",
            category="Tool reliability", status="SETUP_ERROR",
            limitation=f"Configured instrumentation credential {config.auth_env} is missing.")]
    if request.method != "GET" and not request.allow_repeated_post:
        return [ScenarioResult(scenario_id="instrumentation-repeat-safety", name="Instrumented invocation safety gate",
            category="Tool reliability", status="UNSUPPORTED",
            limitation="Instrumented fault scenarios add target invocations; authorize repeated non-GET calls explicitly.")]
    parsed = urlsplit(str(request.target_url))
    control_url = urlunsplit((parsed.scheme, parsed.netloc, instrumentation.CONTROL_PREFIX, "", ""))
    host = parsed.hostname or ""
    try:
        local_target = host.casefold() in {"localhost", "ip6-localhost"} or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local_target = False
    if parsed.scheme != "https" and not local_target:
        return [ScenarioResult(scenario_id="instrumentation-transport", name="Instrumentation transport security",
            category="Tool reliability", status="SETUP_ERROR",
            limitation="Instrumentation bearer credentials require HTTPS for non-loopback targets.")]
    if os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true":
        control_url = docker_resolve_url(control_url)
    validate_target(control_url)
    results: list[ScenarioResult] = []
    async with httpx.AsyncClient(timeout=min(timeout, 15), follow_redirects=False,
                                 trust_env=False) as client:
        try:
            supported = await instrumentation.verify_remote_instrumentation(client, control_url)
        except (httpx.HTTPError, ValueError, TypeError):
            supported = False
        if not supported:
            return [ScenarioResult(scenario_id="instrumentation-contract", name="Target instrumentation contract",
                category="Tool reliability", status="SETUP_ERROR",
                limitation="Target did not advertise the supported, session-scoped BreakMyBot instrumentation contract.")]
        for index, fault in enumerate(config.faults, 1):
            if cancel and cancel.is_set():
                break
            scenario_id = f"instrumented-tool-{index}"
            session_id = uuid4().hex
            fault_data = fault.model_dump(exclude={"expected_substring"})
            armed = False
            cleanup_ok = False
            observed_events: list[dict] = []
            status_code = None
            body = ""
            duration = None
            invocation_error = None
            try:
                await instrumentation.arm_remote_fault(client, control_url, token, session_id, fault_data)
                armed = True
                request_headers = dict(headers or {})
                request_headers["X-BreakMyBot-Session"] = session_id
                status_code, body, duration, _ = await invoke(str(request.target_url), request.endpoint_path,
                    request.method, request.payload, min(timeout, request.max_latency_seconds),
                    docker_mode=os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true",
                    headers=request_headers)
            except (httpx.HTTPError, ValueError) as exc:
                invocation_error = type(exc).__name__
            finally:
                try:
                    observed_events = await instrumentation.read_remote_events(client, control_url, token, session_id)
                except (httpx.HTTPError, ValueError, TypeError):
                    observed_events = []
                try:
                    cleanup_ok = await instrumentation.cleanup_remote_session(client, control_url, token, session_id)
                except (httpx.HTTPError, ValueError, TypeError):
                    cleanup_ok = False
            delivered = [event for event in observed_events if event.get("type") == "fault.injected"
                         and event.get("tool") == fault.tool and event.get("kind") == fault.kind]
            evidence = {"tool": fault.tool, "fault_kind": fault.kind, "fault_injected": bool(delivered),
                "injection_events": delivered, "cleanup_succeeded": cleanup_ok,
                "http_status": status_code, "response_excerpt": redact_text(body, request.payload)[:1200],
                "invocation_error": invocation_error}
            if not armed or not cleanup_ok:
                state = "SETUP_ERROR"
                limitation = "Fault session could not be safely armed, observed, and cleaned up."
            elif not delivered:
                state = "INCONCLUSIVE"
                limitation = "Target returned no matching tool-fault injection event; behavior was not attributed to an injected fault."
            elif invocation_error:
                state = "VERIFIED_FAILURE"
                limitation = "The target request failed while the named tool fault was confirmed injected."
            elif status_code is not None and not 200 <= status_code < 300:
                state = "VERIFIED_FAILURE"
                limitation = "The target returned a non-2xx response while the named tool fault was confirmed injected."
            elif fault.expected_substring is None:
                state = "INCONCLUSIVE"
                limitation = "Fault delivery was confirmed, but no explicit response invariant was supplied."
            else:
                passed = status_code is not None and 200 <= status_code < 300 and fault.expected_substring.casefold() in body.casefold()
                state = "PASSED" if passed else "VERIFIED_FAILURE"
                limitation = None if passed else "The response violated the supplied expected-substring invariant after confirmed fault injection."
                evidence["expected_substring"] = fault.expected_substring
                evidence["expected_substring_passed"] = passed
            result = ScenarioResult(scenario_id=scenario_id, name=f"Tool fault: {fault.kind} at {fault.tool}",
                category="Tool reliability", status=state,
                severity="high" if state == "VERIFIED_FAILURE" else "info",
                duration_ms=duration, http_status=status_code, evidence=evidence, limitation=limitation)
            results.append(result)
            store.append_event(run.id, {"type": "scenario.completed", "scenario_id": scenario_id,
                "status": state, "fault_injected": bool(delivered), "cleanup_succeeded": cleanup_ok})
    return results


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
        unsupported = [("Memory lifecycle and access tests", "Memory integrity"),
                       ("Security boundary invariants", "Security")]
        if request.instrumented_assessment is None:
            unsupported.insert(0, ("Internal tool fault injection", "Tool reliability"))
        for name, category in unsupported:
            run.scenarios.append(ScenarioResult(scenario_id=f"unsupported-{category.lower().replace(' ', '-')}",
                name=name, category=category, status="UNSUPPORTED",
                limitation="Black-box endpoint does not expose the required instrumentation or explicit test fixtures."))
        if request.instrumented_assessment is not None:
            if run.status == "PARTIAL":
                run.scenarios.append(ScenarioResult(scenario_id="instrumentation-baseline-failed",
                    name="Instrumented scenarios not started", category="Tool reliability", status="SETUP_ERROR",
                    limitation="Baseline target invocation did not complete; instrumented scenarios were not started."))
            else:
                run.scenarios.extend(await _run_instrumented_scenarios(run, request, timeout, headers, cancel))
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
