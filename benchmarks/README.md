# Deterministic demo benchmark

`verify_demo.py` exercises the real BreakMyBot queue and runner against the isolated demo agent. It runs two repetitions each of a passing current-memory control, a seeded stale-memory bug, a single injected tool timeout, and an ordered two-tool failure chain. It emits the actual run IDs, expected statuses, captured evidence, execution success, simple reproducibility, local request timing, and a JSON artifact.

This is a deterministic product regression benchmark, not a comparison of research methods. The current nRouter planner returns hypotheses that are capability-gated and explicitly unexecuted; the harness does not pretend those proposals were tested. It reports zero provider tokens/cost because it makes no model calls. Fixed-suite versus LLM-generated versus cascading-scenario strategy comparisons, broad false-positive estimates, and cost comparisons require an executable scenario representation and a pre-registered broader experiment.

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
