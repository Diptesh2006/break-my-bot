# BreakMyBot

**We break your AI so your users don't have to.**

BreakMyBot stress-tests AI applications you control. LLMs help plan and interpret targeted evaluations; reproducible observations—not model opinions—determine the results.

## What it does

It discovers routes, checks behavior and contracts, evaluates your reference cases with a built-in LLM judge, and can test tool or memory failures when your app installs the optional adapters. Scores are deterministic and grounded in observed evidence; LLM-generated plans and findings remain hypotheses.

LLM reasoning is central: configure a provider and approve each run to enable planning and judging. nRouter is the MVP default; OpenAI, OpenRouter, Groq, Gemini, Mistral, DeepSeek, and compatible gateways are also supported. See [`.env.example`](.env.example) and the [semantic judge guide](docs/semantic-judge.md).

BreakMyBot runs standalone; [Turbulence](https://github.com/Diptesh2006/turbulence) and [MemoryScope](https://github.com/Diptesh2006/MemoryScope) are optional integrations, not dependencies. See [integration details](docs/integration-boundaries.md).

## Quick start

Requires Python 3.11+. For the full stack, install Docker Compose, then:

```powershell
git clone https://github.com/Diptesh2006/break-my-bot.git
cd break-my-bot
Copy-Item .env.example .env
# Edit .env and replace POSTGRES_PASSWORD with a private local password.
docker compose up -d --build
```

The API is at `http://127.0.0.1:8000` and dashboard at `http://127.0.0.1:3000`. Compose health checks wait for PostgreSQL, the API, and the dashboard to become responsive; inspect startup with `docker compose ps`. Configure your provider in the API and worker environments. To test an app you control:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
breakmybot test --url http://localhost:8000
```

Or target a chat endpoint:

```powershell
breakmybot test --url http://localhost:8000 --endpoint /api/chat --payload-json '{"message":"What is the refund policy?"}' --expected "14 days"
```

Without Docker, install with `python -m pip install -e .` and run `uvicorn breakmybot.api:app --host 127.0.0.1 --port 8000` plus `python -m breakmybot.worker` in separate terminals. Try the [bundled demo](benchmarks/README.md).

## Safety and limits

Test only systems you control. Writes, repeated calls, provider sharing, and fault injection require opt-in. Targets must be loopback or allowlisted. Hidden tools and memory require adapters. Scores are evidence, not safety certifications. The local dashboard has no authentication; do not expose it publicly.

## Development

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
ruff check src tests examples benchmarks
```

See [scoring](docs/scoring-methodology.md), [memory adapters](docs/memory-adapter.md), and [verification and limits](docs/final-verification.md) for details.
