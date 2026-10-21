# BreakMyBot

**We break your AI so your users don't have to.**

BreakMyBot connects to an AI application's HTTP API, discovers OpenAPI endpoints and request shapes, performs bounded real invocations, records response and latency evidence, computes a deterministic score only when enough checks ran, and generates portable JSON, HTML, and PDF reports.

The current release is the first black-box vertical slice. It does not claim to observe internal tool calls, persistent memory, safety policy, or hidden agent state. Those checks are marked unsupported until an opt-in target adapter is available. Reports distinguish score from coverage and never use the score as a probability of safety.

## Quick start

Start the BreakMyBot API and local storage:

```powershell
docker compose up -d --build
```

The API binds to `127.0.0.1:8000`. Swagger docs are at `http://127.0.0.1:8000/docs`.

Install the CLI from this checkout (Python 3.11+):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
breakmybot doctor
```

Run an assessment:

```powershell
breakmybot test --url http://localhost:8000
```

For an endpoint without OpenAPI, specify it and the request JSON:

```powershell
breakmybot test --url http://localhost:8000 --endpoint /api/chat --payload-json '{"message":"What is the return policy?"}'
```

POST requests require interactive confirmation. Repeated POSTs can cause side effects, so they are disabled unless `--allow-repeated-post` is provided and confirmed. Supply `--expected "14 days"` to add a literal response assertion. Use this only with an answer expectation you have verified.

The CLI stores reports under `data/reports/<run-id>/report.{json,html,pdf}`. Use `breakmybot report --latest`, `breakmybot runs`, `breakmybot status <run-id>`, and `breakmybot cancel <run-id>` to manage assessments.

Download a report copy to the current directory with `breakmybot export --latest --format pdf` (or `html` / `json`).

## Target discovery and connectivity

BreakMyBot checks target reachability from the API/runner environment, then looks for `/openapi.json` and candidate invocation routes such as `/chat`, `/api/chat`, `/invoke`, `/agent`, and `/ask`. If the schema is not sufficient, the CLI requests the route and payload. It does not infer a successful baseline from a host-side-only check.

With Docker Desktop, the compose service maps host loopback URLs to `host.docker.internal` for the container-side request and adds the host gateway. Run a target on the host bound to `0.0.0.0` if container access requires it, and use a host firewall rule limited to the local Docker network. Linux Docker Compose includes the `host-gateway` mapping. Other Compose projects can use their shared network and a hostname explicitly added to `BREAKMYBOT_ALLOWED_HOSTS`.

Loopback targets are allowed by default. Explicit test hosts must be listed in `BREAKMYBOT_ALLOWED_HOSTS`; their resolved addresses must be private, loopback, link-local, or inside a configured `BREAKMYBOT_ALLOWED_CIDRS` network. Redirects are disabled. Do not point this prototype at a production or side-effecting service.

## Profiles and scoring

- `quick`: one invocation.
- `standard`: three invocations when GET, or when repeated POST has been explicitly authorized; otherwise one POST invocation.
- `deep`: up to eight invocations under the same repeat safety gate.

Every response is captured with its HTTP status, content type, bounded body excerpt, and measured latency. Assertions are deterministic: successful HTTP response, user-provided expected substring, and user-configured latency ceiling. Instrumented tool-fault checks are available only through the target's explicit authenticated adapter; persistent memory lifecycle and security-boundary categories remain unsupported, never passed.

Methodology `breakmybot-score-v2` computes the pass ratio over conclusive, evidence-backed executed scenarios only after at least three conclusive scenarios. Depending on opt-in integrations, observed scopes can include black-box HTTP, target-instrumented tool faults, and MemoryScope-grounded answer checks; each report lists the scopes actually observed. Coverage is separate from score. An absent score is reported as **Not established**. A score is not a safety probability, benchmark certification, or universal reliability claim.

## API

- `GET /api/health`
- `POST /api/targets/discover`
- `POST /api/targets/validate`
- `POST /api/runs`
- `GET /api/runs` and `GET /api/runs/{id}`
- `POST /api/runs/{id}/cancel`
- `GET /api/runs/{id}/events` (SSE)
- `GET /api/runs/{id}/report.{html|pdf|json}`
- `GET /api/scoring/methodology`

SQLite run records and report artifacts are stored under `data/`. The current API is a single-process local developer service; the compose bind mount keeps artifacts in the checkout. PostgreSQL, multi-worker coordination, user accounts, and a web dashboard are not implemented in this release.

## Integrating Turbulence and MemoryScope

The reference audit is in [`docs/reference-audit.md`](docs/reference-audit.md). Turbulence's seeded faults currently wrap its own Concentrate adapter and cannot affect an arbitrary target. MemoryScope tests a real external SSE endpoint against supplied references but requires an nRouter judge model and explicit consent; its current beta does not cover persistent memory lifecycle. BreakMyBot does not import their code via local paths or transfer their internal demo scores. Adapter contracts and these limits are documented in [`docs/integration-boundaries.md`](docs/integration-boundaries.md).

Agent developers can opt into session-scoped tool fault injection. Install BreakMyBot in the target agent's Python environment and wire tool calls through its returned controller. The control API is installed only when `enabled=True`, requires a bearer token of at least 32 characters, and only applies faults to a request carrying the armed `X-BreakMyBot-Session` header:

```python
import os
from breakmybot.instrumentation import instrument

tool_faults = instrument(app, token=os.environ["BREAKMYBOT_INSTRUMENTATION_TOKEN"], enabled=True)
result = await tool_faults.invoke("catalog", catalog_lookup, "item-42")
```

Set the same high-entropy `BREAKMYBOT_INSTRUMENTATION_TOKEN` in the target process and BreakMyBot API environment. Add a JSON case array such as `examples/demo-agent/tool-faults.json`, then run `breakmybot test ... --instrumented-faults examples/demo-agent/tool-faults.json --allow-repeated-post` and confirm the target invocation, repeat authorization, and fault arming prompts. Each fault runs in an isolated session, the target returns injection events, and the runner attempts deletion even when arming or invocation fails. A fault counts as a pass/failure only if injection was observed, cleanup succeeded, and an explicit expected substring was supplied (or the target request itself failed); otherwise it remains inconclusive/setup error. Sessions expire after five minutes and are bounded to 100 active sessions. Do not expose the control API to untrusted networks.

An optional MemoryScope API bridge is available for grounded answer evaluation. Configure `MEMORYSCOPE_API_URL` for the separately running service, then pass an approved case array with `--memory-cases cases.json`. BreakMyBot asks for consent before sending reference cases and target answers to the nRouter judge. Configure the MemoryScope service's `NROUTER_API_KEY`, `NROUTER_JUDGE_MODEL`, and target-origin allowlist there. This evaluates answers against references; it does not test persistent-memory lifecycle or internal memory controls.

An optional structured scenario planner can be enabled with `--plan-with-nrouter` after configuring `NROUTER_API_KEY` and `BREAKMYBOT_PLANNER_MODEL`. It sends only the application name, endpoint path, method, and payload field names after a separate consent prompt. Proposed scenarios are capability-gated, recorded as hypotheses, and never automatically executed. Each run makes at most one bounded planning call; usage is recorded when nRouter returns it.

## Local demo target

Run this separate demo process:

```powershell
python -m pip install -r examples/demo-agent/requirements.txt
python -m uvicorn app:app --app-dir examples/demo-agent --host 0.0.0.0 --port 8001
```

Then assess it with `breakmybot test --url http://localhost:8001`. The demo is a deterministic fixture, not a real external LLM, and its report identifies only HTTP behavior.

To try instrumented recovery, start the demo with `BREAKMYBOT_INSTRUMENTATION_ENABLED=true` and a shared `BREAKMYBOT_INSTRUMENTATION_TOKEN` (32+ characters) in both the agent and BreakMyBot API environments. Then run `breakmybot test --url http://localhost:8001 --endpoint /api/chat --payload-json '{"message":"refund policy"}' --instrumented-faults examples/demo-agent/tool-faults.json --allow-repeated-post`. This executes a real target request with an injected `refund_policy` timeout and checks the explicit `contact support` fallback invariant.

## Development

```powershell
python -m pip install -e ".[dev]"
pytest
ruff check src tests
```

See [`docs/scoring-methodology.md`](docs/scoring-methodology.md) for formulas and limits. nRouter planner/judge integration is optional future work; no provider call is made by the current vertical slice.
