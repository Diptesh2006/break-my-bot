from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

MEMORY_CONTROL_PREFIX = "/__breakmybot__/memory"
MEMORY_CONTRACT = "breakmybot-memory-adapter-v1"
MEMORY_CATEGORIES = {
    "outdated_knowledge", "contradictory_information", "cross_session_consistency",
    "cross_agent_memory", "access_boundaries", "memory_poisoning",
}


class MemorySeedRequest(BaseModel):
    scope: dict[str, str] = Field(min_length=1, max_length=10)
    seed: dict[str, Any]

    @field_validator("scope")
    @classmethod
    def bounded_scope(cls, scope: dict[str, str]) -> dict[str, str]:
        if any(not key or len(key) > 100 or len(value) > 256 for key, value in scope.items()):
            raise ValueError("memory scope keys must be 1-100 characters and values at most 256 characters")
        return scope

    @model_validator(mode="after")
    def bounded_request(self):
        if len(self.model_dump_json().encode("utf-8")) > 16384:
            raise ValueError("memory seed request exceeds 16 KiB")
        return self


@dataclass
class _MemorySession:
    scope: dict[str, str]
    snapshot: Any
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class MemoryAdapterController:
    """Protected bridge to an application's actual memory store.

    Callbacks are supplied by the target application. Snapshot and restore must
    cover all state that seed() can modify for the supplied scope.
    """

    snapshot_callback: Callable[..., Any]
    seed_callback: Callable[..., Any]
    restore_callback: Callable[..., Any]
    categories: tuple[str, ...]
    _sessions: dict[str, _MemorySession] = field(default_factory=dict)
    _sweeper: asyncio.Task | None = None

    async def snapshot(self, scope: dict[str, str]) -> Any:
        result = self.snapshot_callback(scope)
        return await result if inspect.isawaitable(result) else result

    async def restore(self, scope: dict[str, str], snapshot: Any) -> None:
        result = self.restore_callback(scope, snapshot)
        if inspect.isawaitable(result):
            await result

    async def restore_session(self, session_id: str) -> None:
        state = self._sessions[session_id]
        await self.restore(state.scope, state.snapshot)
        del self._sessions[session_id]

    async def expire_sessions(self) -> None:
        cutoff = time.monotonic() - 900
        for session_id, state in list(self._sessions.items()):
            if state.created_at < cutoff:
                try:
                    await self.restore_session(session_id)
                except Exception:
                    # Retain failed restores for the next sweep or manual cleanup.
                    continue


def instrument_memory(
    app: FastAPI,
    *,
    token: str,
    snapshot: Callable[..., Any],
    seed: Callable[..., Any],
    restore: Callable[..., Any],
    categories: tuple[str, ...] | list[str],
    enabled: bool = False,
) -> MemoryAdapterController | None:
    """Expose consent-gated, rollback-safe controls for the target's real memory.

    Each callback receives a scope dictionary. The target owns the storage and
    must ensure snapshot/restore fully isolate and recover that scope.
    """
    if not enabled:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", token):
        raise ValueError("memory instrumentation token must be 32-256 URL-safe ASCII characters")
    normalized = tuple(dict.fromkeys(categories))
    if not normalized or any(category not in MEMORY_CATEGORIES for category in normalized):
        raise ValueError("memory adapter categories must be selected from the supported contract")
    controller = MemoryAdapterController(snapshot, seed, restore, normalized)
    router = APIRouter(prefix=MEMORY_CONTROL_PREFIX, include_in_schema=False)

    def authorize(authorization: str | None) -> None:
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def validate_session_id(session_id: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise HTTPException(status_code=422, detail="Invalid memory instrumentation session identifier")

    @router.get("/capabilities")
    async def capabilities():
        return {"contract": MEMORY_CONTRACT, "categories": list(controller.categories),
                "snapshot_seed_restore": True, "maximum_seed_bytes": 16384}

    @router.put("/sessions/{session_id}")
    async def arm(session_id: str, request: MemorySeedRequest,
                  authorization: str | None = Header(default=None)):
        authorize(authorization)
        validate_session_id(session_id)
        if session_id in controller._sessions:
            raise HTTPException(status_code=409, detail="Memory session already exists")
        if len(json.dumps(request.seed, ensure_ascii=False).encode("utf-8")) > 16384:
            raise HTTPException(status_code=413, detail="Memory seed exceeds 16 KiB")
        await controller.expire_sessions()
        if len(controller._sessions) >= 100:
            raise HTTPException(status_code=409, detail="Memory session limit reached")
        try:
            snapshot_value = await controller.snapshot(request.scope)
            controller._sessions[session_id] = _MemorySession(request.scope, snapshot_value)
            result = controller.seed_callback(request.scope, request.seed)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            prior = controller._sessions.get(session_id)
            if prior is not None:
                try:
                    await controller.restore_session(session_id)
                except Exception:
                    raise HTTPException(status_code=500, detail="Memory seed failed and rollback also failed") from exc
            raise HTTPException(status_code=500, detail="Memory seed failed; rollback was attempted") from exc
        return {"session_id": session_id, "seeded": True}

    @router.delete("/sessions/{session_id}")
    async def disarm(session_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        validate_session_id(session_id)
        if session_id not in controller._sessions:
            raise HTTPException(status_code=404, detail="Memory session not found")
        try:
            await controller.restore_session(session_id)
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Memory restore failed; session remains available for retry") from exc
        return {"session_id": session_id, "cleaned_up": True}

    app.include_router(router)

    async def sweep_sessions() -> None:
        while True:
            await asyncio.sleep(30)
            await controller.expire_sessions()

    async def start_sweeper() -> None:
        controller._sweeper = asyncio.create_task(sweep_sessions())

    async def stop_sweeper() -> None:
        if controller._sweeper is not None:
            controller._sweeper.cancel()
            try:
                await controller._sweeper
            except asyncio.CancelledError:
                pass
        for session_id in list(controller._sessions):
            try:
                await controller.restore_session(session_id)
            except Exception:
                continue

    app.add_event_handler("startup", start_sweeper)
    app.add_event_handler("shutdown", stop_sweeper)
    return controller
