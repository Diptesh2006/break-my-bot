from typer.testing import CliRunner

from breakmybot import cli


def test_cli_queues_assessment_without_sending_a_duplicate_preflight(monkeypatch):
    calls = []

    def fake_api_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/targets/discover":
            return {"reachable": True, "openapi_found": True,
                "application_name": "Local demo", "health_endpoints": [],
                "endpoints": [{"method": "POST", "path": "/api/chat",
                    "payload_template": {"message": "hello"}, "streaming": False,
                    "description": "Chat"}]}
        if path == "/api/runs":
            return {"run_id": "run-cli-test"}
        if path == "/api/runs/run-cli-test":
            return {"id": "run-cli-test", "status": "COMPLETED",
                "scoring": {"overall_score": None, "coverage_percent": 0,
                    "warning": "Insufficient evidence", "scenario_counts": {
                        "PASSED": 0, "VERIFIED_FAILURE": 0, "UNSUPPORTED": 0},
                    "observed_scopes": ["target-instrumented memory scenarios"],
                    "observed_memory_categories": ["access_boundaries"],
                    "untested_capabilities": ["memory category: cross_agent_memory"]},
                "planning": {"status": "not_requested", "scenarios": []},
                "report_paths": {"json": "report.json", "html": "report.html", "pdf": "report.pdf"}}
        raise AssertionError(f"Unexpected API request: {method} {path}")

    monkeypatch.setattr(cli, "api_request", fake_api_request)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    result = CliRunner().invoke(cli.app, ["test", "--url", "http://127.0.0.1:8001",
        "--endpoint", "/api/chat", "--payload-json", '{"message":"hello"}',
        "--profile", "quick"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "worker will perform the baseline invocation" in result.output
    assert "Confirmed target memory categories: access_boundaries" in result.output
    assert "memory category: cross_agent_memory" in result.output
    assert not any(path == "/api/targets/preflight" for _method, path, _kwargs in calls)
    run_creates = [kwargs["json"] for method, path, kwargs in calls
        if method == "POST" and path == "/api/runs"]
    assert len(run_creates) == 1
    assert run_creates[0]["payload"] == {"message": "hello"}
    assert run_creates[0]["profile"] == "quick"


def test_cli_prompts_for_target_url_and_allows_editing_discovered_payload(monkeypatch):
    calls = []

    def fake_api_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/targets/discover":
            return {"reachable": True, "openapi_found": True, "application_name": "Local demo",
                "health_endpoints": [], "endpoints": [{"method": "POST", "path": "/api/chat",
                    "payload_template": {"message": "sample"}, "streaming": False,
                    "description": "Chat"}]}
        if path == "/api/runs":
            return {"run_id": "run-cli-prompt"}
        if path == "/api/runs/run-cli-prompt":
            return {"id": "run-cli-prompt", "status": "COMPLETED",
                "scoring": {"overall_score": None, "coverage_percent": 0,
                    "warning": "Insufficient evidence", "scenario_counts": {
                        "PASSED": 0, "VERIFIED_FAILURE": 0, "UNSUPPORTED": 0}},
                "planning": {"status": "not_requested", "scenarios": []},
                "report_paths": {"json": "report.json", "html": "report.html", "pdf": "report.pdf"}}
        raise AssertionError(f"Unexpected API request: {method} {path}")

    monkeypatch.setattr(cli, "api_request", fake_api_request)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    result = CliRunner().invoke(cli.app, ["test", "--profile", "quick"],
        input="http://127.0.0.1:8001\nn\n{\"message\":\"custom\"}\ny\n")

    assert result.exit_code == 0, result.output
    assert "AI application URL" in result.output
    assert "Suggested request payload" in result.output
    create = next(kwargs["json"] for method, path, kwargs in calls
        if method == "POST" and path == "/api/runs")
    assert create["target_url"] == "http://127.0.0.1:8001"
    assert create["payload"] == {"message": "custom"}
