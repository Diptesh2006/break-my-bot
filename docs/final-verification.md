# Verification record

Verification run: 2026-10-21 (Asia/Calcutta)

## BreakMyBot automated checks

- `python -m pytest -q`: 22 passed.
- `python -m ruff check src tests examples`: passed.
- `python -m compileall -q src examples tests`: passed.
- `git diff --check`: passed.

## Instrumented localhost end-to-end run

Started BreakMyBot API and the standalone demo agent as separate Uvicorn processes on loopback ports 18088 and 18089. Both processes received the same test-only instrumentation token; the demo was explicitly started with `BREAKMYBOT_INSTRUMENTATION_ENABLED=true`.

Ran through the interactive CLI (all three safety/opt-in confirmations accepted):

```text
python -m breakmybot.cli test --url http://127.0.0.1:18089 --endpoint /api/chat --payload-json '{"message":"refund policy"}' --instrumented-faults examples/demo-agent/tool-faults.json --allow-repeated-post
```

Observed run `run-4d3dbdcb6586`: `COMPLETED`, observed reliability score 100/100, coverage 77%, 10 passed, 0 failed, 3 unsupported. Scope list was `black-box HTTP` and `instrumented tool faults`. The target returned a matching `fault.injected` event for `refund_policy`/`timeout`, returned the configured `contact support` fallback, and the runner received successful session cleanup. HTML, PDF, and JSON reports were generated under the temporary test data directory (`%TEMP%\\breakmybot-e2e-data-v2\\reports\\run-4d3dbdcb6586`). Both test servers were stopped after inspection.

The overall score is based on all conclusive executed scenarios and is not a statistical safety estimate. The three unsupported results remain visible; the run did not test memory lifecycle or security policy behavior.

## Referenced subsystem regressions

- MemoryScope backend: `python -m pytest -q` in `MemoryGuard/backend`: 27 passed, including the capability-manifest test.
- Turbulence backend: `python -m pytest -q` in `turbulence/apps/api`: 86 passed; Ruff passed. The pre-existing edits in its Concentrate client/test and untracked web ESLint file were preserved and not staged.

## Not verified / limitations

- Docker Compose runtime was not verified: the prior `docker compose up -d --build` attempt could not connect to the Docker Desktop Linux engine. The manual localhost end-to-end path above verifies the HTTP/agent handshake, not container networking.
- No live nRouter request was made; no paid provider credential/model smoke test is claimed.
- The Turbulence manifest accurately reports that its simulator is not an external-target fault injector. A target-scoped Turbulence adapter and MemoryScope persistent-memory lifecycle fixtures remain unimplemented.
- The supported target instrumentation is opt-in FastAPI middleware and tool-wrapper integration; arbitrary agent frameworks are not automatically instrumented.
