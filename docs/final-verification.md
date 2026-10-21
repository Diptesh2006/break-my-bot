# Verification record

Verification run: 2026-10-21 (Asia/Calcutta)

## BreakMyBot automated checks

- `python -m pytest -q`: 29 passed, including ordered fault-chain and SQLite worker execution/queue tests.
- `python -m ruff check src tests examples`: passed.
- `python -m compileall -q src examples tests`: passed.
- `git diff --check`: passed.
- `docker compose config -q`: passed after adding the separate worker service.

The API now stores queued run configurations and cancellation state in SQLite; a separate worker claims jobs under renewable leases. Queue claim, cancellation, stale-lease failure without automatic replay, API enqueue behavior, model reconstruction, and worker processing/acknowledgment are unit-tested. Actual container runtime and worker-to-host target networking remain unverified because the Docker Desktop Linux engine was unavailable in the environment.

## Instrumented localhost end-to-end run

Started BreakMyBot API and the standalone demo agent as separate Uvicorn processes on loopback ports 18088 and 18089. Both processes received the same test-only instrumentation token; the demo was explicitly started with `BREAKMYBOT_INSTRUMENTATION_ENABLED=true`.

Ran through the interactive CLI in both demo modes (all three safety/opt-in confirmations accepted for each run):

```text
python -m breakmybot.cli test --url http://127.0.0.1:18089 --endpoint /api/chat --payload-json '{"message":"refund policy"}' --profile quick --instrumented-faults examples/demo-agent/tool-faults.json --allow-repeated-post
```

Safe run `run-942d5b049ca6`: `COMPLETED`, observed reliability score 100/100, coverage 57%, 4 passed, 0 failed, 3 unsupported. Unsafe run `run-1dc5e5be6c02`: `COMPLETED`, score 75/100, coverage 57%, 3 passed, 1 verified failure, 3 unsupported. Both listed `black-box HTTP` and `instrumented tool faults` as observed scopes. In both runs the target returned a matching `fault.injected` event for `refund_policy`/`timeout`, and the runner successfully cleaned up the session. The safe mode returned the configured `contact support` fallback. The unsafe mode claimed "Your refund is guaranteed for any purchase date." and failed the explicit invariant. HTML, PDF, and JSON reports were generated under `%TEMP%\\breakmybot-fault-demo-final\\reports\\<run-id>`. PDF text extraction verified the category scorecard, Tool reliability result, and unsafe response finding. Both test servers were stopped after inspection.

The earlier recorded demo scores used scoring methodology v2 and are historical evidence only. Under current methodology v3, that recorded run's scenarios cover 45% of weighted rubric categories and would now report **Not established** (the aggregate requires at least 50%). The overall score is not a statistical safety estimate. Unsupported results remain visible; the run did not test memory lifecycle or security policy behavior.

## Separate-process worker and CLI end-to-end runs

The API, SQLite worker, and standalone demo agent ran as separate foreground processes on loopback ports 18088/18089. Healthy chain run `run-79e624419d55` completed at 100/100, 57% scenario coverage, and 60% rubric weight observed. Both injected events (`customer_context` exception, then `refund_policy` timeout) appeared in order; cleanup succeeded and the explicit `contact support` invariant passed. HTML, PDF, and JSON report endpoints returned HTTP 200.

With `DEMO_UNSAFE_TOOL_FALLBACK=true`, chain run `run-348269952c75` completed at 67/100, 57% coverage, and 60% rubric weight. The same injections and cleanup were confirmed; the response 'Your refund is guaranteed for any purchase date.' violated the supplied invariant and was recorded as `VERIFIED_FAILURE`. The PDF was parsed with pypdf and checked for score, finding, both tool names, and invariant evidence.

The interactive CLI was then run against those live services, affirmatively answering its safety and authorization prompts. CLI run `run-faada95ca714` completed at 67/100, 62% coverage, and 60% rubric weight (3 passed, 2 verified failures, 3 unsupported). Both the singleton timeout and ordered chain failed against the deliberately unsafe demo, with confirmed injection and cleanup evidence. HTML, PDF, and JSON were generated under `%TEMP%\\bmbm-worker-e2e\\reports\\run-faada95ca714`; PDF extraction confirmed score, findings, and fault provenance. All temporary processes were stopped. No Docker runtime claim is made.

CLI command:

```powershell
$env:BREAKMYBOT_API_URL='http://127.0.0.1:18088'
$env:BREAKMYBOT_INSTRUMENTATION_TOKEN='<local test token>'
python -m breakmybot.cli test --url http://127.0.0.1:18089 --endpoint /api/chat/chain --payload-json '{}' --profile quick --instrumented-faults examples/demo-agent/tool-faults.json --allow-repeated-post
```

## Referenced subsystem regressions

- MemoryScope backend: `python -m pytest -q` in `MemoryGuard/backend`: 29 passed, including capability and configurable nRouter base-URL tests. `ruff check app/beta.py tests/test_beta.py` passed; running Ruff over the full backend also surfaces 64 pre-existing style violations in archived `legacy_demo.py` and older tests.
- Turbulence backend: `python -m pytest -q` in `turbulence/apps/api`: 86 passed; Ruff passed. The pre-existing edits in its Concentrate client/test and untracked web ESLint file were preserved and not staged.

## Not verified / limitations

- `docker compose config -q` passed, but Docker runtime was not verified: `docker info` and the prior `docker compose up -d --build` attempt could not connect to the Docker Desktop Linux engine (`dockerDesktopLinuxEngine` pipe absent). The manual localhost end-to-end path above verifies the HTTP/agent handshake, not container networking.
- No live nRouter request was made; no paid provider credential/model smoke test is claimed.
- The Turbulence manifest accurately reports that its simulator is not an external-target fault injector. A target-scoped Turbulence adapter and MemoryScope persistent-memory lifecycle fixtures remain unimplemented.
- The supported target instrumentation is opt-in FastAPI middleware and tool-wrapper integration; arbitrary agent frameworks are not automatically instrumented.

## Bundled dashboard and current regression check

After adding the standalone Next.js dashboard to the BreakMyBot Compose stack, verification on 2026-10-21 included:

- `npm run build` in `dashboard/`: production build and TypeScript validation passed.
- `npm run typecheck` in `dashboard/`: passed.
- `docker compose config -q`: passed with API, worker, and dashboard services. This validates Compose configuration only, not container runtime.
- Started the production standalone Next.js server, BreakMyBot API, SQLite worker, and demo agent as separate local processes, with run data isolated under `%TEMP%\breakmybot-audit-20261021`.
- Requested the dashboard page and proxied `GET /api/health`: HTTP 200; the page contained the assessment history and server-side authentication controls.
- Created and polled a real assessment through the dashboard's API proxy against the demo agent: `run-7274e916f6a6`, `COMPLETED`, 43% scenario coverage, aggregate correctly withheld as `Not established` because evidence did not meet the rubric threshold. The JSON report endpoint returned HTTP 200.
- Stopped all four processes after verification. This local process test does not establish Docker networking or an actual Compose startup.

The dashboard accepts only the target credential environment-variable name and header/scheme; the secret stays in the API/worker environment and is not sent by the browser. No live paid-provider call was made.

Current source checks after the dashboard/authentication changes: BreakMyBot `pytest -q` passed (29); Turbulence `python -m pytest -q` and `ruff check .` passed (86); MemoryScope `python -m pytest -q` and `ruff check app/beta.py tests/test_beta.py` passed (29). BreakMyBot Ruff, compileall, and dashboard production build also passed. The benchmark-comparison harness, PostgreSQL/migrations, cross-run comparison UI, project management, target-scoped Turbulence engine, persistent-memory lifecycle adapters, live nRouter smoke test, and Docker runtime/network test remain incomplete or unverified; they are not represented as shipped capabilities.
