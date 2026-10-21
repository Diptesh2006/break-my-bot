# BreakMyBot

**We break your AI so your users don't have to.**

BreakMyBot stress-tests AI applications you control. It probes the real API, uses LLMs to plan and interpret targeted evaluations, and turns observed behavior into evidence you can inspect—not a score invented by a model.

## What it does

- Discovers API routes and runs bounded HTTP, contract, latency, and expectation checks.
- Uses an optional LLM investigator and scenario planner; findings are hypotheses, not test results.
- Evaluates your reference cases with a built-in LLM judge, including evidence quotes and confidence.
- Supports opt-in tool and memory fault tests when the target installs BreakMyBot adapters.
- Produces deterministic scores from verified observations, with coverage and limitations reported separately.

LLM reasoning is central and enabled when configured and approved. nRouter is the MVP-default; OpenAI, OpenRouter, Groq, Gemini, Mistral, DeepSeek, and compatible gateways are supported. Provider calls require per-run consent. See [`.env.example`](.env.example) and the [semantic judge guide](docs/semantic-judge.md).

BreakMyBot runs standalone. [Turbulence](https://github.com/Diptesh2006/turbulence) and [MemoryScope](https://github.com/Diptesh2006/MemoryScope) are optional, separately versioned integrations—not dependencies. See [integration boundaries](docs/integration-boundaries.md) and the [reference audit](docs/reference-audit.md).

## Quick start

Clone this repository. Python mode requires Python 3.11+; the full stack requires Docker Compose.

```powershell
git clone https://github.com/Diptesh2006/break-my-bot.git
cd break-my-bot
docker compose up -d --build
```

The API is at `http://127.0.0.1:8000`; the dashboard is at `http://127.0.0.1:3000`. Configure a provider in the API and worker environments to enable LLM features. Then test an AI application you control:

```powershell
breakmybot test --url http://localhost:8000
```

For a specific chat endpoint:

```powershell
breakmybot test --url http://localhost:8000 --endpoint /api/chat --payload-json '{"message":"What is the refund policy?"}' --expected "14 days"
```

To run the stack without Docker, install the package with `python -m pip install -e .`, then start `uvicorn breakmybot.api:app --host 127.0.0.1 --port 8000` and `python -m breakmybot.worker` in separate terminals. See [the bundled demo](benchmarks/README.md) for a local target.

## Safety and limits

Only test authorized systems. Non-GET requests, repeated calls, provider sharing, and fault tests require explicit opt-in. Targets are restricted to loopback or allowlisted test networks. Hidden tools and memory are not inferable through ordinary HTTP; adapter-backed tests are required. Scores describe observed evidence, not safety certifications. The local dashboard has no authentication—do not expose it publicly.

## Development

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
ruff check src tests examples benchmarks
```

For methodology and implementation details, see [scoring](docs/scoring-methodology.md), [memory adapters](docs/memory-adapter.md), and [verification and limitations](docs/final-verification.md).
