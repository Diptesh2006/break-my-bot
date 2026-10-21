# BreakMyBot

**Give your AI a controlled bad day—before production gives it a catastrophic one.**

Your agent works in the happy path. What happens when a tool times out, memory goes stale, an acknowledgment disappears, or requests hit at once? BreakMyBot runs bounded experiments against a real, authorized AI application, checks what actually happened, and turns reproducible failures into evidence you can act on.

## Break it. Verify it. Fix it.

- **Find the real interface.** Discover OpenAPI routes, request shapes, health checks, and streaming endpoints—or provide the endpoint and payload yourself.
- **Test the running application.** Capture actual status codes, response contracts, latency, and explicit expected-answer checks.
- **Inject faults only where the target allows it.** Opt-in agent instrumentation can trigger tool timeouts, exceptions, malformed results, and ordered fault chains. Every scored injection needs target-side confirmation and successful cleanup.
- **Catch failures that compound.** Exercise stale memory, cascading tool failures, and lost acknowledgments against resettable, isolated demo fixtures. Verify side effects instead of inferring them.
- **Get evidence, not vibes.** Persist observations and reproducible findings; export portable JSON, HTML, and PDF reports.
- **Know what wasn’t tested.** Score and coverage stay separate. Unsupported capabilities remain visible, and a score is withheld when evidence is insufficient.

### Standalone by design

Clone this repository and run it: BreakMyBot includes its API, worker, database setup, CLI, dashboard, reports, and deterministic demo. Turbulence and MemoryScope are optional, separately operated references—not hidden dependencies or borrowed demo scores. nRouter is the default provider for the consent-gated AI planner; the deterministic workflow needs no API key and makes no paid model calls.

### Controlled by design

Targets are restricted to loopback or explicitly allowlisted test networks. POST calls and repeated or concurrent probes require deliberate opt-in. AI-generated proposals are hypotheses by default; executing them requires separate consent and an operator-approved fault and assertion allowlist. An ordinary HTTP connection cannot reveal internal tools or memory, so those capabilities stay untested unless the target exposes a supported adapter.

## Quick start

Clone only this repository. No checkout, installation, API key, or service from Turbulence or MemoryScope is needed for the default product. Choose either the Docker stack (API, worker, and dashboard) or the Python-only local setup.

### Full local stack with Docker

Prerequisite: Docker Desktop (Windows/macOS) or Docker Engine with the Compose plugin (Linux).

```powershell
docker compose up -d --build
```

This starts PostgreSQL, the API, a worker, and the dashboard at `http://127.0.0.1:3000`. PostgreSQL data is kept in a named Docker volume; generated report files are stored under this checkout's `data/` directory. The API advertises its active backend at `/api/health`; Swagger docs are at `http://127.0.0.1:8000/docs`. Compose uses a local-development-only database password by default. Before exposing the database or application beyond your own machine, set a strong `POSTGRES_PASSWORD` in the ignored `.env` file and configure appropriate network access. If the password contains URL-reserved characters, set `BREAKMYBOT_DATABASE_URL` there with its password URL-encoded.

### API and CLI without Docker

Python 3.11+ is required. From the repository root, create and activate an environment (`.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` on macOS/Linux), then install the project:

```powershell
python -m venv .venv
python -m pip install -e .
breakmybot doctor
```

Start the API and durable worker in separate terminals with the environment activated:

```powershell
uvicorn breakmybot.api:app --host 127.0.0.1 --port 8000
```

```powershell
python -m breakmybot.worker
```

They use local SQLite storage under `data/` by default; ordered SQLite schema migrations are applied automatically at startup. To use PostgreSQL outside Compose, install `python -m pip install -e '.[postgres]'` and set `BREAKMYBOT_DATABASE_URL` for both API and worker. PostgreSQL migrations are applied automatically. For a quick CLI-only check against your own reachable target, `breakmybot test --url <target-url>` does not require the API or worker. Queued runs survive API restarts; an expired in-flight run is marked failed rather than replayed, avoiding duplicate target side effects.

Run an assessment against a local target:

```powershell
breakmybot test --url http://localhost:8001 --endpoint /api/chat --payload-json '{"message":"What is the refund policy?"}'
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

BreakMyBot checks target reachability from the API/runner environment, then looks for `/openapi.json` and candidate invocation routes such as `/chat`, `/api/chat`, `/invoke`, `/agent`, and `/ask`. Discovery reports GET health/readiness routes separately, retains response schemas, and marks `text/event-stream` operations as streaming. For request bodies it uses media examples when present, otherwise builds a bounded sample from local schema references, required/nested fields, enums, defaults, common constraints, and conversational fields such as `input`, `prompt`, or `messages`. This is a suggested baseline, not a guarantee that an application accepts it; review it or provide `--payload-json` when needed. The CLI requests an explicit endpoint and JSON payload when discovery is insufficient, and confirms non-GET requests before sending them. It does not infer a successful baseline from a host-side-only check.

With Docker Desktop, the compose service maps host loopback URLs to `host.docker.internal` for the container-side request and adds the host gateway. Run a target on the host bound to `0.0.0.0` if container access requires it, and use a host firewall rule limited to the local Docker network. Linux Docker Compose includes the `host-gateway` mapping. Other Compose projects can use their shared network and a hostname explicitly added to `BREAKMYBOT_ALLOWED_HOSTS`.

Loopback targets are allowed by default. Target URLs may not embed credentials or query parameters; configure auth through environment-variable names instead. Explicit test hosts must be listed in `BREAKMYBOT_ALLOWED_HOSTS`; their resolved addresses must be private, loopback, link-local, or inside a configured `BREAKMYBOT_ALLOWED_CIDRS` network. Redirects are disabled. Do not point this prototype at a production or side-effecting service.

## Profiles and scoring

- `quick`: one invocation.
- `standard`: three invocations when GET, or when repeated POST has been explicitly authorized; otherwise one POST invocation.
- `deep`: up to eight invocations under the same repeat safety gate.

Concurrent handling is separate from profiles and disabled by default. `--concurrency-requests 2` through `5` opts into that many simultaneous requests after a separate CLI confirmation; non-GET probes also require `--allow-repeated-post` and its side-effect confirmation. The runner records each status, duration, and error type. This small functional probe is not a load or capacity benchmark.

Every response is captured with its HTTP status, content type, bounded body excerpt, and measured latency. Assertions are deterministic: successful HTTP response, user-provided expected substring, and user-configured latency ceiling. Instrumented tool-fault checks are available only through the target's explicit authenticated adapter; memory lifecycle and security-boundary categories are tested only when the target installs and advertises the corresponding adapter, otherwise they remain explicitly untested.

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

The reference audit is in [`docs/reference-audit.md`](docs/reference-audit.md). These are optional, separately operated integrations—not runtime dependencies. Turbulence's seeded faults currently wrap its own Concentrate adapter and cannot affect an arbitrary target. MemoryScope tests a real external SSE endpoint against supplied references but requires an nRouter judge model and explicit consent; its current beta does not cover persistent memory lifecycle. BreakMyBot does not import their code via local paths or transfer their internal demo scores. Adapter contracts and these limits are documented in [`docs/integration-boundaries.md`](docs/integration-boundaries.md). For target-owned memory instrumentation, see [`docs/memory-adapter.md`](docs/memory-adapter.md).

Agent developers can opt into session-scoped tool fault injection. Install BreakMyBot in the target agent's Python environment and wire tool calls through its returned controller. The control API is installed only when `enabled=True`, requires a bearer token of at least 32 characters, and only applies faults to a request carrying the armed `X-BreakMyBot-Session` header:

```python
import os
from breakmybot.instrumentation import instrument

tool_faults = instrument(app, token=os.environ["BREAKMYBOT_INSTRUMENTATION_TOKEN"], enabled=True)
result = await tool_faults.invoke("catalog", catalog_lookup, "item-42")
```

Set the same high-entropy `BREAKMYBOT_INSTRUMENTATION_TOKEN` in the target process and BreakMyBot API environment. Add a JSON case array such as `examples/demo-agent/tool-faults.json`, then run `breakmybot test ... --instrumented-faults examples/demo-agent/tool-faults.json --allow-repeated-post` and confirm the target invocation, repeat authorization, and fault arming prompts. Entries can be single faults or named chains with 2–5 ordered tool faults. Each scenario runs in an isolated session, the target returns injection events, and the runner attempts deletion even when arming or invocation fails. A scenario counts only if every configured injection is observed in order, cleanup succeeds, and an explicit expected substring is supplied (or the target request itself fails); otherwise it remains inconclusive/setup error. Sessions expire after five minutes and are bounded to 100 active sessions. Do not expose the control API to untrusted networks.

An optional MemoryScope API bridge is available for grounded answer evaluation. Configure `MEMORYSCOPE_API_URL` for the separately running service, then pass an approved case array with `--memory-cases cases.json`. BreakMyBot asks for consent before sending reference cases and target answers to the nRouter judge. Configure the MemoryScope service's `NROUTER_API_KEY`, `NROUTER_JUDGE_MODEL`, and target-origin allowlist there. This evaluates answers against references; it does not test persistent-memory lifecycle or internal memory controls. Target-owned memory experiments and combined memory/tool-fault cases are documented in [`docs/memory-adapter.md`](docs/memory-adapter.md).

An optional structured scenario planner is enabled with `--plan-with-ai` (legacy alias `--plan-with-nrouter`). nRouter remains the MVP default: configure `NROUTER_API_KEY`, `NROUTER_BASE_URL`, and `BREAKMYBOT_PLANNER_MODEL`. Other API keys can be used with any OpenAI-compatible chat-completions endpoint: set `BREAKMYBOT_LLM_PROVIDER` (for example `openai-compatible`), `BREAKMYBOT_LLM_BASE_URL`, and either `BREAKMYBOT_LLM_API_KEY` or `BREAKMYBOT_LLM_API_KEY_ENV` pointing to the environment variable that holds the key; set `BREAKMYBOT_LLM_MODEL` or `BREAKMYBOT_PLANNER_MODEL`. Optional auth header/scheme, chat-completions path, and token-parameter settings support compatible gateways. Non-loopback provider URLs must use HTTPS. Keep secrets only in the API/worker environment, never in dashboard requests or source control. This generic path speaks the OpenAI-compatible chat-completions format; native provider-specific protocols that do not expose that format are not implicitly supported. The provider actually selected is recorded in run planning metadata. It sends the application name, endpoint/method, and payload field names only after consent. By default, proposals remain unexecuted hypotheses. For an explicitly instrumented target, you may additionally pass `--execute-planned-scenarios`; the CLI asks for separate execution consent and requires an accepted `--instrumented-faults` allowlist, an expected-response oracle, and repeated-call authorization for non-GET endpoints. At most three generated scenarios run by default; `--max-planned-scenarios` bounds this to 1–5. Generated fault settings must come from the operator-approved fault atoms, cascade chains cannot target a tool more than once, and assertions must use an approved oracle. Generated tests always use the originally selected endpoint and payload. If provider setup is unavailable or fails, no generated scenarios execute. Each assessment makes at most one bounded planning call; provider usage and cost are recorded when returned by the provider.

For a matched fixed-vs-generated scenario pilot on the local demo, use [`benchmarks/README.md`](benchmarks/README.md) and `benchmarks/compare_strategies.py`. The script requires the configured AI provider in addition to the demo instrumentation setup; by default that is nRouter. Its default 16 planning calls share an aggregate 12,288-completion-token cap (768 per call), which can be adjusted with `--total-planner-token-budget`. Its small default run is exploratory, not a powered or comparative-performance claim.

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

The PostgreSQL storage contract has an opt-in live integration test. Point `BREAKMYBOT_TEST_POSTGRES_URL` at a disposable PostgreSQL database and run `python -m pytest -q tests/test_postgres_integration.py`; the test creates uniquely named rows and removes them afterward. Do not point it at a production database.

To reproduce the deterministic local fault/memory regression benchmark, follow [`benchmarks/README.md`](benchmarks/README.md). It runs against isolated loopback demo services and is explicitly not a comparative research result.
