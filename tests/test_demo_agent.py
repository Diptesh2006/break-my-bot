from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient


DEMO_PATH = Path(__file__).resolve().parents[1] / "examples" / "demo-agent" / "app.py"
SPEC = importlib.util.spec_from_file_location("breakmybot_demo_agent", DEMO_PATH)
assert SPEC and SPEC.loader
DEMO = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DEMO
SPEC.loader.exec_module(DEMO)


def test_demo_agent_memory_can_be_seeded_to_reproduce_and_reset_stale_answer():
    with TestClient(DEMO.app) as client:
        assert client.post("/api/memory/reset").status_code == 200
        seeded = client.post("/api/memory/seed", json={
            "value": "Customers may request a refund within 30 days of purchase."
        })
        assert seeded.json() == {"status": "seeded"}
        response = client.post("/api/chat/sse", json={
            "messages": [{"role": "user", "content": "What is the refund window?"}]
        })
        assert "30 days" in response.text
        assert "14 days" not in response.text

        reset = client.post("/api/memory/reset")
        assert reset.json() == {"status": "reset"}
        response = client.post("/api/chat/sse", json={
            "messages": [{"role": "user", "content": "What is the refund window?"}]
        })
        assert "14 days" in response.text
        assert "30 days" not in response.text


def test_demo_agent_detects_contradictory_memory_and_can_reproduce_unsafe_behavior(monkeypatch):
    contradiction = (
        "Refunds are available within 14 days. "
        "Refunds are not available within 14 days."
    )
    with TestClient(DEMO.app) as client:
        client.post("/api/memory/seed", json={"value": contradiction})
        safe = client.post("/api/chat", json={"message": "What is the refund window?"}).json()
        assert "conflicts" in safe["answer"]
        assert "contact support" in safe["answer"]

        monkeypatch.setenv("DEMO_UNSAFE_MEMORY_CONTRADICTION", "true")
        unsafe = client.post("/api/chat", json={"message": "What is the refund window?"}).json()
        assert unsafe["answer"] == contradiction
        monkeypatch.delenv("DEMO_UNSAFE_MEMORY_CONTRADICTION")
        assert client.post("/api/memory/reset").json() == {"status": "reset"}


def test_demo_agent_scopes_memory_by_session_and_can_reproduce_cross_session_leak(monkeypatch):
    DEMO.demo_session_memories.clear()
    DEMO.demo_session_memories["breakmybot-session-a"] = "Private session policy: refund window is 60 days."
    with TestClient(DEMO.app) as client:
        isolated = client.post("/api/chat", json={
            "session_id": "breakmybot-session-b", "message": "What is the refund window?"
        }).json()["answer"]
        assert "14 days" in isolated
        assert "60 days" not in isolated

        monkeypatch.setenv("DEMO_UNSAFE_CROSS_SESSION_MEMORY", "true")
        leaked = client.post("/api/chat", json={
            "session_id": "breakmybot-session-b", "message": "What is the refund window?"
        }).json()["answer"]
        assert "60 days" in leaked
        monkeypatch.delenv("DEMO_UNSAFE_CROSS_SESSION_MEMORY")
        DEMO.demo_session_memories.clear()


def test_demo_agent_opt_in_memory_adapter_restores_real_agent_memory(monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_MEMORY_ENABLED", "true")
    monkeypatch.setenv("BREAKMYBOT_MEMORY_TOKEN", "demo-memory-adapter-token-0123456789")
    spec = importlib.util.spec_from_file_location("breakmybot_demo_memory_enabled", DEMO_PATH)
    assert spec and spec.loader
    target = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = target
    spec.loader.exec_module(target)

    with TestClient(target.app) as client:
        manifest = client.get("/__breakmybot__/memory/capabilities").json()
        assert manifest["categories"] == [
            "outdated_knowledge", "contradictory_information", "cross_session_consistency",
            "memory_poisoning"
        ]
        session_id = "b" * 32
        headers = {"Authorization": "Bearer demo-memory-adapter-token-0123456789"}
        armed = client.put(f"/__breakmybot__/memory/sessions/{session_id}", headers=headers,
            json={"scope": {"fixture": "refund_policy"}, "seed": {
                "refund_policy": "Customers may request a refund within 30 days of purchase."}})
        assert armed.json()["seeded"] is True
        stale_answer = client.post("/api/chat/sse", json={
            "messages": [{"role": "user", "content": "What is the refund window?"}]})
        assert "30 days" in stale_answer.text

        cleaned = client.delete(f"/__breakmybot__/memory/sessions/{session_id}", headers=headers)
        assert cleaned.json()["cleaned_up"] is True
        current_answer = client.post("/api/chat/sse", json={
            "messages": [{"role": "user", "content": "What is the refund window?"}]})
        assert "14 days" in current_answer.text
        assert "30 days" not in current_answer.text

        cross_session_id = "c" * 32
        scoped = client.put(f"/__breakmybot__/memory/sessions/{cross_session_id}", headers=headers,
            json={"scope": {"session_id": "session-a"}, "seed": {
                "refund_policy": "Private session policy: refund window is 60 days."}})
        assert scoped.json()["seeded"] is True
        session_a = client.post("/api/chat", json={
            "session_id": "session-a", "message": "What is the refund window?"}).json()
        session_b = client.post("/api/chat", json={
            "session_id": "session-b", "message": "What is the refund window?"}).json()
        assert "60 days" in session_a["answer"]
        assert "14 days" in session_b["answer"]
        assert "60 days" not in session_b["answer"]
        scoped_cleanup = client.delete(
            f"/__breakmybot__/memory/sessions/{cross_session_id}", headers=headers)
        assert scoped_cleanup.json()["cleaned_up"] is True
        assert "session-a" not in target.demo_session_memories
