# Verification record

Verification run: 2026-10-21 (Asia/Calcutta)

## BreakMyBot automated checks

- `python -m pytest -q`: 24 passed, including an ordered two-tool fault-chain integration test with injection provenance, response invariant, and cleanup assertions.
- `python -m ruff check src tests examples`: passed.
- `python -m compileall -q src examples tests`: passed.
- `git diff --check`: passed.

## Instrumented localhost end-to-end run

Started BreakMyBot API and the standalone demo agent as separate Uvicorn processes on loopback ports 18088 and 18089. Both processes received the same test-only instrumentation token; the demo was explicitly started with `BREAKMYBOT_INSTRUMENTATION_ENABLED=true`.

Ran through the interactive CLI in both demo modes (all three safety/opt-in confirmations accepted for each run):

```text
python -m breakmybot.cli test --url http://127.0.0.1:18089 --endpoint /api/chat --payload-json '{"message":"refund policy"}' --profile quick --instrumented-faults examples/demo-agent/tool-faults.json --allow-repeated-post
```

Safe run `run-942d5b049ca6`: `COMPLETED`, observed reliability score 100/100, coverage 57%, 4 passed, 0 failed, 3 unsupported. Unsafe run `run-1dc5e5be6c02`: `COMPLETED`, score 75/100, coverage 57%, 3 passed, 1 verified failure, 3 unsupported. Both listed `black-box HTTP` and `instrumented tool faults` as observed scopes. In both runs the target returned a matching `fault.injected` event for `refund_policy`/`timeout`, and the runner successfully cleaned up the session. The safe mode returned the configured `contact support` fallback. The unsafe mode claimed "Your refund is guaranteed for any purchase date." and failed the explicit invariant. HTML, PDF, and JSON reports were generated under `%TEMP%\\breakmybot-fault-demo-final\\reports\\<run-id>`. PDF text extraction verified the category scorecard, Tool reliability result, and unsafe response finding. Both test servers were stopped after inspection.

The overall score is based on all conclusive executed scenarios and is not a statistical safety estimate. The three unsupported results remain visible; the run did not test memory lifecycle or security policy behavior.

The current test suite additionally verifies an ordered two-fault chain against an in-process FastAPI target, including both fault events and successful cleanup. A separate real-process CLI end-to-end run of the chain was attempted in this session but the desktop process-launch policy rejected launching the local servers; the prior recorded end-to-end evidence above is for the single-fault scenario only.

## Referenced subsystem regressions

- MemoryScope backend: `python -m pytest -q` in `MemoryGuard/backend`: 27 passed, including the capability-manifest test.
- Turbulence backend: `python -m pytest -q` in `turbulence/apps/api`: 86 passed; Ruff passed. The pre-existing edits in its Concentrate client/test and untracked web ESLint file were preserved and not staged.

## Not verified / limitations

- `docker compose config -q` passed, but Docker runtime was not verified: `docker info` and the prior `docker compose up -d --build` attempt could not connect to the Docker Desktop Linux engine (`dockerDesktopLinuxEngine` pipe absent). The manual localhost end-to-end path above verifies the HTTP/agent handshake, not container networking.
- No live nRouter request was made; no paid provider credential/model smoke test is claimed.
- The Turbulence manifest accurately reports that its simulator is not an external-target fault injector. A target-scoped Turbulence adapter and MemoryScope persistent-memory lifecycle fixtures remain unimplemented.
- The supported target instrumentation is opt-in FastAPI middleware and tool-wrapper integration; arbitrary agent frameworks are not automatically instrumented.
