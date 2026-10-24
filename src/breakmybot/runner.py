from __future__ import annotations

import asyncio
import ipaddress
import os
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from urllib.parse import urlsplit, urlunsplit

from . import instrumentation, memory_adapter, memoryscope, planner, reports, scoring, semantic_judge, store
from .models import (InstrumentedAssessment, InstrumentedFaultChain, InstrumentedToolFault,
                     MemoryInvocation)
from .models import RunRecord, RunRequest, ScenarioResult
from .redaction import redact_text
from .target import docker_resolve_url, invoke, validate_target


def _phase(run_id: str, phase: str, **details: object) -> None:
    store.append_event(run_id, {"type": "run.phase", "phase": phase, **details})


def _scenario_event(run_id: str, result: ScenarioResult, completed: int) -> None:
    allowed = {"response_excerpt", "content_type", "assertion_passed", "fault_injected",
        "injection_events", "cleanup_succeeded", "http_status", "invocation_error",
        "expected_substring_passed", "threshold_ms", "timeout_ms", "error_type",
        "response_size_bytes",
        "contract", "body_nonempty", "parse_passed", "steps", "assertions",
        "concurrency", "request_statuses", "request_durations_ms", "error_types", "judge"}
    store.append_event(run_id, {"type": "scenario.completed", "scenario_id": result.scenario_id,
        "name": result.name, "category": result.category, "status": result.status,
        "severity": result.severity, "http_status": result.http_status,
        "duration_ms": result.duration_ms, "completed_scenarios": completed,
        "evidence": {key: value for key, value in result.evidence.items() if key in allowed}})


def _target_invocation_limitation(exc: Exception, *, docker_mode: bool) -> str:
    """Give actionable diagnostics without exposing request data or credentials."""
    if docker_mode and isinstance(exc, httpx.ConnectError):
        return ("The worker container could not connect to the target. For a host-local app, bind it to "
            "0.0.0.0 and use the host.docker.internal gateway; check the host firewall. "
            "For another Compose project, use a shared network and its service DNS name.")
    if docker_mode and isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout)):
        return ("The target timed out from the worker container. Confirm it is running, listening on the host's "
            "Docker-reachable interface, and reachable through the host firewall; increase the request timeout "
            "only if this latency is expected.")
    if isinstance(exc, httpx.ConnectError):
        return "The target connection failed before an HTTP response; verify its URL, port, process, and network access."
    if isinstance(exc, httpx.TimeoutException):
        return "The target did not respond before the configured request timeout."
    if isinstance(exc, ValueError):
        return ("Target request was rejected by BreakMyBot's URL or network policy. Review the target URL and "
            "explicit test-network allowlist.")
    return "Invocation failed before a reliable application result was captured."


async def _run_instrumented_scenarios(run: RunRecord, request: RunRequest, timeout: float,
                                      headers: dict[str, str] | None,
                                      cancel: asyncio.Event | None,
                                      scenario_id_prefix: str = "instrumented-tool") -> list[ScenarioResult]:
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
    try:
        validate_target(control_url)
    except ValueError:
        return [ScenarioResult(scenario_id="instrumentation-target-policy", name="Instrumentation target policy",
            category="Tool reliability", status="SETUP_ERROR",
            limitation="Instrumentation control URL failed the configured target network policy.")]
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
        cases = [(f"fault-{index}", [fault], fault.expected_substring)
                 for index, fault in enumerate(config.faults, 1)]
        cases.extend((chain.name, chain.faults, chain.expected_substring) for chain in config.chains)
        for index, (case_name, faults, expected_substring) in enumerate(cases, 1):
            if cancel and cancel.is_set():
                break
            scenario_id = f"{scenario_id_prefix}-{index}"
            store.append_event(run.id, {"type": "scenario.started", "scenario_id": scenario_id,
                "name": f"Tool fault scenario: {case_name}", "category": "Tool reliability"})
            session_id = uuid4().hex
            fault_data = [fault.model_dump(exclude={"expected_substring"}) for fault in faults]
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
            delivered = [event for event in observed_events if event.get("type") == "fault.injected"]
            provenance = [event for event in observed_events if event.get("type") in
                {"fault.injected", "fault.acknowledgment_lost"}]
            expected_faults = [{"tool": fault.tool, "kind": fault.kind} for fault in faults]
            observed_faults = [{"tool": event.get("tool"), "kind": event.get("kind")} for event in delivered]
            ordered_injections = _contains_ordered_faults(observed_faults, expected_faults)
            evidence = {"tool": faults[0].tool if len(faults) == 1 else None,
                "fault_kind": faults[0].kind if len(faults) == 1 else None,
                "faults": expected_faults, "fault_injected": ordered_injections,
                "injection_events": provenance, "cleanup_succeeded": cleanup_ok,
                "http_status": status_code, "response_excerpt": redact_text(body, request.payload)[:1200],
                "invocation_error": invocation_error}
            if not armed or not cleanup_ok:
                state = "SETUP_ERROR"
                limitation = "Fault session could not be safely armed, observed, and cleaned up."
            elif not ordered_injections:
                state = "INCONCLUSIVE"
                limitation = "Target did not confirm every configured fault in order; behavior was not attributed to the complete scenario."
            elif invocation_error:
                state = "VERIFIED_FAILURE"
                limitation = "The target request failed while the named tool fault was confirmed injected."
            elif status_code is not None and not 200 <= status_code < 300:
                state = "VERIFIED_FAILURE"
                limitation = "The target returned a non-2xx response while the named tool fault was confirmed injected."
            elif expected_substring is None:
                state = "INCONCLUSIVE"
                limitation = "Fault delivery was confirmed, but no explicit response invariant was supplied."
            else:
                passed = status_code is not None and 200 <= status_code < 300 and expected_substring.casefold() in body.casefold()
                state = "PASSED" if passed else "VERIFIED_FAILURE"
                limitation = None if passed else "The response violated the supplied expected-substring invariant after confirmed fault injection."
                evidence["expected_substring"] = expected_substring
                evidence["expected_substring_passed"] = passed
            result = ScenarioResult(scenario_id=scenario_id, name=f"Tool fault scenario: {case_name}",
                category="Tool reliability", status=state,
                severity="high" if state == "VERIFIED_FAILURE" else "info",
                duration_ms=duration, http_status=status_code, evidence=evidence, limitation=limitation)
            results.append(result)
            _scenario_event(run.id, result, len(run.scenarios) + len(results))
    return results


async def _run_planned_scenarios(run: RunRecord, request: RunRequest, timeout: float,
                                 headers: dict[str, str] | None,
                                 cancel: asyncio.Event | None) -> list[ScenarioResult]:
    if not request.execute_planned_scenarios or request.instrumented_assessment is None:
        return []
    selected: list[tuple[dict, planner.ScenarioProposal]] = []
    for row in run.planning.get("scenarios", []):
        if len(selected) >= request.max_planned_scenarios:
            break
        proposal, reason = planner.gate_generated_proposal(row, request)
        if proposal is None:
            row["execution_eligible"] = False
            row["execution_gate_reason"] = reason
            continue
        selected.append((row, proposal))

    results: list[ScenarioResult] = []
    for row, proposal in selected:
        if cancel and cancel.is_set():
            break
        execution = proposal.execution
        if execution is None:
            continue
        if execution.mode == "individual":
            faults = [InstrumentedToolFault(tool=execution.faults[0].tool,
                kind=execution.faults[0].kind, expected_substring=execution.expected_substring,
                delay_seconds=execution.faults[0].delay_seconds,
                every_n_calls=execution.faults[0].every_n_calls,
                on_call=execution.faults[0].on_call)]
            config = InstrumentedAssessment(auth_env=request.instrumented_assessment.auth_env,
                faults=faults)
        else:
            faults = [InstrumentedToolFault(tool=item.tool, kind=item.kind,
                delay_seconds=item.delay_seconds, every_n_calls=item.every_n_calls,
                on_call=item.on_call) for item in execution.faults]
            config = InstrumentedAssessment(auth_env=request.instrumented_assessment.auth_env,
                chains=[InstrumentedFaultChain(name=proposal.scenario_id, faults=faults,
                    expected_substring=execution.expected_substring)])
        planned_request = request.model_copy(update={"instrumented_assessment": config})
        scenario_id_prefix = f"planned-{proposal.scenario_id}"
        planned_results = await _run_instrumented_scenarios(run, planned_request, timeout, headers,
            cancel, scenario_id_prefix=scenario_id_prefix)
        results.extend(planned_results)
        row["executed"] = bool(planned_results)
        row["execution_scenario_ids"] = [item.scenario_id for item in planned_results]
        row["execution_statuses"] = [item.status for item in planned_results]
    return results


async def _run_memory_scenarios(request: RunRequest, timeout: float,
                                headers: dict[str, str] | None,
                                cancel: asyncio.Event | None) -> list[ScenarioResult]:
    config = request.instrumented_memory_assessment
    if config is None:
        return []
    categories = {
        "outdated_knowledge", "contradictory_information", "cross_session_consistency",
        "cross_agent_memory", "access_boundaries", "memory_poisoning",
    }
    category_names = {
        "outdated_knowledge": "Outdated memory",
        "contradictory_information": "Contradictory memory",
        "cross_session_consistency": "Cross-session consistency",
        "cross_agent_memory": "Cross-agent memory",
        "access_boundaries": "Memory access boundaries",
        "memory_poisoning": "Memory poisoning",
    }

    def score_category(category: str) -> str:
        return "Security" if category == "access_boundaries" else "Memory integrity"

    def memory_secrets(scenario) -> tuple[str, ...]:
        found: set[str] = set()

        def visit(value):
            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str) and len(value) >= 4:
                found.add(value)

        visit(scenario.seed)
        visit(scenario.scope)
        return tuple(found)
    memory_token = os.getenv(config.auth_env)
    if not memory_token:
        return [ScenarioResult(scenario_id="memory-instrumentation-credentials",
            name="Instrumented memory credentials", category="Memory integrity", status="SETUP_ERROR",
            limitation=f"Configured memory instrumentation credential {config.auth_env} is missing.")]
    if request.method != "GET" and not request.allow_repeated_post:
        return [ScenarioResult(scenario_id="memory-repeat-safety", name="Memory scenario invocation safety",
            category="Memory integrity", status="UNSUPPORTED",
            limitation="Memory scenarios add target calls; authorize repeated non-GET calls explicitly.")]
    parsed = urlsplit(str(request.target_url))
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    control_url = origin + memory_adapter.MEMORY_CONTROL_PREFIX
    combined_scenarios = [scenario for scenario in config.scenarios if scenario.faults]
    tool_token = (os.getenv(request.instrumented_assessment.auth_env)
        if combined_scenarios and request.instrumented_assessment else None)
    if combined_scenarios and not tool_token:
        return [ScenarioResult(scenario_id=f"memory-{scenario.id}", name=scenario.name,
            category=score_category(scenario.category), status="SETUP_ERROR",
            limitation="Combined memory/tool test requires the configured tool-instrumentation credential.")
            for scenario in combined_scenarios]
    tool_control_url = origin + instrumentation.CONTROL_PREFIX
    try:
        target_host = parsed.hostname or ""
        loopback = target_host.casefold() in {"localhost", "ip6-localhost"} or ipaddress.ip_address(target_host).is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme != "https" and not loopback:
        return [ScenarioResult(scenario_id="memory-instrumentation-transport",
            name="Memory instrumentation transport", category="Memory integrity", status="SETUP_ERROR",
            limitation="Memory instrumentation bearer credentials require HTTPS for non-loopback targets.")]
    if os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true":
        control_url = docker_resolve_url(control_url)
    try:
        validate_target(control_url)
    except ValueError:
        return [ScenarioResult(scenario_id="memory-instrumentation-target-policy",
            name="Memory instrumentation target policy", category="Memory integrity", status="SETUP_ERROR",
            limitation="The target memory control endpoint is not permitted by the configured target policy.")]

    auth = {"Authorization": f"Bearer {memory_token}"}
    available: set[str] = set()
    results: list[ScenarioResult] = []
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            manifest_response = await client.get(control_url + "/capabilities")
            manifest_response.raise_for_status()
            manifest = manifest_response.json()
            if not isinstance(manifest, dict):
                raise ValueError("Target memory capability manifest must be an object")
            raw_categories = manifest.get("categories")
            if manifest.get("contract") == memory_adapter.MEMORY_CONTRACT and manifest.get("snapshot_seed_restore") is True and isinstance(raw_categories, list):
                available = categories.intersection(item for item in raw_categories if isinstance(item, str))

            tool_supported = False
            if combined_scenarios and tool_token:
                try:
                    if os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true":
                        checked_tool_url = docker_resolve_url(tool_control_url)
                    else:
                        checked_tool_url = tool_control_url
                    tool_supported = await instrumentation.verify_remote_instrumentation(client,
                        checked_tool_url)
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    tool_supported = False

            seen: set[str] = set()
            for scenario in config.scenarios:
                seen.add(scenario.category)
                if cancel and cancel.is_set():
                    results.append(ScenarioResult(scenario_id=f"memory-{scenario.id}",
                        name=scenario.name, category="Memory integrity", status="SKIPPED",
                        limitation="Memory test cancelled before execution."))
                    continue
                if scenario.category not in available:
                    results.append(ScenarioResult(scenario_id=f"memory-{scenario.id}",
                        name=scenario.name, category=score_category(scenario.category), status="UNSUPPORTED",
                        limitation="NOT TESTED — target memory interface unavailable for this category."))
                    continue
                if scenario.faults and not tool_supported:
                    results.append(ScenarioResult(scenario_id=f"memory-{scenario.id}",
                        name=scenario.name, category=score_category(scenario.category), status="SETUP_ERROR",
                        limitation="Target did not advertise the required session-scoped tool-fault adapter."))
                    continue
                session_id = uuid4().hex
                started = asyncio.get_running_loop().time()
                seeded = False
                arm_attempted = False
                memory_cleanup_ok = True
                tool_arm_attempted = False
                tool_cleanup_ok = not bool(scenario.faults)
                tool_events: list[dict] = []
                state = "PASSED"
                status_code = None
                step_evidence: list[dict] = []
                try:
                    if scenario.faults:
                        tool_arm_attempted = True
                        resolved_tool_url = (docker_resolve_url(tool_control_url)
                            if os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true"
                            else tool_control_url)
                        await instrumentation.arm_remote_fault(client, resolved_tool_url,
                            tool_token, session_id, [fault.model_dump(exclude={"expected_substring"})
                                for fault in scenario.faults])
                    arm_attempted = True
                    memory_cleanup_ok = False
                    response = await client.put(f"{control_url}/sessions/{session_id}", headers=auth,
                        json={"scope": scenario.scope, "seed": scenario.seed})
                    response.raise_for_status()
                    seed_receipt = response.json()
                    if not isinstance(seed_receipt, dict):
                        raise ValueError("Target memory seed receipt must be an object")
                    seeded = seed_receipt.get("seeded") is True
                    if not seeded:
                        raise ValueError("Target did not confirm the memory seed")
                    steps = scenario.invocations or [MemoryInvocation(
                        endpoint_path=scenario.endpoint_path, payload=scenario.payload,
                        expected_literals=scenario.expected_literals,
                        forbidden_literals=scenario.forbidden_literals)]
                    for index, step in enumerate(steps, 1):
                        if cancel and cancel.is_set():
                            state = "SKIPPED"
                            break
                        step_headers = dict(headers or {})
                        extra_secrets = [memory_token]
                        if request.auth_env and os.getenv(request.auth_env):
                            extra_secrets.append(os.getenv(request.auth_env))
                        if step.auth_env:
                            case_token = os.getenv(step.auth_env)
                            if not case_token:
                                raise ValueError(f"Configured memory case credential {step.auth_env} is missing")
                            step_headers[step.auth_header] = f"{step.auth_scheme} {case_token}".strip()
                            extra_secrets.append(case_token)
                        if scenario.faults:
                            step_headers["X-BreakMyBot-Session"] = session_id
                            extra_secrets.append(tool_token)
                        status_code, body, duration, _content_type = await invoke(
                            str(request.target_url), step.endpoint_path, request.method, step.payload,
                            timeout, docker_mode=os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true",
                            headers=step_headers or None)
                        folded = body.casefold()
                        assertions = {
                            **{f"expected_literal_{number}": literal.casefold() in folded
                               for number, literal in enumerate(step.expected_literals, 1)},
                            **{f"forbidden_literal_{number}": literal.casefold() not in folded
                               for number, literal in enumerate(step.forbidden_literals, 1)},
                        }
                        passed = 200 <= status_code < 300 and all(assertions.values())
                        if not passed:
                            state = "VERIFIED_FAILURE"
                        sanitized = redact_text(body, step.payload,
                            tuple(extra_secrets) + memory_secrets(scenario))[:1200]
                        step_evidence.append({"step": index, "http_status": status_code,
                            "duration_ms": round(duration, 2), "assertions": assertions,
                            "response_excerpt": sanitized})
                except (httpx.HTTPError, ValueError, TypeError):
                    state = "SETUP_ERROR"
                finally:
                    if arm_attempted:
                        try:
                            cleanup = await client.delete(f"{control_url}/sessions/{session_id}", headers=auth)
                            cleanup_receipt = cleanup.json() if cleanup.is_success else {}
                            memory_cleanup_ok = (cleanup.is_success and isinstance(cleanup_receipt, dict)
                                and cleanup_receipt.get("cleaned_up") is True)
                        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                            memory_cleanup_ok = False
                    if tool_arm_attempted:
                        resolved_tool_url = (docker_resolve_url(tool_control_url)
                            if os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true"
                            else tool_control_url)
                        try:
                            tool_events = await instrumentation.read_remote_events(client,
                                resolved_tool_url, tool_token, session_id)
                        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                            tool_events = []
                        try:
                            tool_cleanup_ok = await instrumentation.cleanup_remote_session(client,
                                resolved_tool_url, tool_token, session_id)
                        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                            tool_cleanup_ok = False
                cleanup_ok = memory_cleanup_ok and tool_cleanup_ok
                if not cleanup_ok:
                    state = "SETUP_ERROR"
                expected_faults = [{"tool": fault.tool, "kind": fault.kind} for fault in scenario.faults]
                observed_faults = [{"tool": event.get("tool"), "kind": event.get("kind")}
                    for event in tool_events if event.get("type") == "fault.injected"]
                fault_injected = _contains_ordered_faults(observed_faults, expected_faults) if scenario.faults else None
                if cleanup_ok and scenario.faults and not fault_injected and state not in {"SETUP_ERROR", "SKIPPED"}:
                    state = "INCONCLUSIVE"
                evidence = {"steps": step_evidence, "cleanup_succeeded": cleanup_ok,
                    "memory_category": scenario.category}
                if scenario.faults:
                    evidence.update({"memory_cleanup_succeeded": memory_cleanup_ok,
                        "tool_cleanup_succeeded": tool_cleanup_ok, "fault_injected": fault_injected,
                        "faults": expected_faults,
                        "injection_events": [event for event in tool_events
                            if event.get("type") in {"fault.injected", "fault.acknowledgment_lost"}]})
                if status_code is not None:
                    evidence["http_status"] = status_code
                results.append(ScenarioResult(scenario_id=f"memory-{scenario.id}",
                    name=scenario.name, category=score_category(scenario.category), status=state,
                    severity="high" if state == "VERIFIED_FAILURE" else "info",
                    duration_ms=round((asyncio.get_running_loop().time() - started) * 1000, 2),
                    http_status=status_code, evidence=evidence,
                    limitation=("Target did not confirm every configured tool fault in order; combined behavior is inconclusive."
                        if state == "INCONCLUSIVE" and cleanup_ok else
                        None if state not in {"SETUP_ERROR", "SKIPPED"} else
                        "Target memory restoration was not confirmed; result excluded from scoring."
                        if not cleanup_ok else
                        "Memory test did not complete; no result was scored." if state == "SETUP_ERROR" else
                        "Memory test was cancelled after cleanup.")))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return [ScenarioResult(scenario_id=f"memory-unavailable-{category}",
                name=category_names[category], category=score_category(category), status="UNSUPPORTED",
                limitation="NOT TESTED — memory interface unavailable.") for category in sorted(categories)]
        return [ScenarioResult(scenario_id="memory-interface", name="Target memory interface",
            category="Memory integrity", status="SETUP_ERROR",
            limitation="The target memory adapter returned an unsuccessful control response.")]
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        return [ScenarioResult(scenario_id="memory-interface", name="Target memory interface",
            category="Memory integrity", status="SETUP_ERROR",
            limitation="The target did not provide the supported memory adapter contract.")]

    for category in sorted(categories - seen):
        status = "UNSUPPORTED"
        results.append(ScenarioResult(scenario_id=f"memory-unconfigured-{category}",
            name=category_names[category], category=score_category(category), status=status,
            limitation="NOT TESTED — no operator-supplied scenario for this memory category."))
    return results


def _contains_ordered_faults(observed: list[dict], expected: list[dict]) -> bool:
    cursor = 0
    for event in observed:
        if cursor < len(expected) and event == expected[cursor]:
            cursor += 1
    return cursor == len(expected)


async def _run_concurrency_probe(request: RunRequest, timeout: float,
                                 headers: dict[str, str] | None,
                                 cancel: asyncio.Event | None) -> ScenarioResult:
    if cancel and cancel.is_set():
        return ScenarioResult(scenario_id="concurrency", name="Concurrent request handling",
            category="HTTP reliability", status="SKIPPED",
            limitation="Concurrency probe cancelled before requests were sent.")
    started = asyncio.get_running_loop().time()
    tasks = [invoke(str(request.target_url), request.endpoint_path, request.method,
        request.payload, min(timeout, request.max_latency_seconds),
        docker_mode=os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true",
        headers=headers) for _ in range(request.concurrency_requests)]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    durations = [round(outcome[2], 2) for outcome in outcomes if not isinstance(outcome, BaseException)]
    statuses = [outcome[0] for outcome in outcomes if not isinstance(outcome, BaseException)]
    errors = [type(outcome).__name__ for outcome in outcomes if isinstance(outcome, BaseException)]
    within_latency = all(value <= request.max_latency_seconds * 1000 for value in durations)
    succeeded = (not errors and len(statuses) == request.concurrency_requests
        and all(200 <= status < 300 for status in statuses) and within_latency)
    failed_status = next((status for status in statuses if not 200 <= status < 300), None)
    result = ScenarioResult(scenario_id="concurrency", name="Concurrent request handling",
        category="HTTP reliability", status="PASSED" if succeeded else "VERIFIED_FAILURE",
        severity="info" if succeeded else "high" if errors or any(status >= 500 for status in statuses) else "medium",
        duration_ms=round((asyncio.get_running_loop().time() - started) * 1000, 2),
        http_status=failed_status if failed_status is not None else (
            statuses[0] if statuses and all(status == statuses[0] for status in statuses) else None),
        evidence={"concurrency": request.concurrency_requests, "request_statuses": statuses,
            "request_durations_ms": durations, "error_types": errors,
            "threshold_ms": request.max_latency_seconds * 1000,
            "assertion_passed": succeeded},
        limitation=None if succeeded else "One or more concurrent requests failed, returned non-2xx, or exceeded the latency threshold.")
    return result


async def _run_semantic_judge(run: RunRecord, request: RunRequest, timeout: float,
                              headers: dict[str, str] | None,
                              cancel: asyncio.Event | None) -> list[ScenarioResult]:
    config = request.semantic_judge
    if config is None:
        return []
    if not planner.provider_credentials_configured("judge"):
        provider_name = planner.configured_provider_name()
        run.semantic_judging = {"status": "unavailable", "provider": provider_name, "judged_cases": 0,
            "reason": "Judge credentials/model or provider URL are unavailable; no case target calls were made."}
        return [ScenarioResult(scenario_id=f"judge-{case.id}", name=case.name,
            category="Output correctness", status="SETUP_ERROR",
            limitation="Judge provider is not configured; target case invocation was skipped.")
            for case in config.cases]
    results: list[ScenarioResult] = []
    candidates: list[dict] = []
    case_by_id = {case.id: case for case in config.cases}
    token = os.getenv(request.auth_env) if request.auth_env else None
    _phase(run.id, "SEMANTIC_JUDGING", cases=len(config.cases))
    for case in config.cases:
        if cancel and cancel.is_set():
            completed_ids = {item.scenario_id for item in results}
            results.extend(ScenarioResult(scenario_id=f"judge-{pending.id}", name=pending.name,
                category="Output correctness", status="SKIPPED",
                limitation="Semantic judge case was not completed because the run was cancelled.")
                for pending in config.cases if f"judge-{pending.id}" not in completed_ids)
            run.semantic_judging = {"status": "cancelled", "provider": planner.configured_provider_name(),
                "judged_cases": 0}
            break
        store.append_event(run.id, {"type": "scenario.started", "scenario_id": f"judge-{case.id}",
            "name": case.name, "category": "Output correctness"})
        try:
            status, raw_body, duration, content_type = await invoke(str(request.target_url), request.endpoint_path,
                request.method, case.payload, min(timeout, request.max_latency_seconds),
                docker_mode=os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true", headers=headers)
            answer = semantic_judge.extract_answer(raw_body, content_type)
            answer = redact_text(answer, case.payload, (token,) if token else ())[:12000]
            base_evidence = {"http_status": status, "duration_ms": round(duration, 2),
                "content_type": content_type, "response_excerpt": answer[:1200],
                "response_size_bytes": len(raw_body.encode("utf-8"))}
            violations = []
            if not 200 <= status < 300:
                violations.append(f"Target returned HTTP {status} rather than 2xx")
            folded = answer.casefold()
            missing = [literal for literal in case.required_literals if literal.casefold() not in folded]
            forbidden = [literal for literal in case.forbidden_literals if literal.casefold() in folded]
            if missing:
                violations.append("Required literal checks failed")
            if forbidden:
                violations.append("Forbidden literal checks failed")
            if violations:
                results.append(ScenarioResult(scenario_id=f"judge-{case.id}", name=case.name,
                    category="Output correctness", status="VERIFIED_FAILURE", severity="medium",
                    duration_ms=round(duration, 2), http_status=status,
                    evidence={**base_evidence, "deterministic_failures": violations,
                        "missing_required_literals": missing, "present_forbidden_literals": forbidden,
                        "judge_called": False}))
            else:
                candidates.append({"case_id": case.id, "question": case.question,
                    "reference": case.reference, "expected": case.expected, "answer": answer})
                candidates[-1]["target_observation"] = base_evidence
        except (httpx.HTTPError, ValueError):
            results.append(ScenarioResult(scenario_id=f"judge-{case.id}", name=case.name,
                category="Output correctness", status="SETUP_ERROR",
                limitation="Target invocation failed before a grounded semantic judgment could be made."))

    if candidates and not (cancel and cancel.is_set()):
        provider_result = await semantic_judge.judge_answers(candidates)
        run.semantic_judging = {key: provider_result[key] for key in (
            "status", "provider", "requested_model", "selected_model", "actual_model",
            "provider_attempts", "usage", "provider_cost", "reason")
            if key in provider_result}
        run.semantic_judging["judged_cases"] = len(provider_result.get("judgments", []))
        judgments = {item["case_id"]: item for item in provider_result.get("judgments", [])}
        for case_id, case in case_by_id.items():
            candidate = next((item for item in candidates if item["case_id"] == case_id), None)
            if candidate is None:
                continue
            judgment = judgments.get(case_id)
            if provider_result.get("status") != "completed" or judgment is None:
                results.append(ScenarioResult(scenario_id=f"judge-{case.id}", name=case.name,
                    category="Output correctness", status="SETUP_ERROR",
                    evidence={"judge_provider": provider_result.get("provider", "configured provider")},
                    limitation=provider_result.get("reason", "The provider did not return a validated judgment.")))
                continue
            state = {"pass": "PASSED", "fail": "VERIFIED_FAILURE", "inconclusive": "INCONCLUSIVE"}[judgment["verdict"]]
            evidence = {**candidate["target_observation"], "judge": {key: judgment[key] for key in (
                "verdict", "confidence", "confidence_threshold", "explanation", "answer_quote", "reference_quote")}}
            results.append(ScenarioResult(scenario_id=f"judge-{case.id}", name=case.name,
                category="Output correctness", status=state,
                severity="medium" if state == "VERIFIED_FAILURE" else "info",
                duration_ms=candidate["target_observation"]["duration_ms"],
                http_status=candidate["target_observation"]["http_status"], evidence=evidence,
                limitation="LLM reference-grounded judgment; review the quoted evidence before acting."))
    elif candidates:
        completed_ids = {item.scenario_id for item in results}
        results.extend(ScenarioResult(scenario_id=f"judge-{case.id}", name=case.name,
            category="Output correctness", status="SKIPPED",
            limitation="Semantic judgment was not sent because the run was cancelled.")
            for case in config.cases if f"judge-{case.id}" not in completed_ids)
    elif results and run.semantic_judging == {}:
        run.semantic_judging = {"status": "deterministic_only", "provider": planner.configured_provider_name(),
            "judged_cases": 0, "reason": "Deterministic checks failed or target invocation was inconclusive; no provider call was needed."}
    for index, result in enumerate(results, 1):
        _scenario_event(run.id, result, len(run.scenarios) + index)
    return results


async def execute(run_id: str, request: RunRequest, cancel: asyncio.Event | None = None) -> RunRecord:
    run = store.get(run_id)
    if not run:
        raise KeyError(run_id)
    run.status = "RUNNING"
    store.save(run)
    store.append_event(run.id, {"type": "run.started", "phase": "DISCOVERING"})
    _phase(run.id, "DISCOVERING")
    if cancel and cancel.is_set():
        run.status = "CANCELLED"
        run.completed_at = datetime.now(timezone.utc)
        store.append_event(run.id, {"type": "run.cancelled", "phase": "CANCELLED"})
        store.save(run)
        return run
    run.planning = {"status": "not_requested", "provider": "none", "scenarios": []}
    _phase(run.id, "BASELINE")
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
            specs = [(scenario_id, "Baseline invocation" if index == 0 else f"Read-only repeat {index}", "HTTP reliability"),
                (f"contract-{index}", f"Response contract {index+1}", "Response validity"),
                (f"latency-{index}", f"Latency threshold {index+1}", "Latency")]
            if request.expected_substring:
                specs.append((f"expected-{index}", f"Expected response content {index+1}", "Output correctness"))
            for active_id, active_name, active_category in specs:
                store.append_event(run.id, {"type": "scenario.started", "scenario_id": active_id,
                    "name": active_name, "category": active_category})
            try:
                status, raw_body, duration, content_type = await invoke(str(request.target_url), request.endpoint_path,
                    request.method, request.payload, min(timeout, request.max_latency_seconds),
                    docker_mode=os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true", headers=headers)
                body = redact_text(raw_body, request.payload, (token,) if request.auth_env else ())
                success = 200 <= status < 300
                state = "PASSED" if success else "VERIFIED_FAILURE"
                severity = "high" if status >= 500 else "medium" if status >= 400 else "info"
                evidence = {"response_excerpt": body[:1200], "content_type": content_type,
                    "response_size_bytes": len(raw_body.encode("utf-8")),
                    "assertion": "HTTP status is 2xx", "assertion_passed": success}
                if request.expected_substring:
                    matched = request.expected_substring.casefold() in raw_body.casefold()
                    evidence["expected_substring_passed"] = matched
                    if not matched:
                        state = "VERIFIED_FAILURE"
                        severity = "high"
                run.scenarios.append(ScenarioResult(scenario_id=scenario_id,
                    name="Baseline invocation" if index == 0 else f"Read-only repeat {index}",
                    category="HTTP reliability", status=state, severity=severity,
                    duration_ms=round(duration, 2), http_status=status, evidence=evidence))
                _scenario_event(run.id, run.scenarios[-1], len(run.scenarios))
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
                _scenario_event(run.id, run.scenarios[-1], len(run.scenarios))
                if request.expected_substring:
                    expected_ok = request.expected_substring.casefold() in raw_body.casefold()
                    run.scenarios.append(ScenarioResult(scenario_id=f"expected-{index}", name=f"Expected response content {index+1}",
                        category="Output correctness", status="PASSED" if expected_ok else "VERIFIED_FAILURE",
                        severity="info" if expected_ok else "high", duration_ms=round(duration, 2), http_status=status,
                        evidence={"expected_substring": request.expected_substring, "matched": expected_ok}))
                    _scenario_event(run.id, run.scenarios[-1], len(run.scenarios))
                if duration > request.max_latency_seconds * 1000:
                    run.scenarios.append(ScenarioResult(scenario_id=f"latency-{index}", name=f"Latency threshold {index+1}",
                        category="Latency", status="VERIFIED_FAILURE", severity="medium", duration_ms=round(duration, 2),
                        http_status=status, evidence={"threshold_ms": request.max_latency_seconds * 1000}))
                else:
                    run.scenarios.append(ScenarioResult(scenario_id=f"latency-{index}", name=f"Latency threshold {index+1}",
                        category="Latency", status="PASSED", duration_ms=round(duration, 2), http_status=status,
                        evidence={"threshold_ms": request.max_latency_seconds * 1000}))
                _scenario_event(run.id, run.scenarios[-1], len(run.scenarios))
            except httpx.TimeoutException:
                timeout_ms = min(timeout, request.max_latency_seconds) * 1000
                run.scenarios.append(ScenarioResult(scenario_id=scenario_id, name="Target invocation timeout",
                    category="HTTP reliability", status="VERIFIED_FAILURE", severity="high",
                    duration_ms=timeout_ms, evidence={"timeout_ms": timeout_ms,
                    "assertion": "target responded before timeout", "assertion_passed": False}))
                run.scenarios.append(ScenarioResult(scenario_id=f"latency-{index}", name=f"Latency threshold {index+1}",
                    category="Latency", status="VERIFIED_FAILURE", severity="high", duration_ms=timeout_ms,
                    evidence={"threshold_ms": request.max_latency_seconds * 1000}))
                _scenario_event(run.id, run.scenarios[-2], len(run.scenarios)-1)
                _scenario_event(run.id, run.scenarios[-1], len(run.scenarios))
            except (httpx.HTTPError, ValueError) as exc:
                run.scenarios.append(ScenarioResult(scenario_id=scenario_id, name="Target invocation",
                    category="HTTP reliability", status="SETUP_ERROR", evidence={"error_type": type(exc).__name__},
                    limitation=_target_invocation_limitation(exc,
                        docker_mode=os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true")))
                _scenario_event(run.id, run.scenarios[-1], len(run.scenarios))
                run.status = "PARTIAL"
                break
        if request.use_nrouter_planner and not (cancel and cancel.is_set()):
            baseline = next((item for item in run.scenarios if item.scenario_id == "baseline"), None)
            if baseline is None or baseline.http_status is None:
                run.planning = {"status": "skipped", "provider": planner.configured_provider_name(),
                    "reason": "No HTTP baseline observation was available; no provider request was made.",
                    "scenarios": []}
            else:
                contract = next((item for item in run.scenarios if item.scenario_id == "contract-0"), None)
                profile = {"http_status": baseline.http_status,
                    "http_success": baseline.status == "PASSED",
                    "latency_ms": baseline.duration_ms,
                    "content_type": baseline.evidence.get("content_type"),
                    "response_size_bytes": baseline.evidence.get("response_size_bytes"),
                    "response_contract": contract.status if contract else "not_observed"}
                try:
                    run.planning = await planner.plan(request, baseline_profile=profile)
                except Exception:
                    run.planning = {"status": "error", "provider": planner.configured_provider_name(),
                        "reason": "Planner setup failed.", "scenarios": []}
            store.save(run)
            store.append_event(run.id, {"type": "planning.completed", "status": run.planning.get("status"),
                "scenario_count": len(run.planning.get("scenarios", []))})
        if request.semantic_judge is not None:
            if run.status == "PARTIAL":
                run.semantic_judging = {"status": "skipped", "reason": "Baseline invocation failed."}
                run.scenarios.extend(ScenarioResult(scenario_id=f"judge-{case.id}", name=case.name,
                    category="Output correctness", status="SETUP_ERROR",
                    limitation="Semantic case was not invoked because the baseline failed.")
                    for case in request.semantic_judge.cases)
            else:
                run.scenarios.extend(await _run_semantic_judge(run, request, timeout, headers, cancel))
        if request.concurrency_requests:
            if run.status == "PARTIAL":
                concurrency_result = ScenarioResult(scenario_id="concurrency",
                    name="Concurrent request handling", category="HTTP reliability", status="SETUP_ERROR",
                    limitation="Concurrency probe was not started because the baseline invocation failed.")
            else:
                _phase(run.id, "CONCURRENCY", requests=request.concurrency_requests)
                concurrency_result = await _run_concurrency_probe(request, timeout, headers, cancel)
            run.scenarios.append(concurrency_result)
            _scenario_event(run.id, concurrency_result, len(run.scenarios))
        memory_categories = {item.category for item in request.instrumented_memory_assessment.scenarios} \
            if request.instrumented_memory_assessment else set()
        unsupported = [] if request.instrumented_memory_assessment is None or "access_boundaries" in memory_categories else [
            ("Security boundary invariants", "Security")]
        if request.instrumented_assessment is None:
            unsupported.insert(0, ("Internal tool fault injection", "Tool reliability"))
        for name, category in unsupported:
            run.scenarios.append(ScenarioResult(scenario_id=f"unsupported-{category.lower().replace(' ', '-')}",
                name=name, category=category, status="UNSUPPORTED",
                limitation="Black-box endpoint does not expose the required instrumentation or explicit test fixtures."))
        if request.instrumented_memory_assessment is None:
            memory_categories = {
                "outdated_knowledge": "Outdated memory",
                "contradictory_information": "Contradictory memory",
                "cross_session_consistency": "Cross-session consistency",
                "cross_agent_memory": "Cross-agent memory",
                "access_boundaries": "Memory access boundaries",
                "memory_poisoning": "Memory poisoning",
            }
            for category_key, name in memory_categories.items():
                run.scenarios.append(ScenarioResult(scenario_id=f"unsupported-memory-{category_key}",
                    name=name, category="Security" if category_key == "access_boundaries" else "Memory integrity",
                    status="UNSUPPORTED", limitation="NOT TESTED — memory interface unavailable."))
        if request.instrumented_assessment is not None:
            if run.status == "PARTIAL":
                run.scenarios.append(ScenarioResult(scenario_id="instrumentation-baseline-failed",
                    name="Instrumented scenarios not started", category="Tool reliability", status="SETUP_ERROR",
                    limitation="Baseline target invocation did not complete; instrumented scenarios were not started."))
            else:
                _phase(run.id, "INSTRUMENTED_FAULTS")
                if request.execute_approved_instrumented_scenarios:
                    run.scenarios.extend(await _run_instrumented_scenarios(
                        run, request, timeout, headers, cancel))
                if request.execute_planned_scenarios:
                    _phase(run.id, "AI_PLANNED_SCENARIOS",
                        max_scenarios=request.max_planned_scenarios)
                    run.scenarios.extend(await _run_planned_scenarios(
                        run, request, timeout, headers, cancel))
        if request.memory_assessment:
            _phase(run.id, "MEMORY_EVALUATION")
            run.scenarios.extend(await memoryscope.evaluate(request, request.memory_assessment, cancel))
        elif not request.expected_substring:
            run.scenarios.append(ScenarioResult(scenario_id="unsupported-output-oracle", name="Semantic output correctness",
                category="Output correctness", status="UNSUPPORTED",
                limitation="No verified expected answer or grounded reference cases were supplied."))
        if request.instrumented_memory_assessment is not None:
            _phase(run.id, "INSTRUMENTED_MEMORY")
            if run.status == "PARTIAL":
                run.scenarios.extend([ScenarioResult(scenario_id=f"memory-{case.id}",
                    name=case.name, category="Memory integrity", status="SETUP_ERROR",
                    limitation="Memory scenario not started because the baseline invocation failed.")
                    for case in request.instrumented_memory_assessment.scenarios])
            else:
                memory_results = await _run_memory_scenarios(request, timeout, headers, cancel)
                run.scenarios.extend(memory_results)
                for result in memory_results:
                    _scenario_event(run.id, result, len(run.scenarios))
        if cancel and cancel.is_set():
            run.status = "CANCELLED"
        _phase(run.id, "SCORING")
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
