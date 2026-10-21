# Deterministic demo benchmark

`verify_demo.py` exercises the real BreakMyBot queue and runner against the isolated demo agent. It runs two repetitions each of a passing current-memory control, a seeded stale-memory bug, a single injected tool timeout, and an ordered two-tool failure chain. It emits the actual run IDs, expected statuses, captured evidence, execution success, simple reproducibility, local request timing, and a JSON artifact.

This deterministic product regression is distinct from the opt-in strategy comparison below. It reports zero provider tokens/cost because it makes no model calls. Neither harness estimates a general false-positive rate or establishes strategy superiority.

Keep all three processes on loopback. Use separate terminals from the repository root and set the same disposable test token in the API, worker, and demo agent:

```powershell
$env:BREAKMYBOT_DATA_DIR="$env:TEMP\breakmybot-benchmark"
$env:BREAKMYBOT_ALLOWED_HOSTS="127.0.0.1"
$env:BREAKMYBOT_INSTRUMENTATION_TOKEN="replace-with-a-random-local-test-token-at-least-32-characters"
python -m uvicorn breakmybot.api:app --host 127.0.0.1 --port 18088
```

In a second terminal, set the same `BREAKMYBOT_DATA_DIR`, `BREAKMYBOT_ALLOWED_HOSTS`, and `BREAKMYBOT_INSTRUMENTATION_TOKEN`, then run:

```powershell
python -m breakmybot.worker
```

In a third terminal:

```powershell
$env:BREAKMYBOT_INSTRUMENTATION_TOKEN="replace-with-a-random-local-test-token-at-least-32-characters"
$env:BREAKMYBOT_INSTRUMENTATION_ENABLED="true"
$env:DEMO_UNSAFE_TOOL_FALLBACK="true"
python -m uvicorn app:app --app-dir examples/demo-agent --host 127.0.0.1 --port 18089
```

Then, from the repository root:

```powershell
python benchmarks/verify_demo.py --api-url http://127.0.0.1:18088 --target-url http://127.0.0.1:18089 --output "$env:TEMP\breakmybot-demo-benchmark.json"
```

The harness refuses non-loopback HTTP endpoints, checks API and target health first, and resets the demo memory to its current value when it finishes. It writes only to the requested output path. Stop the demo services after use. Never run these mutating fixture endpoints on a real application.

## Fixed vs generated strategy pilot

`compare_strategies.py` compares four bounded arms on the same loopback demo application: fixed individual tool faults, nRouter-generated individual faults, fixed cascading fault recipes, and BreakMyBot-generated cascading recipes. Each arm gets four scenario recipes times the selected repetition count. The generated arms receive one proposal and may execute at most one scenario per assessment; they use the same exact fault settings and response oracle allowlist as the fixed arm. Cascade proposals may compose two distinct approved fault atoms. Every assessment also performs the same baseline invocation. The generated arms use a per-call completion-token cap (768 by default); provider-reported usage and cost are recorded, and absent cost data stays null.

This is an exploratory demo-agent pilot, not a powered study. It isolates tool-fault strategies; the deterministic harness above separately tests stale memory. Do not infer performance on arbitrary applications or claim superiority from the default two repetitions. A candidate that fails schema/capability/allowlist validation consumes its planned attempt but does not count as a valid target scenario.

Keep nRouter credentials out of command history and source control. Configure `NROUTER_API_KEY` and `BREAKMYBOT_PLANNER_MODEL` in the API, worker, and benchmark process environments. Keep the disposable instrumentation token in the API, worker, demo-agent, and benchmark environments. Restart the API and worker after setting provider configuration. With all three local services running, from the repository root run:

```powershell
python benchmarks/compare_strategies.py --api-url http://127.0.0.1:18088 --target-url http://127.0.0.1:18089 --repetitions 2 --planner-token-budget 768 --output "$env:TEMP\breakmybot-strategy-comparison.json"
```

The command rejects non-loopback endpoints. At the default budget it submits 16 bounded nRouter planning calls (8 per generated arm), executes at most 8 generated scenarios per arm (16 total), and writes run-level provenance and metrics to the requested artifact. `comparison_status` is `incomplete_generated_execution` if either generated arm failed to execute any valid scenario; the command then exits nonzero. The live provider comparison must still be run with an operator-configured key/model; mocked provider tests do not count as a research result.
