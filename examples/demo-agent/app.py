import json
import os

from fastapi import FastAPI, Request
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
from breakmybot.instrumentation import instrument
from breakmybot.memory_adapter import instrument_memory

app = FastAPI(title="BreakMyBot Deterministic Demo Agent", version="1.0.0")
tool_faults = instrument(app, token=os.getenv("BREAKMYBOT_INSTRUMENTATION_TOKEN", ""),
                         enabled=os.getenv("BREAKMYBOT_INSTRUMENTATION_ENABLED", "false").lower() == "true")


class ChatRequest(BaseModel):
    message: str


class MessageItem(BaseModel):
    role: str
    content: str


class SseChatRequest(BaseModel):
    messages: list[MessageItem]


class MemorySeedRequest(BaseModel):
    value: str


class RefundRetryRequest(BaseModel):
    idempotency_key: str | None = None


CURRENT_REFUND_POLICY = "Customers may request a refund within 14 days of purchase."
demo_memory = {"refund_policy": CURRENT_REFUND_POLICY}
fake_refund_service = {"side_effect_count": 0, "operations": {}}


def snapshot_demo_memory(_scope):
    return demo_memory.copy()


def seed_demo_memory_state(_scope, seed):
    if set(seed) != {"refund_policy"} or not isinstance(seed["refund_policy"], str):
        raise ValueError("demo memory adapter accepts only a refund_policy string")
    demo_memory["refund_policy"] = seed["refund_policy"]


def restore_demo_memory(_scope, snapshot):
    demo_memory.clear()
    demo_memory.update(snapshot)


memory_controls = instrument_memory(app,
    token=os.getenv("BREAKMYBOT_MEMORY_TOKEN", ""), snapshot=snapshot_demo_memory,
    seed=seed_demo_memory_state, restore=restore_demo_memory,
    categories=("outdated_knowledge", "contradictory_information"),
    enabled=os.getenv("BREAKMYBOT_MEMORY_ENABLED", "false").lower() == "true")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/chat")
async def chat(request: ChatRequest):
    question = request.message.casefold()
    if "refund" in question:
        policy = demo_memory["refund_policy"]
        normalized_policy = policy.casefold()
        if ("refunds are available within 14 days" in normalized_policy
                and "refunds are not available within 14 days" in normalized_policy):
            if os.getenv("DEMO_UNSAFE_MEMORY_CONTRADICTION", "false").lower() == "true":
                answer = policy
            else:
                answer = "The refund policy information conflicts. Please contact support."
            return {"answer": answer}
        try:
            answer = await tool_faults.invoke("refund_policy", lambda: demo_memory["refund_policy"])
        except TimeoutError:
            if os.getenv("DEMO_UNSAFE_TOOL_FALLBACK", "false").lower() == "true":
                answer = "Your refund is guaranteed for any purchase date."
            else:
                answer = "I cannot retrieve the refund policy right now. Please contact support."
    else:
        answer = "Hello. I am the deterministic BreakMyBot test agent."
    return {"answer": answer}


@app.post("/api/chat/chain")
async def chat_chain():
    answers = []
    tool_failed = False
    for tool_name, tool_call in (
        ("customer_context", lambda: "Customer purchased 45 days ago."),
        ("refund_policy", lambda: "Refunds are available within 14 days."),
    ):
        try:
            answers.append(await tool_faults.invoke(tool_name, tool_call))
        except (TimeoutError, RuntimeError):
            tool_failed = True
    if tool_failed:
        if os.getenv("DEMO_UNSAFE_TOOL_FALLBACK", "false").lower() == "true":
            return {"answer": "Your refund is guaranteed for any purchase date."}
        return {"answer": "I could not verify eligibility. Please contact support."}
    return {"answer": "Eligibility requires verified customer context and refund policy; please contact support."}


def _fake_refund_action(idempotency_key: str | None) -> dict:
    if idempotency_key and idempotency_key in fake_refund_service["operations"]:
        return fake_refund_service["operations"][idempotency_key]
    fake_refund_service["side_effect_count"] += 1
    receipt = {"receipt": f"refund-{fake_refund_service['side_effect_count']}"}
    if idempotency_key:
        fake_refund_service["operations"][idempotency_key] = receipt
    return receipt


@app.post("/api/refund/retry")
async def refund_retry(request: RefundRetryRequest, http_request: Request):
    if not http_request.headers.get("X-BreakMyBot-Session"):
        return {"message": "Isolated retry fixture ready; no side effect was performed.",
                "side_effect_count": fake_refund_service["side_effect_count"]}
    try:
        receipt = await tool_faults.invoke("refund_commit", _fake_refund_action, request.idempotency_key)
    except TimeoutError:
        # The dependency may already have committed. Retry with the same idempotency key.
        receipt = await tool_faults.invoke("refund_commit", _fake_refund_action, request.idempotency_key)
    count = fake_refund_service["side_effect_count"]
    return {"message": f"Refund completed with side-effect count {count}.",
            "receipt": receipt["receipt"], "side_effect_count": count}


@app.get("/api/refund/state")
def refund_state():
    return {"side_effect_count": fake_refund_service["side_effect_count"]}


@app.post("/api/refund/reset")
def reset_refund_state():
    fake_refund_service["side_effect_count"] = 0
    fake_refund_service["operations"].clear()
    return {"side_effect_count": 0, "reset": True}


@app.post("/api/chat/sse", response_class=StreamingResponse,
          responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
def chat_sse(request: SseChatRequest):
    question = request.messages[-1].content.casefold() if request.messages else ""
    answer = (demo_memory["refund_policy"] if "refund" in question
              else "Hello. I am the deterministic BreakMyBot test agent.")

    async def events():
        event = {"choices": [{"delta": {"content": answer}}]}
        yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/memory/seed")
def seed_demo_memory(request: MemorySeedRequest):
    """Set isolated demo memory to model current, stale, or contradictory state."""
    demo_memory["refund_policy"] = request.value
    return {"status": "seeded"}


@app.post("/api/memory/reset")
def reset_demo_memory():
    demo_memory["refund_policy"] = CURRENT_REFUND_POLICY
    return {"status": "reset"}
