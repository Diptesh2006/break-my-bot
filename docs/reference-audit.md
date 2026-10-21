# Reference repository audit

Audit date: 2026-10-21

## Repositories

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
- Turbulence: no direct modifications planned for the initial slice; expose an integration boundary and reuse its verified capabilities only when they can operate against the connected target.
- MemoryScope: no direct modifications planned for the initial slice; document and optionally call its existing API when its prerequisites are explicitly met.

## Implementation status update

The initial BreakMyBot vertical slice is implemented and pushed to its `main` branch. It now also supports opt-in FastAPI target instrumentation for session-scoped tool faults; the run only scores an instrumented scenario when the target confirms the matching injection event, cleanup succeeds, and a response invariant is supplied. The current scoring methodology is `breakmybot-score-v3`, with weighted category scores, explicit observed-weight coverage, and reports listing observed scopes. The standalone demo demonstrates actual timeout recovery.

MemoryScope's `memoryscope-answer-evaluation-v1` contract is consumed by the BreakMyBot bridge; it explicitly advertises persistent memory lifecycle and tenant-isolation checks as unsupported. Turbulence's `breakmybot-turbulence-v1` contract advertises that its current simulator cannot inject faults into arbitrary external targets. These subsystem commits are local and were not pushed. A localhost CLI-to-agent end-to-end run was verified; Docker engine availability and live nRouter calls remain unverified.
