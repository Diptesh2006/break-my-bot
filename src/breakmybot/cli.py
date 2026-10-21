from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import planner

app = typer.Typer(help="Test the observable reliability of a locally running AI application.")
console = Console()
API = os.getenv("BREAKMYBOT_API_URL", "http://127.0.0.1:8000").rstrip("/")


def api_request(method: str, path: str, **kwargs):
    try:
        response = httpx.request(method, API + path, timeout=20, **kwargs)
    except httpx.HTTPError as exc:
        console.print(f"[red]BreakMyBot API is unreachable at {API}.[/red]")
        raise typer.Exit(code=2) from exc
    if response.is_error:
        console.print(f"[red]BreakMyBot API error {response.status_code}:[/red] {response.text[:500]}")
        raise typer.Exit(code=2)
    return response.json() if "json" in response.headers.get("content-type", "") else response


@app.command()
def init():
    """Create a local .env file from the example if one is not present."""
    path = Path(".env")
    if path.exists():
        console.print(".env already exists")
        return
    example = Path(__file__).resolve().parents[2] / ".env.example"
    if example.exists():
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        console.print("Created .env. Review the planner provider settings and allowed hosts.")
    else:
        console.print("Run from the BreakMyBot repository root to create .env.")


@app.command()
def doctor():
    """Check API connectivity."""
    result = api_request("GET", "/api/health")
    console.print(f"[green]OK[/green] BreakMyBot API reachable ({result['status']}) at {API}")


@app.command()
def test(
    url: str | None = typer.Option(None, "--url", help="Base URL of the AI application"),
    endpoint: str | None = typer.Option(None, "--endpoint", help="Invocation path, e.g. /api/chat"),
    payload_json: str | None = typer.Option(None, "--payload-json", help="JSON body for the selected endpoint"),
    profile: str = typer.Option("standard", help="quick, standard, or deep"),
    expected: str | None = typer.Option(None, "--expected", help="Optional exact substring expected in responses"),
    allow_repeated_post: bool = typer.Option(False, "--allow-repeated-post", help="Authorize repeated non-GET requests to the selected test endpoint"),
    concurrency_requests: int = typer.Option(0, "--concurrency-requests", min=0, max=5,
        help="Opt in to 2-5 simultaneous baseline requests; 0 disables the probe"),
    auth_env: str | None = typer.Option(None, "--auth-env", help="Environment variable name containing the target bearer token"),
    memory_cases: Path | None = typer.Option(None, "--memory-cases", help="JSON array of approved MemoryScope reference cases"),
    memory_agent_path: str | None = typer.Option(None, "--memory-agent-path", help="SSE chat path on the same target origin"),
    memory_auth_env: str | None = typer.Option(None, "--memory-auth-env", help="MemoryScope agent auth environment variable name"),
    memory_scenarios: Path | None = typer.Option(None, "--memory-scenarios", help="JSON scenarios for the target's opt-in real-memory adapter"),
    memory_control_auth_env: str = typer.Option("BREAKMYBOT_MEMORY_TOKEN", "--memory-control-auth-env", help="Environment variable containing the target memory-control token"),
    instrumented_faults: Path | None = typer.Option(None, "--instrumented-faults", help="JSON file of opt-in tool fault scenarios"),
    instrumentation_auth_env: str = typer.Option("BREAKMYBOT_INSTRUMENTATION_TOKEN", "--instrumentation-auth-env", help="Environment variable containing the target instrumentation control token"),
    plan_with_nrouter: bool = typer.Option(False, "--plan-with-ai", "--plan-with-nrouter",
        help="Opt in to send endpoint metadata to the configured AI provider (nRouter by default)"),
    execute_planned_scenarios: bool = typer.Option(False, "--execute-planned-scenarios",
        help="Explicitly authorize bounded AI-proposed faults from the supplied instrumentation allowlist"),
    max_planned_scenarios: int = typer.Option(3, "--max-planned-scenarios", min=1, max=5,
        help="Maximum number of AI-proposed scenarios to execute (1-5)"),
):
    """Discover and run bounded black-box HTTP checks."""
    if profile not in {"quick", "standard", "deep"}:
        raise typer.BadParameter("profile must be quick, standard, or deep")
    if concurrency_requests == 1:
        raise typer.BadParameter("concurrency-requests must be 0 (disabled) or between 2 and 5")
    console.print("[bold]BREAKMYBOT[/bold]\nBreak your AI so your users don't have to.\n")
    url = url or Prompt.ask("AI application URL")
    discovered = api_request("POST", "/api/targets/discover", json={"url": url})
    if not discovered["reachable"]:
        console.print("[red]The target could not be reached from the BreakMyBot execution environment.[/red]")
        for item in discovered["diagnostics"]:
            console.print(f"  {item}")
        console.print("For a host application in Docker, bind it to 0.0.0.0 and allow host.docker.internal access.")
        raise typer.Exit(code=3)
    console.print("[green]OK[/green] Application reachable from BreakMyBot")
    if discovered["openapi_found"]:
        console.print("[green]OK[/green] API specification detected")
    for health_endpoint in discovered.get("health_endpoints", []):
        console.print(f"Health check: {health_endpoint}")
    choices = discovered["endpoints"]
    selected = next((row for row in choices if endpoint and row["path"] == endpoint), None)
    if not endpoint and len(choices) == 1:
        selected = choices[0]
    elif not endpoint and choices:
        table = Table("#", "Method", "Path", "Response", "Description")
        for index, row in enumerate(choices, 1):
            response_kind = "streaming" if row.get("streaming") else "standard"
            table.add_row(str(index), row["method"], row["path"], response_kind,
                          row["description"][:60])
        console.print(table)
        index = int(Prompt.ask("Choose endpoint number", default="1"))
        if not 1 <= index <= len(choices):
            raise typer.BadParameter("endpoint choice is out of range")
        selected = choices[index - 1]
    if selected:
        endpoint, method, template = selected["path"], selected["method"], selected["payload_template"]
        response_kind = " (streaming response)" if selected.get("streaming") else ""
        console.print(f"Selected endpoint: {method} {endpoint}{response_kind}")
    else:
        endpoint = endpoint or Prompt.ask("Invocation endpoint path (required; no suitable OpenAPI operation found)")
        method = Prompt.ask("HTTP method", choices=["GET", "POST", "PUT", "PATCH"], default="POST")
        template = {}
    if payload_json:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter("payload-json must be valid JSON") from exc
    elif template:
        payload = template
        console.print(f"Schema-derived sample payload: {json.dumps(payload, ensure_ascii=False)}")
    elif method == "GET":
        payload = {}
    else:
        payload_text = Prompt.ask("Request JSON payload (schema unavailable; provide a valid body)")
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter("payload JSON must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise typer.BadParameter("payload JSON must be an object")
    if concurrency_requests and method != "GET" and not allow_repeated_post:
        raise typer.BadParameter("non-GET concurrency probes require --allow-repeated-post")
    if concurrency_requests and not Confirm.ask(
            f"Authorize {concurrency_requests} simultaneous requests to this target? This may increase load.",
            default=False):
        concurrency_requests = 0
    if method != "GET" and not Confirm.ask("Confirm endpoint and payload are safe for a test invocation", default=False):
        console.print("Cancelled before creating the assessment.")
        raise typer.Exit(code=4)
    console.print("The assessment worker will perform the baseline invocation and record its response.")
    if allow_repeated_post and not Confirm.ask("Confirm repeated calls cannot create unwanted side effects", default=False):
        raise typer.Exit(code=4)
    instrumented_assessment = None
    if instrumented_faults:
        if method != "GET" and not allow_repeated_post:
            raise typer.BadParameter("Instrumented fault scenarios add target calls; use --allow-repeated-post and confirm safety")
        try:
            fault_cases = json.loads(instrumented_faults.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise typer.BadParameter("instrumented-faults must be a readable JSON array") from exc
        if not isinstance(fault_cases, list):
            raise typer.BadParameter("instrumented-faults must contain a JSON array")
        singles, chains = [], []
        for case in fault_cases:
            (chains if isinstance(case, dict) and "faults" in case else singles).append(case)
        if len(singles) > 10 or len(chains) > 5 or not (singles or chains):
            raise typer.BadParameter("instrumented-faults allows up to 10 faults and 5 chains")
        if not Confirm.ask("Arm the target's opt-in BreakMyBot tool-fault controller for these tests?", default=False):
            console.print("Skipping instrumented scenarios; continuing with black-box checks only.")
            instrumented_assessment = None
        else:
            instrumented_assessment = {"auth_env": instrumentation_auth_env,
                "faults": singles, "chains": chains}
    memory_assessment = None
    if memory_cases:
        try:
            cases = json.loads(memory_cases.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise typer.BadParameter("memory-cases must be a readable JSON array") from exc
        if not isinstance(cases, list):
            raise typer.BadParameter("memory-cases must contain a JSON array")
        consent = Confirm.ask("MemoryScope will send these references and the target answers to the configured nRouter judge. Consent?", default=False)
        memory_assessment = {"agent_path": memory_agent_path or endpoint, "cases": cases,
            "send_to_nrouter": consent, "auth_env": memory_auth_env}
    instrumented_memory_assessment = None
    execute_memory_scenarios = False
    if memory_scenarios:
        if method != "GET" and not allow_repeated_post:
            raise typer.BadParameter("memory scenarios add target calls; use --allow-repeated-post and confirm safety")
        try:
            scenario_cases = json.loads(memory_scenarios.read_text(encoding="utf-8"))
            if not isinstance(scenario_cases, list):
                raise ValueError("expected an array")
            from .models import InstrumentedMemoryAssessment
            validated = InstrumentedMemoryAssessment(auth_env=memory_control_auth_env,
                scenarios=scenario_cases)
        except (OSError, json.JSONDecodeError, ValueError, ValidationError) as exc:
            raise typer.BadParameter("memory-scenarios must be a valid array matching the documented contract") from exc
        combined_faults = [fault for case in validated.scenarios for fault in case.faults]
        if combined_faults:
            if instrumented_assessment is None:
                raise typer.BadParameter("combined memory/tool cases require an accepted --instrumented-faults allowlist")
            from .models import InstrumentedAssessment
            try:
                approved_config = InstrumentedAssessment.model_validate(instrumented_assessment)
            except ValidationError as exc:
                raise typer.BadParameter("--instrumented-faults does not match its validated schema") from exc
            approved = [fault.model_dump(exclude={"expected_substring"}) for fault in approved_config.faults]
            approved.extend(fault.model_dump(exclude={"expected_substring"})
                for chain in approved_config.chains for fault in chain.faults)
            if any(fault.model_dump(exclude={"expected_substring"}) not in approved
                   for fault in combined_faults):
                raise typer.BadParameter("combined memory/tool cases may use only approved --instrumented-faults")
        if Confirm.ask("Seed the target's real memory for these approved scenarios and attempt restoration after each test? The target may have side effects.", default=False):
            instrumented_memory_assessment = validated.model_dump(mode="json")
            execute_memory_scenarios = True
    if plan_with_nrouter and not Confirm.ask(
        f"Send target name, endpoint/method and payload field names to {planner.configured_provider_name()} for scenario planning? "
        "When generated execution is enabled, the approved tool/fault pairs and response oracles are also sent.",
        default=False,
    ):
        plan_with_nrouter = False
    if execute_planned_scenarios:
        if not plan_with_nrouter:
            raise typer.BadParameter("--execute-planned-scenarios requires --plan-with-ai (or --plan-with-nrouter) and its consent")
        if instrumented_assessment is None:
            raise typer.BadParameter("--execute-planned-scenarios requires an accepted --instrumented-faults allowlist")
        if method != "GET" and not allow_repeated_post:
            raise typer.BadParameter("planned fault scenarios require --allow-repeated-post and its safety confirmation")
        if not Confirm.ask(
            f"Execute up to {max_planned_scenarios} AI-generated scenarios against only this endpoint, "
            "using only the approved tool/fault pairs and expected-response oracles? The target may have side effects.",
            default=False,
        ):
            execute_planned_scenarios = False
    created = api_request("POST", "/api/runs", json={"target_url": url, "endpoint_path": endpoint,
        "method": method, "payload": payload, "profile": profile, "application_name": discovered.get("application_name"),
        "expected_substring": expected, "allow_repeated_post": allow_repeated_post,
        "concurrency_requests": concurrency_requests, "auth_env": auth_env,
        "memory_assessment": memory_assessment, "instrumented_assessment": instrumented_assessment,
        "instrumented_memory_assessment": instrumented_memory_assessment,
        "execute_memory_scenarios": execute_memory_scenarios,
        "use_nrouter_planner": plan_with_nrouter,
        "execute_planned_scenarios": execute_planned_scenarios,
        "max_planned_scenarios": max_planned_scenarios})
    run_id = created["run_id"]
    console.print(f"Assessment started: {run_id}")
    while True:
        run = api_request("GET", f"/api/runs/{run_id}")
        if run["status"] not in {"CREATED", "RUNNING"}:
            break
        time.sleep(0.75)
    if run["status"] not in {"COMPLETED", "PARTIAL"}:
        console.print(f"[red]Assessment {run['status'].lower()}[/red]")
        raise typer.Exit(code=3)
    score = run["scoring"].get("overall_score")
    console.print(f"\n[bold]BREAKMYBOT - TEST {run['status']}[/bold]")
    console.print(f"Observed reliability score: {score}/100" if score is not None else "Observed reliability score: Not established")
    console.print(f"Coverage: {run['scoring'].get('coverage_percent', 0)}%")
    executed_plans = sum(bool(item.get("executed")) for item in run.get("planning", {}).get("scenarios", []))
    execution_note = f"{executed_plans} generated scenarios executed" if execute_planned_scenarios else "proposals not executed"
    console.print(f"Scenario planner: {run.get('planning', {}).get('status', 'not requested')} ({len(run.get('planning', {}).get('scenarios', []))} proposals; {execution_note})")
    counts = run["scoring"]["scenario_counts"]
    console.print(f"Passed: {counts['PASSED']} | Failed: {counts['VERIFIED_FAILURE']} | Unsupported: {counts['UNSUPPORTED']}")
    console.print(run["scoring"]["warning"])
    console.print("Reports:")
    for kind, path in run["report_paths"].items():
        console.print(f"  {kind}: {path}")
    console.print(f"API: {API}")


@app.command()
def runs():
    """List recent assessments."""
    table = Table("Run", "Status", "Target", "Score", "Created")
    for run in api_request("GET", "/api/runs"):
        score = run.get("scoring", {}).get("overall_score")
        table.add_row(run["id"], run["status"], run["target_url"], str(score) if score is not None else "N/A", run["created_at"][:19])
    console.print(table)


@app.command()
def status(run_id: str):
    """Show one assessment and its observed scenario results."""
    console.print_json(json.dumps(api_request("GET", f"/api/runs/{run_id}"), indent=2))


@app.command()
def report(latest: bool = typer.Option(False, "--latest"), run_id: str | None = typer.Option(None, "--run")):
    """Print report artifact locations."""
    if latest:
        items = api_request("GET", "/api/runs?limit=1")
        if not items:
            console.print("No assessments found")
            raise typer.Exit(code=1)
        run_id = items[0]["id"]
    if not run_id:
        raise typer.BadParameter("Use --latest or --run RUN_ID")
    record = api_request("GET", f"/api/runs/{run_id}")
    for kind, path in record.get("report_paths", {}).items():
        console.print(f"{kind}: {path}")


@app.command()
def export(
    latest: bool = typer.Option(False, "--latest"),
    run_id: str | None = typer.Option(None, "--run"),
    format: str = typer.Option("pdf", "--format", help="html, pdf, or json"),
    output: Path | None = typer.Option(None, "--output", help="Destination file (defaults to report.<format>)"),
):
    """Download a report artifact from the local API."""
    if format not in {"html", "pdf", "json"}:
        raise typer.BadParameter("format must be html, pdf, or json")
    if latest:
        items = api_request("GET", "/api/runs?limit=1")
        if not items:
            console.print("No assessments found")
            raise typer.Exit(code=1)
        run_id = items[0]["id"]
    if not run_id:
        raise typer.BadParameter("Use --latest or --run RUN_ID")
    response = api_request("GET", f"/api/runs/{run_id}/report.{format}")
    destination = output or Path(f"report.{format}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(response.content)
    console.print(f"Saved {format} report to {destination.resolve()}")


@app.command()
def cancel(run_id: str):
    """Request cancellation of a running assessment."""
    console.print_json(json.dumps(api_request("POST", f"/api/runs/{run_id}/cancel")))


if __name__ == "__main__":
    app()
