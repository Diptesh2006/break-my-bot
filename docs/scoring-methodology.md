# Scoring methodology: breakmybot-score-v3

The aggregate covers only evidence-backed scenarios actually executed in a run. These may include black-box HTTP checks, opt-in target-instrumented tool-fault checks, and MemoryScope-grounded answer checks. The report records observed scopes explicitly. Each scenario is either `PASSED`, `VERIFIED_FAILURE`, `INCONCLUSIVE`, `SETUP_ERROR`, or `UNSUPPORTED`. Only passed and verified-failure scenarios are conclusive. Setup errors, inconclusive observations, and unsupported capabilities are not successes.

For at least three conclusive checks and one or more scored categories:

```text
Category score = 100 × passed checks in category / conclusive checks in category
Observed reliability score = sum(category weight × category score)
                         / sum(weights of observed scored categories)
```

Version 3 category weights are HTTP reliability 20%, response validity 10%, latency 10%, tool reliability 20%, memory integrity 15%, security 20%, and output correctness 5%. Weights are normalized over categories with conclusive evidence, so absent capabilities do not silently count as failures or successes. An aggregate is withheld unless at least 50% of total rubric weight is observed and at least three scenarios are conclusive. The report must also be read with observed-weight percent and scenario coverage; a score based on limited evidence is not broad reliability evidence. Categories without an assigned version-3 weight remain visible in category results but do not affect the aggregate.

Latency checks use the configured threshold, measured around the target request. HTTP checks require a 2xx status. Literal checks are case-insensitive and compare against the returned response body; they do not establish broad semantic correctness. An instrumented fault result is score-eligible only when the target advertises the session-scoped contract, reports matching injection provenance, and the runner confirms cleanup. With no explicit response invariant, successful fault delivery remains inconclusive. A response timeout is a verified availability/latency failure with its configured timeout bound recorded. Other connection and transport errors are setup errors unless a confirmed instrumented fault explains the observed target failure.

Coverage is calculated separately as:

```text
coverage = 100 × conclusive scenario results / all recorded scenarios
```

The report also lists unsupported capabilities and the scopes actually observed. A score does not describe tests that were unavailable, infer hidden agent state, or imply a statistical probability of safety. Persistent memory lifecycle, authorization boundaries, and security invariants still need compatible adapters and are not covered unless those scenarios were actually executed and reported.
