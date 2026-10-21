from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

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
        console.print("Created .env. Review optional nRouter settings and allowed hosts.")
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
    auth_env: str | None = typer.Option(None, "--auth-env", help="Environment variable name containing the target bearer token"),
    memory_cases: Path | None = typer.Option(None, "--memory-cases", help="JSON array of approved MemoryScope reference cases"),
    memory_agent_path: str | None = typer.Option(None, "--memory-agent-path", help="SSE chat path on the same target origin"),
    memory_auth_env: str | None = typer.Option(None, "--memory-auth-env", help="MemoryScope agent auth environment variable name"),
    instrumented_faults: Path | None = typer.Option(None, "--instrumented-faults", help="JSON file of opt-in tool fault scenarios"),
    instrumentation_auth_env: str = typer.Option("BREAKMYBOT_INSTRUMENTATION_TOKEN", "--instrumentation-auth-env", help="Environment variable containing the target instrumentation control token"),
    plan_with_nrouter: bool = typer.Option(False, "--plan-with-nrouter", help="Opt in to send endpoint metadata to the configured nRouter planner"),
):
    """Discover and run bounded black-box HTTP checks."""
    if profile not in {"quick", "standard", "deep"}:
        raise typer.BadParameter("profile must be quick, standard, or deep")
    console.print("[bold]BREAKMYBOT[/bold]\nWe break your AI so your users don't have to.\n")
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
    choices = discovered["endpoints"]
    selected = next((row for row in choices if endpoint and row["path"] == endpoint), None)
    if not endpoint and len(choices) == 1:
        selected = choices[0]
    elif not endpoint and choices:
        table = Table("#", "Method", "Path", "Description")
        for index, row in enumerate(choices, 1):
            table.add_row(str(index), row["method"], row["path"], row["description"][:60])
        console.print(table)
        index = int(Prompt.ask("Choose endpoint number", default="1"))
        if not 1 <= index <= len(choices):
            raise typer.BadParameter("endpoint choice is out of range")
        selected = choices[index - 1]
    if selected:
        endpoint, method, template = selected["path"], selected["method"], selected["payload_template"]
        console.print(f"Selected endpoint: {method} {endpoint}")
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
    if method != "GET" and not Confirm.ask("Confirm endpoint and payload are safe for a test invocation", default=False):
        console.print("Cancelled before sending the target request.")
        raise typer.Exit(code=4)
    console.print("Running one baseline request from the BreakMyBot execution environment…")
    baseline = api_request("POST", "/api/targets/preflight", json={"target_url": url,
        "endpoint_path": endpoint, "method": method, "payload": payload, "profile": "quick",
        "auth_env": auth_env})
    if not baseline["successful"]:
        console.print(f"[red]Baseline invocation returned HTTP {baseline['status_code']}.[/red]")
        console.print("Review the endpoint, payload, target logs, and authentication before running scenarios.")
        raise typer.Exit(code=3)
    console.print(f"[green]OK[/green] Baseline returned HTTP {baseline['status_code']} "
        f"in {baseline['duration_ms']} ms ({baseline['content_type'] or 'unknown content type'})")
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
    if plan_with_nrouter and not Confirm.ask("Send target name, endpoint path and payload field names to nRouter for scenario planning?", default=False):
        plan_with_nrouter = False
    created = api_request("POST", "/api/runs", json={"target_url": url, "endpoint_path": endpoint,
        "method": method, "payload": payload, "profile": profile, "application_name": discovered.get("application_name"),
        "expected_substring": expected, "allow_repeated_post": allow_repeated_post, "auth_env": auth_env,
        "memory_assessment": memory_assessment, "instrumented_assessment": instrumented_assessment,
        "use_nrouter_planner": plan_with_nrouter})
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
    console.print(f"Scenario planner: {run.get('planning', {}).get('status', 'not requested')} ({len(run.get('planning', {}).get('scenarios', []))} proposals; none automatically executed)")
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
