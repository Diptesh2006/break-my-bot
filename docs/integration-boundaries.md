# Integration boundaries

## Turbulence adapter

Turbulence remains an independently versioned FastAPI/Next.js product. Its current deterministic injector evaluates faults at the Concentrate client boundary. The simulator creates simulated provider faults; they are not incidents in the target application. Its agent safety outcomes use fixtures and are likewise not external-target observations.

BreakMyBot's current public run model stores target endpoint, observed status/body evidence, measured latency, explicit assertions, and unsupported capabilities. Its new `breakmybot.instrumentation.ToolFaultController` is an opt-in in-process SDK seam: a developer routes tool calls through `controller.invoke(...)` inside an `async with` scope, and it can inject bounded timeout, exception, empty/malformed response, delay, or deterministic intermittent faults. Events identify which tool fault was injected. This does not instrument arbitrary HTTP apps, and the current BreakMyBot run orchestrator does not arm this SDK automatically; those scenarios must remain unsupported until an explicit target adapter connects setup, evidence collection, and cleanup to a run.

Turbulence remains independently versioned and its fault engine continues to act at its own Concentrate client boundary. Its `GET /api/integrations/breakmybot/capabilities` manifest advertises provider-boundary simulation but explicitly reports external-target tool injection and an HTTP fault proxy as unsupported. The manifest is a compatibility/scope contract, not an instruction for BreakMyBot to import Turbulence simulator outcomes. Turbulence is not yet a pluggable BreakMyBot `FaultEngine`. Sharing target results requires an explicit, target-scoped adapter contract with injection provenance and cleanup evidence. There are no absolute filesystem imports or copied Turbulence demo results.

## MemoryScope adapter

MemoryScope remains an independently versioned local beta at `Desktop/proj/MemoryGuard`. Its `/api/beta/capabilities` endpoint advertises the versioned `memoryscope-answer-evaluation-v1` contract. The supported capability is grounded answer evaluation against an external SSE endpoint; persistent memory lifecycle and tenant isolation are explicitly false. It requires explicit consent and a configured nRouter judge, and persists evidence locally. Lifecycle seed/reset adapters are not implemented; archived synthetic memory experiments are not external-target evidence.

BreakMyBot has an optional API bridge. The operator provides `MEMORYSCOPE_API_URL`, an SSE-compatible path on the selected target's same origin, approved reference cases, nRouter credentials/model in the MemoryScope server environment, and explicit consent before data is sent. The bridge checks the capability contract and judge readiness, starts and polls an actual MemoryScope run, then imports its per-case answer and grounded judge evidence into the BreakMyBot run. Missing services, incompatible contracts, or configuration produce setup errors; denied consent skips the evaluation. This evaluates answer correctness, not persistent memory state. Unsupported memory lifecycle/access categories remain unsupported. BreakMyBot's default path makes no nRouter calls.

## nRouter provider contract

The selected provider is nRouter, matching the user's direction and the existing MemoryScope integration. A future BreakMyBot AI planner/judge should use configurable `NROUTER_API_KEY`, `NROUTER_BASE_URL`, and pinned model IDs. Its output may propose scenarios or provide bounded semantic judgments; it cannot inject faults, invent observations, or set the deterministic aggregate score. The initial deterministic demonstration requires no provider key.
