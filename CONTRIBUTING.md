# Contributing

Thanks for helping improve BreakMyBot. Before opening a change:

1. Open an issue for substantial behavior or API changes.
2. Keep observed evidence separate from generated hypotheses and simulated events.
3. Do not treat unavailable scenarios as passes or use an LLM to set the overall score.
4. Add focused tests for changes to scoring, target discovery, or run lifecycle.
5. Run `python -m pytest -q` and `python -m ruff check src tests`.
6. Document target-network and side-effect implications for new scenarios.

Scenario and adapter contributions must define required capabilities, setup and cleanup behavior, evidence captured, and conditions that produce `UNSUPPORTED` or `INCONCLUSIVE`. Never include real credentials or customer data in fixtures.
