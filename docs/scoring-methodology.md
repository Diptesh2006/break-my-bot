# Scoring methodology: breakmybot-score-v1

This release scores a narrow set of observed HTTP checks. Each scenario is either `PASSED`, `VERIFIED_FAILURE`, `SETUP_ERROR`, or `UNSUPPORTED`. Only passed and verified-failure scenarios are conclusive. Setup errors and unsupported capabilities are not successes.

For at least three conclusive checks:

```text
HTTP reliability score = 100 × passed conclusive checks / all conclusive checks
```

Latency checks use the configured threshold, measured around the target request. HTTP checks require a 2xx status. An optional literal check is case-insensitive and compares against the returned response body. It does not establish broad semantic correctness. A response timeout is a verified availability/latency failure with its configured timeout bound recorded. Other connection and transport errors are setup errors because the runner cannot establish what the application did.

Coverage is calculated separately as:

```text
coverage = 100 × conclusive scenario results / all recorded scenarios
```

The report also lists unsupported capabilities. A score does not describe tests that were unavailable, infer hidden agent state, or imply a statistical probability of safety. Memory behavior, internal tool injection, authorization boundaries, and security invariants need a compatible adapter and are not covered by this score.
