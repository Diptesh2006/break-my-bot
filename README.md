# BreakMyBot

**We break your AI so your users don't have to.**

An agent can ace a demo and still fail when a tool stalls, a retry repeats a write, or yesterday's answer collides with today's memory. BreakMyBot turns those edge cases into controlled, inspectable experiments against an AI app you own.

## From prompt to proof

BreakMyBot combines LLM reasoning with evidence-locked evaluation:

- **Investigate:** an LLM helps find promising tests and interpret reference cases. nRouter is the MVP default; OpenAI, OpenRouter, Groq, Gemini, Mistral, DeepSeek, and OpenAI-compatible gateways are supported.
- **Exercise:** check routes, response contracts, latency, and repeat behavior. Optional target-installed adapters expose real tool-fault and memory scenarios; hidden internals are never guessed at.
- **Verify:** scores come from observed responses, explicit oracles, and confirmed adapter events—not an LLM's confidence. Unsupported coverage stays visible as unsupported.
- **Review:** inspect run evidence and reports in the API, CLI, and dashboard.

Configure a provider and approve each AI-assisted run. Without credentials—or when consent is declined—deterministic checks still work and no model call is made. See [provider setup](docs/semantic-judge.md) and [scoring](docs/scoring-methodology.md).

BreakMyBot is a complete, standalone product. [Turbulence](https://github.com/Diptesh2006/turbulence) and [MemoryScope](https://github.com/Diptesh2006/MemoryScope) are optional, separately operated integrations—not prerequisites. The integration guide explains exactly what each can and cannot prove.

## How it fits among agent eval tools

[LangSmith](https://www.langchain.com/langsmith/evaluation) and [Braintrust](https://www.braintrust.dev/manifesto) emphasize datasets, experiments, traces, and the ongoing evaluation loop. [Inspect AI](https://inspect.aisi.org.uk/) is a composable framework for authoring and running a broad range of evaluations. [Promptfoo](https://www.promptfoo.dev/docs/red-team/agents/) is a close neighbor for configurable app evaluations and red teaming, including agent and tool-trajectory testing. These tools overlap with BreakMyBot; none should be reduced to “just a judge.”

BreakMyBot's narrower focus is a self-hosted test bench for an **already-running app you control**: exercise its HTTP boundary, then—only if the app opts in—inject tool faults or test memory through target-owned hooks. Behavioral pass/fail claims need an explicit oracle; injected-fault results also need target-confirmed injection and cleanup. Pick BreakMyBot when that operational failure-and-recovery workflow matters; use broader eval or observability platforms for their richer dataset, trace, collaboration, or production-monitoring workflows.

## Run it locally

Requires Python 3.11+ and Docker Compose. Keep your AI app running on port `8001` so it doesn't collide with BreakMyBot's API:

```powershell
git clone https://github.com/Diptesh2006/break-my-bot.git
cd break-my-bot
Copy-Item .env.example .env
# Set a private POSTGRES_PASSWORD. Add nRouter credentials/models for AI-assisted runs.
docker compose up -d --build

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
breakmybot doctor
breakmybot test --url http://localhost:8001
```

The API is at `http://127.0.0.1:8000`; the dashboard is at `http://127.0.0.1:3000`. To test a chat route, pass `--endpoint /api/chat --payload-json '{"message":"What is the refund policy?"}' --expected "14 days"`. Targets must be loopback or explicitly allowlisted. Only test systems you control. Writes, repeated calls, provider data sharing, and fault injection require opt-in; the local dashboard has no authentication and should not be exposed publicly.

No Docker? Install the package with `python -m pip install -e .`, then run `uvicorn breakmybot.api:app --host 127.0.0.1 --port 8000` and `python -m breakmybot.worker` in separate terminals. The [bundled demo](benchmarks/README.md) shows a real timeout-recovery evaluation.

## What's next

Planned—not shipped: broader first-party adapters for more agent and persistent-memory stacks; a target-aware network/dependency fault proxy that can connect to Turbulence; and larger, repeatable benchmark suites for comparing models and strategies. Today, a hidden capability is reported as untested—not simulated into a passing score.

## Build and explore

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
ruff check src tests examples benchmarks
```

Read the [memory adapter contract](docs/memory-adapter.md), [integration boundaries](docs/integration-boundaries.md), or the [reference-repository audit](docs/reference-audit.md). See [CONTRIBUTING](CONTRIBUTING.md) to help build the next failure mode into a test.
