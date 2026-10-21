import json

from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.responses import StreamingResponse

app = FastAPI(title="BreakMyBot Deterministic Demo Agent", version="1.0.0")


class ChatRequest(BaseModel):
    message: str


class MessageItem(BaseModel):
    role: str
    content: str


class SseChatRequest(BaseModel):
    messages: list[MessageItem]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/chat")
def chat(request: ChatRequest):
    question = request.message.casefold()
    if "refund" in question:
        answer = "Customers may request a refund within 14 days of purchase."
    else:
        answer = "Hello. I am the deterministic BreakMyBot test agent."
    return {"answer": answer}


@app.post("/api/chat/sse")
def chat_sse(request: SseChatRequest):
    question = request.messages[-1].content.casefold() if request.messages else ""
    answer = ("Customers may request a refund within 14 days of purchase."
              if "refund" in question else "Hello. I am the deterministic BreakMyBot test agent.")

    async def events():
        event = {"choices": [{"delta": {"content": answer}}]}
        yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
