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
