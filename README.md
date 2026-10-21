# BreakMyBot

**Break your AI so your users don't have to.**

Your agent works in the happy path. What happens when a tool times out, memory goes stale, an acknowledgment disappears, or requests hit at once? BreakMyBot runs bounded checks against an application you control, verifies what the target actually did, and turns reproducible failures into evidence you can fix before users find them.

## Break it. Verify it. Fix it.

- **Find the real interface.** Discover OpenAPI routes, request shapes, health checks, and streaming endpoints—or provide the endpoint and payload yourself.
- **Test the running application.** Capture actual status codes, response contracts, latency, and explicit expected-answer checks.
- **Inject faults only where the target allows it.** The opt-in FastAPI adapter can trigger tool timeouts, exceptions, malformed results, and ordered fault chains. Every scored injection needs target-side confirmation and successful cleanup.
- **Explore failures that compound.** The bundled isolated demo reproduces stale-memory, cascading-tool, and lost-acknowledgment bugs. Against your own agent, these tests require a compatible, explicitly installed adapter; ordinary HTTP access cannot expose hidden state.
- **Get evidence, not vibes.** Persist observations and reproducible findings; export portable JSON, HTML, and PDF reports.
- **Know what wasn’t tested.** Score and coverage stay separate. Unsupported capabilities remain visible, and a score is withheld when evidence is insufficient.

### Standalone by design

Clone this repository and run it: BreakMyBot includes its API, worker, database setup, CLI, dashboard, reports, and deterministic demo. The public [Turbulence](https://github.com/Diptesh2006/turbulence) and [MemoryScope](https://github.com/Diptesh2006/MemoryScope) projects are optional integrations, not hidden dependencies. BreakMyBot never imports their local source or copies their internal demo scores into your assessment. nRouter is the default provider for the optional, consent-gated AI planner; the normal deterministic workflow needs no API key and makes no paid model calls.

### Controlled by design

Targets are restricted to loopback or explicitly allowlisted test networks. POST calls and repeated or concurrent probes require deliberate opt-in. AI-generated proposals are hypotheses by default; executing them requires separate consent and an operator-approved fault and assertion allowlist. An ordinary HTTP connection cannot reveal internal tools or memory, so those capabilities stay untested unless the target exposes a supported adapter.

## What you can test

| Access to your application | What BreakMyBot can verify | What it cannot infer |
| --- | --- | --- |
| HTTP endpoint and a valid request | Connectivity, real response status/body, basic response validity, latency, and operator-supplied literal expectations | Hidden prompts, tool calls, internal state, semantic correctness beyond supplied checks, or memory behavior |
| BreakMyBot's opt-in FastAPI tool adapter installed in your agent | Confirmed tool-level faults, ordered chains, target responses, and cleanup evidence | Other frameworks or tools that do not use the adapter |
| Your real memory store exposed through the opt-in memory adapter | Operator-authored seed/snapshot/restore cases for categories your target explicitly advertises | Any category your adapter does not implement; lifecycle behavior cannot be inferred from chat answers |
| Separately operated MemoryScope service | Reference-grounded evaluation of answers from a supported SSE endpoint | Persistent memory lifecycle, tenant isolation, or hidden memory controls |

These are distinct testing levels, not a single universal agent hook. Unsupported checks are reported as untested; they are never silently counted as passes.

## How an assessment works

1. BreakMyBot reaches the target from the API/worker network, discovers OpenAPI when available, and suggests an invocation endpoint and payload.
2. You choose or confirm the endpoint and payload. The CLI asks before invoking non-GET operations.
3. The worker performs bounded real target calls and records status, response evidence, latency, and any confirmed adapter events.
4. Deterministic rules evaluate the checks. An optional AI planner can suggest scenarios, but cannot set the final score; generated faults require a separate execution opt-in and operator allowlists.
5. The run is persisted and the CLI/dashboard link to JSON, HTML, and PDF reports based on that run's evidence.

## Quick start

Clone only this repository. You do not need to clone or run Turbulence or MemoryScope to use BreakMyBot.

```powershell
git clone https://github.com/Diptesh2006/break-my-bot.git
cd break-my-bot
```

Choose the Docker stack for the dashboard and managed services, or run the API and worker directly with Python. The CLI is a host-side Python command in either setup.

### Full local stack with Docker

Prerequisite: Docker Desktop (Windows/macOS) or Docker Engine with the Compose plugin (Linux). Start the stack:

```powershell
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

This starts PostgreSQL, the API, a worker, and the dashboard at `http://127.0.0.1:3000`. PostgreSQL data is kept in a named Docker volume; generated report files are stored under this checkout's `data/` directory. Swagger docs are at `http://127.0.0.1:8000/docs`.

To use the CLI from your computer, install it once in a host Python environment using the next section. Start your target application separately; if you want to try BreakMyBot immediately, use the bundled demo instructions in [Local demo target](#local-demo-target).

The Compose database password is for local development only. The app and database ports bind to loopback by default. If you configure provider credentials, target allowlists, or a different database password, copy `.env.example` to `.env` and edit it locally; `.env` is ignored by Git. Before exposing any service beyond your machine, set strong credentials and review network access. If the database password contains URL-reserved characters, set `BREAKMYBOT_DATABASE_URL` with the password URL-encoded. To use the `breakmybot` CLI from your host, install the project using the next section; Docker Compose does not install a host CLI command.

### API and CLI without Docker

Python 3.11+ is required. From the repository root, create and activate an environment (`.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` on macOS/Linux), then install the project:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Start the API and durable worker in two separate terminals. Activate `.venv` in each terminal first:

```powershell
uvicorn breakmybot.api:app --host 127.0.0.1 --port 8000
```

```powershell
python -m breakmybot.worker
```

They use local SQLite storage under `data/` by default; ordered schema migrations run automatically at startup. In a third terminal, activate `.venv`, then check the API and run assessments with the CLI:

```powershell
breakmybot doctor
breakmybot test --url http://localhost:8001
```

The CLI submits assessments to the API; the worker must be running to execute queued runs. For PostgreSQL outside Compose, install `python -m pip install -e '.[postgres]'` and set `BREAKMYBOT_DATABASE_URL` for both API and worker. Queued runs survive API restarts; an expired in-flight run is marked failed rather than replayed, avoiding duplicate target side effects.

To try the commands above, first start your own target or the [bundled demo](#local-demo-target). Replace the example URL, endpoint, and payload with values for your application.

Run an assessment against a local target:

```powershell
breakmybot test --url http://localhost:8001 --endpoint /api/chat --payload-json '{"message":"What is the refund policy?"}'
```

For an endpoint without OpenAPI, specify it and the request JSON:

```powershell
breakmybot test --url http://localhost:8000 --endpoint /api/chat --payload-json '{"message":"What is the return policy?"}'
```

POST requests require interactive confirmation. The CLI discovers the target using GET requests; it does not send the selected invocation before the assessment. The worker performs the assessment's baseline invocation once. Further repeated non-GET calls are disabled unless `--allow-repeated-post` is provided and confirmed. Instrumented, memory, and concurrency options add their own calls and prompts. Supply `--expected "14 days"` to add a literal response assertion; use an expectation you have verified.

The CLI stores reports under `data/reports/<run-id>/report.{json,html,pdf}`. Use `breakmybot report --latest`, `breakmybot runs`, `breakmybot status <run-id>`, and `breakmybot cancel <run-id>` to manage assessments.

The portable `report.json` includes `$schema` and `report_schema_version` fields and validates against [`docs/report-schema.json`](docs/report-schema.json). Its `report_paths` are relative artifact names, not machine-specific filesystem paths.

Download a report copy to the current directory with `breakmybot export --latest --format pdf` (or `html` / `json`).

## Target discovery and connectivity

BreakMyBot checks target reachability from the API/runner environment, then looks for `/openapi.json` and candidate invocation routes such as `/chat`, `/api/chat`, `/invoke`, `/agent`, and `/ask`. Discovery reports GET health/readiness routes separately, retains response schemas, and marks `text/event-stream` operations as streaming. For request bodies it uses media examples when present, otherwise builds a bounded sample from local schema references, required/nested fields, enums, defaults, common constraints, and conversational fields such as `input`, `prompt`, or `messages`. This is a suggested baseline, not a guarantee that an application accepts it; review it or provide `--payload-json` when needed. The CLI requests an explicit endpoint and JSON payload when discovery is insufficient, and confirms non-GET requests before sending them. It does not infer a successful baseline from a host-side-only check.

With Docker Desktop, the Compose services translate host loopback URLs to `host.docker.internal` for container-side requests and add the host gateway. Run a host target bound to an interface reachable by Docker (often `0.0.0.0`) and use a firewall rule limited to the local Docker network. Linux Compose also gets the `host-gateway` mapping. For a target in another Compose project, attach the API and worker to a shared Docker network and set `BREAKMYBOT_ALLOWED_HOSTS` in `.env` to include both `host.docker.internal` and the target service hostname, comma-separated. Do not publish the target service to the public internet just to make it reachable.

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
- `POST /api/targets/preflight` (explicitly invokes the selected target endpoint once; use only when that call is safe)
- `POST /api/runs`
- `GET /api/runs?project_id=...` and `GET /api/runs/{id}`
- `POST /api/runs/{id}/cancel`
- `GET /api/runs/{id}/events` (SSE)
- `GET /api/runs/{id}/report.{html|pdf|json}`
- `GET /api/scoring/methodology`

Projects and their same-origin assessments are persisted in the configured database (SQLite for direct local use, PostgreSQL in Compose); the dashboard can filter by project and compare up to four stored runs from the same project or target. SQLite is intended for a single local worker. PostgreSQL migrations and concurrent queue claims are implemented, but user accounts and dashboard authentication are not. Keep the bundled dashboard and API bound to loopback unless you add an appropriate authentication and network-security layer.

For target authentication, put the secret in the BreakMyBot API and worker environment (for Compose, add a variable to the ignored local `.env` file), then enter only its variable name in the dashboard's Authentication section. Configure the header and scheme there as needed. Raw tokens are never entered in or stored by the browser.

The CLI intentionally does not call the preflight endpoint: a preflight followed by a run could invoke a side-effecting endpoint twice. Instead, the run records its own baseline response. Use preflight only when you explicitly want a separate target invocation and have confirmed it is safe.

## How Turbulence and MemoryScope fit in

BreakMyBot was designed alongside two public projects, but it is a standalone product. You do **not** need to clone, install, or run either one for the default workflow. These links are here so you can understand exactly what each project contributes—and where its boundaries are.

### [Turbulence](https://github.com/Diptesh2006/turbulence): a provider-reliability simulator

Turbulence is a separate FastAPI/Next.js pre-production simulator. Its current faults run at its own Concentrate provider-routing boundary; its Agent Safety Lab uses seeded simulated actions. Those simulations do not inject faults into an arbitrary application you connect to BreakMyBot. Turbulence publishes a [BreakMyBot capability manifest](https://github.com/Diptesh2006/turbulence/blob/main/apps/api/turbulence_api/api/routes/health.py) that marks external-target tool injection and an HTTP fault proxy unsupported. BreakMyBot does not count Turbulence simulation results as evidence about your agent and does not currently load Turbulence as a fault engine.

For actual target-side tool faults, BreakMyBot provides its own optional [FastAPI instrumentation](#opt-in-agent-instrumentation). Your agent must explicitly route tool calls through the adapter; an ordinary HTTP connection cannot intercept hidden tool calls.

### [MemoryScope](https://github.com/Diptesh2006/MemoryScope): grounded answer evaluation

MemoryScope (the local checkout folder has historically been named `MemoryGuard`) is a separate evaluator for a real external SSE chat endpoint. It checks user-provided reference cases with deterministic checks and a required nRouter reference-grounded judge. The public [API guide](https://github.com/Diptesh2006/MemoryScope/blob/main/API.md) documents its versioned contract. The current beta evaluates answers; it does **not** test persistent-memory lifecycle, cross-tenant isolation, or internal memory controls.

BreakMyBot's optional MemoryScope bridge is for that grounded answer evaluation only. It requires a separately running MemoryScope service, `MEMORYSCOPE_API_URL`, approved reference cases, server-side nRouter judge configuration in MemoryScope, and explicit consent before references and target answers are sent to the judge. If you do not run MemoryScope, skip this feature: the rest of BreakMyBot works without it.

For persistent memory experiments against your own application, use BreakMyBot's separate target-owned [memory adapter](docs/memory-adapter.md). It requires your agent to expose real snapshot/seed/restore callbacks; it does not import MemoryScope's internal demos or claim lifecycle tests without target evidence.

The [reference audit](docs/reference-audit.md) and [integration contracts and limits](docs/integration-boundaries.md) contain implementation-level detail. In short: BreakMyBot owns its runner, target instrumentation, memory adapter, scoring, and reports; both sibling projects remain separately versioned and optional.

Agent developers can opt into session-scoped tool fault injection. Install BreakMyBot in the target agent's Python environment and wire tool calls through its returned controller. The control API is installed only when `enabled=True`, requires a bearer token of at least 32 characters, and only applies faults to a request carrying the armed `X-BreakMyBot-Session` header:

```python
import os
from breakmybot.instrumentation import instrument

tool_faults = instrument(app, token=os.environ["BREAKMYBOT_INSTRUMENTATION_TOKEN"], enabled=True)
result = await tool_faults.invoke("catalog", catalog_lookup, "item-42")
```

Set the same high-entropy `BREAKMYBOT_INSTRUMENTATION_TOKEN` in the target process and BreakMyBot API environment. Add a JSON case array such as `examples/demo-agent/tool-faults.json`, then run `breakmybot test ... --instrumented-faults examples/demo-agent/tool-faults.json --allow-repeated-post` and confirm the target invocation, repeat authorization, and fault arming prompts.

Entries can be single faults or named chains with 2–5 ordered tool faults. Each scenario runs in an isolated session, the target returns injection events, and the runner attempts deletion even when arming or invocation fails. A scenario counts only if every configured injection is observed in order, cleanup succeeds, and an explicit expected substring is supplied (or the target request itself fails); otherwise it remains inconclusive/setup error. Sessions expire after five minutes and are bounded to 100 active sessions. Do not expose the control API to untrusted networks.

### Optional reference-grounded answer evaluation

If you also operate MemoryScope, configure `MEMORYSCOPE_API_URL` for that service and pass an approved case array with `--memory-cases cases.json`. BreakMyBot asks before sending references and target answers to MemoryScope's nRouter judge. Configure the judge key/model and target-origin allowlist in MemoryScope itself. This evaluates answers against references; it does not test persistent-memory lifecycle or internal memory controls. If your goal is testing the target's actual memory state, use the target-owned adapter and combined memory/tool-fault cases in [`docs/memory-adapter.md`](docs/memory-adapter.md).

### Optional AI scenario planner

The planner is opt-in (`--plan-with-ai`; `--plan-with-nrouter` is a legacy alias). nRouter is the MVP default. Set `NROUTER_API_KEY`, `NROUTER_BASE_URL`, and `BREAKMYBOT_PLANNER_MODEL` in the API and worker environment to use it. You can use another API key with an OpenAI-compatible chat-completions service: configure `BREAKMYBOT_LLM_PROVIDER`, `BREAKMYBOT_LLM_BASE_URL`, and either `BREAKMYBOT_LLM_API_KEY` or `BREAKMYBOT_LLM_API_KEY_ENV`, plus `BREAKMYBOT_LLM_MODEL` (or `BREAKMYBOT_PLANNER_MODEL`). Optional auth header/scheme, request path, and token parameter settings support compatible gateways. Native provider protocols that do not implement OpenAI-compatible chat completions are not supported by this generic route. Non-loopback provider URLs must use HTTPS. See [`.env.example`](.env.example) for all settings; keep secrets in the API/worker environment, never in dashboard requests or source control.

The provider is contacted only when you explicitly request planning and consent. BreakMyBot sends the application name, endpoint/method, and payload field names—not the payload values. Proposals are hypotheses and do not affect the deterministic score. To execute generated scenarios against an instrumented target, separately pass `--execute-planned-scenarios`; the CLI asks for consent and requires an operator-approved `--instrumented-faults` allowlist, an expected-response oracle, and repeated-call authorization for non-GET endpoints. Up to three scenarios run by default (`--max-planned-scenarios` can bound this from 1–5). Generated faults and assertions must stay within the approved allowlist. If provider setup fails, generated scenarios do not execute. Each assessment makes at most one bounded planning call; returned usage and cost are recorded when available.

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
