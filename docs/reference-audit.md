# Reference repository audit

Audit date: 2026-10-21

## Repositories at initial audit

| Product | Local path | Remote | Revision | Working tree |
|---|---|---|---|---|
| BreakMyBot | `C:\Users\pdasv\OneDrive - vit.ac.in\Desktop\random stuff\break-my-bot` | `https://github.com/Diptesh2006/break-my-bot.git` | `959559a` | clean at audit start |
| Turbulence | `C:\Users\pdasv\OneDrive - vit.ac.in\Desktop\proj\turbulence\turbulence` | `https://github.com/Diptesh2006/turbulence.git` | `2d52319` | existing edits in Concentrate client/test and untracked `apps/web/.eslintrc.json`; preserved |
| MemoryScope (folder named MemoryGuard) | `C:\Users\pdasv\OneDrive - vit.ac.in\Desktop\proj\MemoryGuard` | `https://github.com/Diptesh2006/MemoryScope.git` | `f6454a8` | clean at audit start |

## Findings

### BreakMyBot

The initial repository contains only a short README. It has no backend, CLI, dashboard, test suite, persistence, reports, or container configuration. It is the integration host.

### Turbulence

Reusable modules include a FastAPI API, deterministic scenario and workload types, a simulation orchestrator, seeded fault rules, event persistence/replay within process memory, SSE run telemetry, deterministic metrics and reports, and a Next.js cockpit. The current fault injector sits at Turbulence's own Concentrate adapter boundary. Its provider and action safety scenarios are simulations; it cannot inject failures into an arbitrary external AI agent. Its README explicitly notes that run history and agent safety state are in-memory and its configured SQLAlchemy database is not wired into those stores.

Integration decision: use Turbulence as a separately operated subsystem behind an explicit scope contract. Do not import it through local filesystem paths or count its internal demo runs as target-agent tests. A real outbound dependency proxy or instrumented-agent adapter is needed before its fault injection can be attributed to an external target. No Turbulence source files were changed at the time of the initial audit; a later capability-manifest route was added in commit `6b0bf13` without touching the pre-existing Concentrate client/test edits or untracked web config.

### MemoryScope

The current beta accepts a real external SSE chat endpoint and user-supplied cases with approved references and expected behavior. It performs deterministic literal/citation checks, followed by a required nRouter semantic judge with quote grounding. Results and evidence are persisted in local SQLite. It is a single-user loopback beta and does not currently implement session seed/reset adapters or persistent-memory lifecycle tests. Older synthetic memory benchmarks are archived and not mounted by the current API.

Integration decision: use its documented API as an optional separately operated evaluator when nRouter credentials, explicit consent, SSE-compatible target endpoint, and reference cases are provided. Memory tests remain `UNSUPPORTED` in the base black-box profile unless the target exposes a supported memory-control adapter. Never transfer its internal/legacy demo scores into BreakMyBot runs. No MemoryScope source files were changed at the time of the initial audit; a later external-answer capability manifest was added in commit `1258e66`.

## Integration plan and limits

BreakMyBot owns target discovery, black-box invocation, run state, deterministic scoring, and report artifacts. Its initial target runner uses observed HTTP responses and explicitly marks hidden memory state and internal tool faults unavailable. Turbulence and MemoryScope remain independently versioned and operable. Future adapters should communicate through documented HTTP contracts or separately installable packages; there are no fragile path imports.

Current risks: localhost routing differs between host and Docker; Docker-side reachability must be confirmed by the runner. Arbitrary non-loopback targets must not be allowed by default. A response-only black-box assessment cannot prove semantic correctness or internal failure recovery without a user-supplied oracle or instrumentation. nRouter live calls are paid and require explicit configuration and consent; the default run must not make them.

## Planned changes

- BreakMyBot: build the first functional vertical slice: safe HTTP discovery and payload selection, baseline and bounded repeat/latency checks, persisted runs, deterministic score/coverage separation, CLI, portable HTML/JSON reports, Docker connectivity setup, demo target, and developer documentation.
- Turbulence: expose an integration boundary and reuse verified capabilities only when they can operate against the connected target. Its capability manifest was added and pushed; its own simulator remains independent and is not treated as target evidence.
- MemoryScope: document and optionally call its existing API when prerequisites are explicitly met. A versioned capability manifest and configurable nRouter base URL were added and pushed; persistent memory lifecycle is still outside the advertised capability.

## Implementation status update

The initial BreakMyBot vertical slice is implemented and pushed to its `main` branch. It now also supports opt-in FastAPI target instrumentation for session-scoped tool faults; the run only scores an instrumented scenario when the target confirms the matching injection event, cleanup succeeds, and a response invariant is supplied. The current scoring methodology is `breakmybot-score-v3`, with weighted category scores, explicit observed-weight coverage, and reports listing observed scopes. The standalone demo demonstrates actual timeout recovery.

Follow-up work expanded the standalone BreakMyBot repository with its own SQLite-backed API/worker stack, CLI, reports, demo, Next.js dashboard, optional server-side target authentication, persisted project grouping, and run comparison. Clone/run does not require either reference repository. A live local dashboard-to-API-to-worker-to-demo run and regressions in all three repositories are recorded in [`final-verification.md`](final-verification.md). The original repository revisions above describe the audit starting point, not current HEADs.

MemoryScope's `memoryscope-answer-evaluation-v1` contract is consumed by the BreakMyBot bridge; it explicitly advertises persistent memory lifecycle and tenant-isolation checks as unsupported. Its judge honors the configured nRouter-compatible base URL and requires HTTPS for non-loopback providers. Turbulence's `breakmybot-turbulence-v1` contract advertises that its current simulator cannot inject faults into arbitrary external targets. Capability contracts are pushed in their respective repositories. Separate-process worker and CLI end-to-end runs verified the localhost agent workflow, including passing and failing fault chains; Docker engine availability and live nRouter calls remain unverified.

## Current integration status (refreshed 2026-10-22)

- BreakMyBot is self-contained for its default workflow: FastAPI API, SQLite-backed worker/store, Typer CLI, deterministic demo, reports, and Next.js dashboard. The optional reference integrations are not needed to clone/run it.
- BreakMyBot consumes MemoryScope only for its advertised external SSE answer-evaluation contract. Imported nested answer/judge evidence is sanitized before persistence. Separately, BreakMyBot now owns a target-installed memory snapshot/seed/restore contract; its bundled demo verifies a stale-memory + tool-timeout composition. Neither feature executes MemoryScope's internal benchmarks against the selected target. The bridge uses mocked HTTP responses; a live MemoryScope+nRouter execution remains unverified.
- BreakMyBot does not import or execute Turbulence's internal simulator. Its own opt-in FastAPI instrumentation is target-scoped and verifies target-returned injection events plus cleanup. Turbulence itself still advertises no arbitrary external-target injector, so no Turbulence engine is reused as if it were target evidence.
- Current acceptance includes in-process demo discovery → preflight → queued worker → persisted scenarios → HTML/PDF/JSON artifacts; separate-process CLI/instrumentation and stale-memory fixture runs are documented in `final-verification.md`. The memory adapter supports all six operator-authored category labels and multi-invocation cases, but only the bundled stale-memory composition is exercised end to end.
- LLM-assisted runs now expose a structured investigation (observed baseline facts, inferred dependencies, failure-sensitive operations, candidate invariants, and coverage gaps). The HTML and PDF reports now present these findings separately and explicitly label them as hypotheses excluded from deterministic scoring; the model identity is included when available. This improves report transparency but is not a substitute for live provider verification.
- Remaining directive gaps include Docker engine startup and container-to-host networking, live nRouter/MemoryScope calls, adapting Turbulence's simulator into an external-target injector (its current contract explicitly says it cannot do so), persistent-memory integrations for real third-party stores beyond the target-owned generic adapter, the full set of five requested memory/tool combinations against representative targets, and live comparative strategy research. PostgreSQL migrations/storage are implemented, but the exact Compose PostgreSQL 16 stack remains unverified because the Docker engine was unavailable. These gaps are not claimed complete.

## Refreshed sibling repository regression evidence

On 2026-10-22, the current Turbulence API tree passed `python -m pytest -q` (86 passed) and `ruff check turbulence_api tests`; the existing Concentrate client/test edits and untracked `apps/web/.eslintrc.json` were preserved. The current MemoryScope backend passed `python -m pytest -q` (29 passed) and scoped Ruff on `app/beta.py tests/test_beta.py`. These are repository regression checks, not proof that either subsystem is currently used to inject or score faults in a BreakMyBot target.
