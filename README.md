# BreakMyBot

**We break your AI so your users don't have to.**

BreakMyBot connects to an AI application's HTTP API, discovers OpenAPI endpoints and request shapes, performs bounded real invocations, records response and latency evidence, computes a deterministic score only when enough checks ran, and generates portable JSON, HTML, and PDF reports.

BreakMyBot is a standalone repository: its API, durable worker, SQLite store, CLI, reports, demo target, and bundled dashboard are all part of this project. It supports black-box HTTP checks and opt-in FastAPI tool-fault instrumentation. Persistent memory lifecycle, security boundaries, and hidden agent state remain unsupported unless a suitable target adapter exists. Optional MemoryScope and nRouter integrations do not need to be installed to use the default product. Reports distinguish score from coverage and never present the score as a probability of safety.

## Quick start

Start the BreakMyBot API and local storage:

```powershell
docker compose up -d --build
```

BreakMyBot is self-contained: clone this repository and run `docker compose up -d --build`. It starts the API at `http://127.0.0.1:8000`, a worker, and the Next.js dashboard at `http://127.0.0.1:3000`; SQLite and reports are stored in this checkout's `data/` directory. Turbulence and MemoryScope are not required to install, run, or use the default product. Swagger docs are at `http://127.0.0.1:8000/docs`.

Queued runs survive API restarts. Worker leases are renewed while a run executes; an expired in-flight run is marked failed instead of automatically replayed, avoiding duplicate target side effects.

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

The portable `report.json` includes `$schema` and `report_schema_version` fields and validates against [`docs/report-schema.json`](docs/report-schema.json). Its `report_paths` are relative artifact names, not machine-specific filesystem paths.

Download a report copy to the current directory with `breakmybot export --latest --format pdf` (or `html` / `json`).

## Target discovery and connectivity

BreakMyBot checks target reachability from the API/runner environment, then looks for `/openapi.json` and candidate invocation routes such as `/chat`, `/api/chat`, `/invoke`, `/agent`, and `/ask`. If the schema is not sufficient, the CLI requests the route and payload. It does not infer a successful baseline from a host-side-only check.

With Docker Desktop, the compose service maps host loopback URLs to `host.docker.internal` for the container-side request and adds the host gateway. Run a target on the host bound to `0.0.0.0` if container access requires it, and use a host firewall rule limited to the local Docker network. Linux Docker Compose includes the `host-gateway` mapping. Other Compose projects can use their shared network and a hostname explicitly added to `BREAKMYBOT_ALLOWED_HOSTS`.

Loopback targets are allowed by default. Target URLs may not embed credentials or query parameters; configure auth through environment-variable names instead. Explicit test hosts must be listed in `BREAKMYBOT_ALLOWED_HOSTS`; their resolved addresses must be private, loopback, link-local, or inside a configured `BREAKMYBOT_ALLOWED_CIDRS` network. Redirects are disabled. Do not point this prototype at a production or side-effecting service.

## Profiles and scoring

- `quick`: one invocation.
- `standard`: three invocations when GET, or when repeated POST has been explicitly authorized; otherwise one POST invocation.
- `deep`: up to eight invocations under the same repeat safety gate.

Every response is captured with its HTTP status, content type, bounded body excerpt, and measured latency. Assertions are deterministic: successful HTTP response, user-provided expected substring, and user-configured latency ceiling. Instrumented tool-fault checks are available only through the target's explicit authenticated adapter; persistent memory lifecycle and security-boundary categories remain unsupported, never passed.

Methodology `breakmybot-score-v3` computes per-category pass ratios, then combines observed scored categories using the published rubric weights. At least 50% of total rubric weight and three conclusive scenarios are required to establish an aggregate; the normalized observed weight is reported so partial-scope evidence is not presented as a full score. Depending on opt-in integrations, observed scopes can include black-box HTTP, target-instrumented tool faults, and MemoryScope-grounded answer checks; each report lists the scopes actually observed. Coverage is separate from score. An absent score is reported as **Not established**. A score is not a safety probability, benchmark certification, or universal reliability claim.

## API

- `GET /api/health`
- `POST /api/projects` and `GET /api/projects`
- `POST /api/targets/discover`
- `POST /api/targets/validate`
- `POST /api/runs`
- `GET /api/runs?project_id=...` and `GET /api/runs/{id}`
- `POST /api/runs/{id}/cancel`
- `GET /api/runs/{id}/events` (SSE)
- `GET /api/runs/{id}/report.{html|pdf|json}`
- `GET /api/scoring/methodology`

Projects and their same-origin assessments are persisted in SQLite; the dashboard can filter by project and compare up to four stored runs from the same project or target. The local deployment supports one active worker process using SQLite leases; horizontal multi-worker scaling, PostgreSQL, user accounts, and dashboard authentication are not implemented in this release. Keep the bundled dashboard bound to loopback.

For target authentication, put the secret in the BreakMyBot API and worker environment (for Compose, add a variable to the ignored local `.env` file), then enter only its variable name in the dashboard's Authentication section. Configure the header and scheme there as needed. Raw tokens are never entered in or stored by the browser.

## Integrating Turbulence and MemoryScope

The reference audit is in [`docs/reference-audit.md`](docs/reference-audit.md). These are optional, separately operated integrations—not runtime dependencies. Turbulence's seeded faults currently wrap its own Concentrate adapter and cannot affect an arbitrary target. MemoryScope tests a real external SSE endpoint against supplied references but requires an nRouter judge model and explicit consent; its current beta does not cover persistent memory lifecycle. BreakMyBot does not import their code via local paths or transfer their internal demo scores. Adapter contracts and these limits are documented in [`docs/integration-boundaries.md`](docs/integration-boundaries.md).

Agent developers can opt into session-scoped tool fault injection. Install BreakMyBot in the target agent's Python environment and wire tool calls through its returned controller. The control API is installed only when `enabled=True`, requires a bearer token of at least 32 characters, and only applies faults to a request carrying the armed `X-BreakMyBot-Session` header:

```python
import os
from breakmybot.instrumentation import instrument

tool_faults = instrument(app, token=os.environ["BREAKMYBOT_INSTRUMENTATION_TOKEN"], enabled=True)
result = await tool_faults.invoke("catalog", catalog_lookup, "item-42")
```

Set the same high-entropy `BREAKMYBOT_INSTRUMENTATION_TOKEN` in the target process and BreakMyBot API environment. Add a JSON case array such as `examples/demo-agent/tool-faults.json`, then run `breakmybot test ... --instrumented-faults examples/demo-agent/tool-faults.json --allow-repeated-post` and confirm the target invocation, repeat authorization, and fault arming prompts. Entries can be single faults or named chains with 2–5 ordered tool faults. Each scenario runs in an isolated session, the target returns injection events, and the runner attempts deletion even when arming or invocation fails. A scenario counts only if every configured injection is observed in order, cleanup succeeds, and an explicit expected substring is supplied (or the target request itself fails); otherwise it remains inconclusive/setup error. Sessions expire after five minutes and are bounded to 100 active sessions. Do not expose the control API to untrusted networks.

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

For a deterministic failing control, also start the demo with `DEMO_UNSAFE_TOOL_FALLBACK=true`. The same injected timeout then produces a false refund claim, which violates the `contact support` invariant and should appear as a verified Tool reliability finding. Keep this switch limited to the isolated local demo.

The example `examples/demo-agent/tool-faults.json` also includes an ordered two-tool chain for `/api/chat/chain`: it injects an exception in `customer_context`, then a timeout in `refund_policy`, and verifies the safe support fallback. The automated suite covers this target-side session/provenance/cleanup flow.

The isolated fake refund dependency also demonstrates an actual duplicate side effect after a lost acknowledgment. Start the demo with instrumentation enabled as above, reset its fake service, then run:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8001/api/refund/reset
breakmybot test --url http://localhost:8001 --endpoint /api/refund/retry --payload-json '{}' --profile quick --instrumented-faults examples/demo-agent/duplicate-action-fault.json --allow-repeated-post
Invoke-RestMethod -Uri http://localhost:8001/api/refund/state
```

The baseline request is explicitly side-effect-free; only an armed session performs the fake refund. The fault runs the first fake action, loses its acknowledgment, and lets the demo agent retry. Without an idempotency key, the isolated service counter reaches 2 and the configured `side-effect count 1` invariant fails. Repeat after reset with `--payload-json '{"idempotency_key":"local-test-refund-1"}'`; the same ordered event should be observed while the fake service count remains 1 and the invariant passes. This fixture never calls a real payment/refund service. Its automated acceptance test verifies both counts from the fixture state endpoint and requires confirmed lost-ack provenance and session cleanup before accepting the assessment evidence.

The demo also includes a resettable in-process memory fixture for grounded stale-answer checks. To seed stale state, run `Invoke-RestMethod -Method Post -Uri http://localhost:8001/api/memory/seed -ContentType 'application/json' -Body '{"value":"Customers may request a refund within 30 days of purchase."}'`, then use `breakmybot test --url http://localhost:8001 --endpoint /api/chat/sse --payload-json '{"messages":[{"role":"user","content":"What is the refund window?"}]}' --profile quick --expected '14 days'`. The expected invariant should fail against the seeded 30-day memory. Reset with `Invoke-RestMethod -Method Post -Uri http://localhost:8001/api/memory/reset`. This local fixture proves a stale answer is observable; actual MemoryScope judge evaluation still requires its separately configured service and explicit consent. The demo endpoints are unauthenticated and must stay isolated on localhost.

## Development

```powershell
python -m pip install -e ".[dev]"
pytest
ruff check src tests examples benchmarks
```

See [`docs/scoring-methodology.md`](docs/scoring-methodology.md) for formulas and limits. The optional nRouter planner is bounded and consent-gated; the deterministic default workflow makes no paid provider calls. MemoryScope answer judging is a separate optional integration and does not verify persistent-memory behavior.

To reproduce the deterministic local fault/memory regression benchmark, follow [`benchmarks/README.md`](benchmarks/README.md). It runs against isolated loopback demo services and is explicitly not a comparative research result.
