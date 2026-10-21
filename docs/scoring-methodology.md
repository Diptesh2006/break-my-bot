# Scoring methodology: breakmybot-score-v2

The aggregate covers only evidence-backed scenarios actually executed in a run. These may include black-box HTTP checks, opt-in target-instrumented tool-fault checks, and MemoryScope-grounded answer checks. The report records observed scopes explicitly. Each scenario is either `PASSED`, `VERIFIED_FAILURE`, `INCONCLUSIVE`, `SETUP_ERROR`, or `UNSUPPORTED`. Only passed and verified-failure scenarios are conclusive. Setup errors, inconclusive observations, and unsupported capabilities are not successes.

For at least three conclusive checks:

```text
Observed reliability score = 100 × passed conclusive checks / all conclusive checks
```

Latency checks use the configured threshold, measured around the target request. HTTP checks require a 2xx status. Literal checks are case-insensitive and compare against the returned response body; they do not establish broad semantic correctness. An instrumented fault result is score-eligible only when the target advertises the session-scoped contract, reports matching injection provenance, and the runner confirms cleanup. With no explicit response invariant, successful fault delivery remains inconclusive. A response timeout is a verified availability/latency failure with its configured timeout bound recorded. Other connection and transport errors are setup errors unless a confirmed instrumented fault explains the observed target failure.

Coverage is calculated separately as:

```text
coverage = 100 × conclusive scenario results / all recorded scenarios
```

The report also lists unsupported capabilities and the scopes actually observed. A score does not describe tests that were unavailable, infer hidden agent state, or imply a statistical probability of safety. Persistent memory lifecycle, authorization boundaries, and security invariants still need compatible adapters and are not covered unless those scenarios were actually executed and reported.
