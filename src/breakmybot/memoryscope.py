from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from .models import MemoryAssessment, RunRequest, ScenarioResult
from .redaction import redact_evidence
from .target import docker_resolve_url, validate_target


def supports_external_answer_evaluation(manifest: dict) -> bool:
    return (manifest.get("contract") == "memoryscope-answer-evaluation-v1"
            and manifest.get("capabilities", {}).get("external_agent_answer_evaluation") is True)


def _agent_url(request: RunRequest, agent_path: str) -> str:
    parsed = urlsplit(str(request.target_url))
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    target = origin + agent_path
    if os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true":
        target = docker_resolve_url(target)
    validate_target(target)
    return target


def _map_results(result: dict, config: MemoryAssessment, local_secrets: tuple[str, ...]) -> list[ScenarioResult]:
    rows = result.get("results", [])
    if not isinstance(rows, list) or len(rows) > len(config.cases):
        return [ScenarioResult(scenario_id="memoryscope-result-contract", name="MemoryScope result contract",
            category="Output correctness", status="SETUP_ERROR",
            limitation="MemoryScope returned an invalid number or shape of case results; no case outcomes were imported.")]

    mapped: list[ScenarioResult] = []
    for index, case in enumerate(config.cases):
        if index >= len(rows):
            state = "SKIPPED" if result.get("status") == "cancelled" else "INCONCLUSIVE"
            mapped.append(ScenarioResult(scenario_id=f"memoryscope-{index+1}", name=case.name,
                category="Output correctness", status=state,
                limitation=("MemoryScope evaluation was cancelled before this case returned evidence."
                    if state == "SKIPPED" else "MemoryScope ended before this case returned evidence.")))
            continue
        item = rows[index]
        if not isinstance(item, dict):
            mapped.append(ScenarioResult(scenario_id=f"memoryscope-{index+1}", name=case.name,
                category="Output correctness", status="SETUP_ERROR",
                limitation="MemoryScope returned a malformed case result."))
            continue
        source_status = item.get("status")
        state = {"pass": "PASSED", "fail": "VERIFIED_FAILURE", "inconclusive": "INCONCLUSIVE",
                 "error": "SETUP_ERROR"}.get(source_status, "INCONCLUSIVE")
        evidence = {key: item.get(key) for key in ("answer", "checks", "judge", "answer_latency_ms") if key in item}
        evidence = redact_evidence(evidence, local_secrets)
        mapped.append(ScenarioResult(scenario_id=f"memoryscope-{index+1}",
            name=item.get("name", case.name), category="Output correctness", status=state,
            severity="medium" if state == "VERIFIED_FAILURE" else "info",
            duration_ms=item.get("answer_latency_ms"), evidence=evidence,
            limitation=item.get("error")))

    if result.get("status") == "failed":
        mapped.append(ScenarioResult(scenario_id="memoryscope-run-failed", name="MemoryScope evaluation status",
            category="Output correctness", status="SETUP_ERROR",
            limitation="MemoryScope reported the evaluation run as failed; inspect the MemoryScope service logs."))
    return mapped


async def evaluate(request: RunRequest, config: MemoryAssessment, cancel: asyncio.Event | None = None) -> list[ScenarioResult]:
    """Call the user's separately running MemoryScope API; its server owns nRouter credentials."""
    if not config.send_to_nrouter:
        return [ScenarioResult(scenario_id="memory-consent", name="Grounded answer evaluation",
            category="Output correctness", status="SKIPPED",
            limitation="References and target answers were not authorized for transmission to the nRouter judge.")]
    api_base = os.getenv("MEMORYSCOPE_API_URL", "").strip()
    if not api_base:
        return [ScenarioResult(scenario_id="memoryscope-config", name="MemoryScope integration",
            category="Output correctness", status="SETUP_ERROR",
            limitation="Set MEMORYSCOPE_API_URL to the separately running MemoryScope API.")]
    docker_mode = os.getenv("BREAKMYBOT_DOCKER_MODE", "false").lower() == "true"
    if docker_mode:
        api_base = docker_resolve_url(api_base)
    try:
        validate_target(api_base)
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            capabilities_response = await client.get(api_base.rstrip("/") + "/api/beta/capabilities")
            capabilities_response.raise_for_status()
            capabilities = capabilities_response.json()
            if not supports_external_answer_evaluation(capabilities):
                return [ScenarioResult(scenario_id="memoryscope-contract", name="MemoryScope integration contract",
                    category="Output correctness", status="SETUP_ERROR",
                    limitation="MemoryScope does not advertise the supported external-agent answer-evaluation contract.")]
            settings_response = await client.get(api_base.rstrip("/") + "/api/beta/settings")
            settings_response.raise_for_status()
            settings = settings_response.json()
            if not settings.get("judge_configured"):
                return [ScenarioResult(scenario_id="memoryscope-judge", name="MemoryScope judge readiness",
                    category="Output correctness", status="SETUP_ERROR",
                    limitation="MemoryScope reports that NROUTER_API_KEY or NROUTER_JUDGE_MODEL is not configured.")]
            payload = {
                "agent_name": request.application_name or "BreakMyBot target",
                "endpoint": _agent_url(request, config.agent_path),
                "auth_env": config.auth_env,
                "send_to_judge": True,
                "cases": [case.model_dump() for case in config.cases],
            }
            started = await client.post(api_base.rstrip("/") + "/api/beta/runs", json=payload)
            started.raise_for_status()
            run_id = started.json()["id"]
            deadline = time.monotonic() + float(os.getenv("BREAKMYBOT_MEMORY_TIMEOUT_SECONDS", "900"))
            while time.monotonic() < deadline:
                if cancel and cancel.is_set():
                    await client.post(api_base.rstrip("/") + f"/api/beta/runs/{run_id}/cancel")
                    return [ScenarioResult(scenario_id="memoryscope-cancelled", name="Grounded answer evaluation",
                        category="Output correctness", status="SKIPPED",
                        limitation="MemoryScope evaluation was cancelled before its results were imported.")]
                await asyncio.sleep(1)
                detail = await client.get(api_base.rstrip("/") + f"/api/beta/runs/{run_id}")
                detail.raise_for_status()
                result = detail.json()
                if result.get("status") in {"completed", "failed", "cancelled"}:
                    local_secrets = tuple(value for value in (os.getenv(config.auth_env) if config.auth_env else None,
                        os.getenv("NROUTER_API_KEY")) if value)
                    return _map_results(result, config, local_secrets)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return [ScenarioResult(scenario_id="memoryscope-error", name="MemoryScope integration",
            category="Output correctness", status="SETUP_ERROR",
            limitation="MemoryScope could not complete the configured external-agent evaluation; inspect its local service status and allowlists.")]
    return [ScenarioResult(scenario_id="memoryscope-timeout", name="MemoryScope evaluation timeout",
        category="Output correctness", status="SETUP_ERROR",
        limitation="MemoryScope did not finish before BREAKMYBOT_MEMORY_TIMEOUT_SECONDS elapsed.")]
