# BreakMyBot

**We break your AI so your users don't have to.**

BreakMyBot is an open-source reliability lab for AI applications. Point it at an agent you control, discover its API, run bounded checks, and turn verified failures into evidence and reports.

It tests the real application—not a copy of your agent. It records what the target actually returned, distinguishes unsupported checks from passes, and never lets an LLM assign the reliability score.

## What it does

- Discovers OpenAPI routes, request shapes, health endpoints, and streaming APIs; lets you choose or provide the invocation and payload.
- Checks real HTTP outcomes, response contracts, latency, and assertions you supply.
- Uses a configured LLM investigator and scenario planner to analyze bounded API context, surface hypotheses, and suggest experiments. Consent is requested for each run.
- Executes tool or memory faults only when your target explicitly installs the corresponding adapter. Confirmed injection and cleanup evidence are required.
- Calculates a deterministic score from conclusive observations, reports coverage separately, and exports portable JSON, HTML, and PDF reports.

## LLM reasoning and evidence

BreakMyBot combines probabilistic reasoning with deterministic evaluation. When configured, the LLM investigator/planner is offered by default in the CLI and dashboard. It receives endpoint schemas/descriptions, payload field names, and a compact baseline profile—not payload values or response text. Its findings and scenario ideas are hypotheses for review; they do not count as test observations or affect the score. Use `--no-plan-with-ai` to skip it in the CLI.

nRouter is the MVP-default provider. OpenAI, OpenRouter, Groq, Gemini, Mistral, DeepSeek, and other OpenAI-compatible gateways are supported. Set provider credentials and a model in the API and worker environments; see [`.env.example`](.env.example). Without credentials—or if you decline consent—BreakMyBot runs deterministic checks without provider calls. Executing AI-proposed faults is a separate opt-in requiring target instrumentation and an operator-approved fault/oracle allowlist.

## What access enables

| Target access | BreakMyBot can test | It cannot infer |
| --- | --- | --- |
| HTTP endpoint and valid request | Reachability, actual responses, contracts, latency, and supplied expectations | Hidden tools, private state, or semantic correctness without an oracle |
| Opt-in BreakMyBot tool adapter | Confirmed tool faults, ordered fault chains, target behavior, and cleanup | Tools or frameworks not wired to the adapter |
| Opt-in target memory adapter | Operator-authored seed/snapshot/restore cases the target advertises | Unimplemented memory categories or hidden memory behavior |
| Optional MemoryScope service | Reference-grounded judging of answers from an external SSE endpoint | Persistent-memory lifecycle, tenant isolation, or internal memory controls |

Turbulence and MemoryScope are independently versioned optional projects; neither is a hidden dependency. BreakMyBot does not import their local source or copy their demo scores. See [how the integrations fit](docs/integration-boundaries.md), the [reference audit](docs/reference-audit.md), and the projects’ [Turbulence](https://github.com/Diptesh2006/turbulence) and [MemoryScope](https://github.com/Diptesh2006/MemoryScope) repositories.

## Quick start

Clone this repository only. Python mode uses Python 3.11+; Docker mode requires Docker Compose.

```powershell
git clone https://github.com/Diptesh2006/break-my-bot.git
cd break-my-bot
```

For the full stack (PostgreSQL, API, worker, and dashboard):

```powershell
docker compose up -d --build
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

The dashboard is at `http://127.0.0.1:3000`. The local stack binds its ports to loopback. To use the host CLI, install it in a Python environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
breakmybot doctor
```

Start your AI application separately, then run:

```powershell
breakmybot test --url http://localhost:8000
```

For an endpoint without OpenAPI or when you want to specify the request explicitly:

```powershell
breakmybot test --url http://localhost:8000 --endpoint /api/chat --payload-json '{"message":"What is the refund policy?"}' --expected "14 days"
```

The CLI discovers with GET requests; the worker sends the selected invocation once as the baseline. Non-GET calls require confirmation. Repeats, concurrency, memory cases, and instrumented faults have additional opt-ins. Runs and reports are under `data/reports/<run-id>/`; use `breakmybot report --latest`, `breakmybot runs`, or the dashboard to review them.

Without Docker, install the package as above, then run the API and worker in separate terminals:

```powershell
uvicorn breakmybot.api:app --host 127.0.0.1 --port 8000
python -m breakmybot.worker
```

### Try the bundled demo

In another terminal, install its requirements and start the deterministic demo:

```powershell
python -m pip install -r examples/demo-agent/requirements.txt
python -m uvicorn app:app --app-dir examples/demo-agent --host 127.0.0.1 --port 8001
```

Then run `breakmybot test --url http://localhost:8001`. The demo is a local fixture, not an external LLM. Its stale-memory, tool-failure, and lost-acknowledgment examples are documented in the [demo verification guide](benchmarks/README.md) and [memory adapter guide](docs/memory-adapter.md).

## Safety and limits

Only test systems you are authorized to assess. Targets are restricted to loopback or explicitly allowlisted test networks; redirects are disabled. Docker-side checks run from the API/worker network, not the host CLI. For a host target, bind it to an interface Docker can reach and use a firewall rule limited to the local Docker network; see [target connectivity](docs/final-verification.md).

An ordinary HTTP connection cannot expose hidden tools or memory. Those checks remain unsupported unless the target provides the matching adapter. Scores describe only observed scenarios, are not safety probabilities or certifications, and are withheld when evidence is insufficient. The local dashboard has no user authentication; do not expose it publicly.

## Development

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
ruff check src tests examples benchmarks
```

Dashboard checks: `cd dashboard; npm ci; npm run typecheck; npm run build`.

Further detail: [scoring methodology](docs/scoring-methodology.md), [target memory adapter](docs/memory-adapter.md), [provider configuration](.env.example), [API](http://127.0.0.1:8000/docs) when running locally, and [final verification and limitations](docs/final-verification.md).
