# Built-in semantic judge

BreakMyBot can run reference-grounded semantic evaluations without MemoryScope. Supply 1–20 operator-authored cases; each case makes one additional request to the selected target endpoint, and all eligible answers are evaluated in one bounded LLM call. This is separate from the investigator/planner and is opt-in per run.

## Configure and run

Configure a provider key and model in both API and worker environments. The judge uses `BREAKMYBOT_JUDGE_MODEL` when set, otherwise it shares the planner model. The default provider is nRouter; other supported providers use the same OpenAI-compatible configuration described in [`.env.example`](../.env.example). `BREAKMYBOT_JUDGE_TOKEN_BUDGET` bounds the completion tokens for the single batch call (default 2048, maximum 6000).

Create a case file:

```json
[
  {
    "id": "refund-window",
    "name": "Refund window",
    "question": "How long do I have to request a refund?",
    "reference": "Customers may request a refund within 14 days of purchase.",
    "expected": "State that the refund window is 14 days.",
    "payload": {"message": "How long do I have to request a refund?"},
    "required_literals": ["14"],
    "forbidden_literals": ["30 days"]
  }
]
```

Run against an authorized target:

```powershell
breakmybot test --url http://localhost:8000 --endpoint /api/chat --judge-cases cases.json
```

Each case reuses the selected method, endpoint, and target authentication. Non-GET cases require `--allow-repeated-post` and its safety confirmation because the target is called once per case. The CLI separately asks before sending questions, references, expected behavior, and redacted target answers to the configured judge. If consent is declined, no case calls or judge call are made. The dashboard offers the same feature through the reference-grounded judge case editor and asks separately before any additional non-GET invocations and provider transmission.

## Interpretation and safeguards

- Required/forbidden literal checks run first. A deterministic mismatch is reported as a failure and is not sent to the model.
- Eligible answers are sent together in one provider request, bounded to 256 KiB of judge input and a configurable completion-token budget. There are no automatic retries.
- The structured response must include exactly one judgment per case. Pass/fail requires verbatim quotes that are checked against the recorded answer and approved reference. Invalid, missing, duplicate, or ungrounded judgments become setup errors, never passes.
- Judgments below 0.65 confidence become inconclusive. The model cannot set the aggregate score; the fixed scoring rubric consumes persisted scenario states and continues to report coverage separately.
- Reports include provider/model, usage/cost when returned, confidence, rationale, and grounded quotes. Treat judgments as fallible and review them before acting; this is not a legal, financial, security, or safety certification.
- Case requests and references are stored with the queued job until execution completes. Protect the local database. Credentials are read server-side and are not sent by the browser.

This feature evaluates answer correctness against supplied references. It does not inspect hidden application state, verify the reference itself, or test persistent memory lifecycle. For target-owned memory seed/snapshot/restore experiments, see the [memory adapter guide](memory-adapter.md). The optional MemoryScope service remains available for its separately operated SSE evaluation workflow.
