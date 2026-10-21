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
        assert manifest["categories"] == ["outdated_knowledge"]
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
