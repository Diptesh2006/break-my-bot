"""Opt-in, in-process tool fault injection for agent developers and test fixtures.

This does not instrument arbitrary HTTP targets. The application must route its tool
calls through ``ToolFaultController.invoke`` for an injected fault to reach a tool.
"""

from __future__ import annotations

import asyncio
import contextvars
import hmac
import inspect
import re
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi import APIRouter
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, Field

FaultKind = Literal["timeout", "exception", "empty_response", "malformed_response", "delay"]
CONTROL_PREFIX = "/__breakmybot__"
INSTRUMENTATION_CONTRACT = "breakmybot-instrumentation-v1"


@dataclass(frozen=True)
class ToolFault:
    tool: str
    kind: FaultKind
    value: Any = None
    delay_seconds: float = 0
    every_n_calls: int = 1

    def __post_init__(self) -> None:
        if self.kind not in {"timeout", "exception", "empty_response", "malformed_response", "delay"}:
            raise ValueError("unsupported tool fault kind")
        if not self.tool.strip():
            raise ValueError("tool name must not be empty")
        if self.delay_seconds < 0 or self.delay_seconds > 30:
            raise ValueError("fault delay must be between 0 and 30 seconds")
        if self.every_n_calls < 1:
            raise ValueError("every_n_calls must be positive")


@dataclass
class _FaultSession:
    faults: tuple[ToolFault, ...]
    created_at: float = field(default_factory=time.monotonic)
    events: list[dict[str, Any]] = field(default_factory=list)
    counts: defaultdict[str, int] = field(default_factory=lambda: defaultdict(int))


@dataclass
class ToolFaultController(AbstractAsyncContextManager["ToolFaultController"]):
    """Route selected instrumented tool calls through deterministic test faults."""

    faults: tuple[ToolFault, ...] = ()
    events: list[dict[str, Any]] = field(default_factory=list)
    _counts: defaultdict[str, int] = field(default_factory=lambda: defaultdict(int), init=False)
    _active: bool = field(default=False, init=False)
    _sessions: dict[str, _FaultSession] = field(default_factory=dict, init=False)
    _current_session: contextvars.ContextVar[str | None] = field(
        default_factory=lambda: contextvars.ContextVar("breakmybot_session", default=None), init=False
    )

    async def __aenter__(self) -> ToolFaultController:
        if self._active:
            raise RuntimeError("fault controller is already active")
        self._active = True
        self.events.clear()
        self._counts.clear()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._active = False

    def arm(self, session_id: str, faults: tuple[ToolFault, ...]) -> None:
        cutoff = time.monotonic() - 300
        self._sessions = {key: value for key, value in self._sessions.items()
                          if value.created_at >= cutoff}
        if session_id in self._sessions:
            raise ValueError("instrumentation session already exists")
        if not faults:
            raise ValueError("at least one tool fault must be configured")
        if len(self._sessions) >= 100:
            raise ValueError("instrumentation session limit reached; clean up prior sessions")
        self._sessions[session_id] = _FaultSession(faults=faults)

    def session_context(self, session_id: str):
        state = self._sessions.get(session_id)
        if state is None or state.created_at < time.monotonic() - 300:
            self._sessions.pop(session_id, None)
            raise KeyError(session_id)
        return self._current_session.set(session_id)

    def reset_session_context(self, token) -> None:
        self._current_session.reset(token)

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        state = self._sessions.get(session_id)
        if state is None:
            raise KeyError(session_id)
        return list(state.events)

    def disarm(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    async def invoke(self, tool_name: str, tool: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if not self._active:
            session_id = self._current_session.get()
            if session_id is None and not self.faults:
                counts, events, faults = self._counts, self.events, ()
            elif session_id is None or session_id not in self._sessions:
                raise RuntimeError("use an armed instrumentation session or 'async with' before invoking tools")
            else:
                state = self._sessions[session_id]
                counts, events, faults = state.counts, state.events, state.faults
        else:
            counts, events, faults = self._counts, self.events, self.faults
        counts[tool_name] += 1
        call_number = counts[tool_name]
        fault = next((item for item in faults if item.tool == tool_name and self._matches(item, call_number)), None)
        if fault is not None:
            events.append({"type": "fault.injected", "tool": tool_name, "kind": fault.kind,
                           "call": call_number, "source": "instrumented_tool"})
            if fault.kind == "timeout":
                raise TimeoutError(f"Injected timeout at tool {tool_name}")
            if fault.kind == "exception":
                raise RuntimeError(f"Injected exception at tool {tool_name}")
            if fault.kind == "empty_response":
                return ""
            if fault.kind == "malformed_response":
                return fault.value if fault.value is not None else "{malformed response"
            if fault.kind == "delay":
                await asyncio.sleep(fault.delay_seconds)

        events.append({"type": "tool.call", "tool": tool_name, "call": call_number,
                       "fault_injected": fault is not None})
        try:
            result = (await tool(*args, **kwargs) if inspect.iscoroutinefunction(tool)
                      else await asyncio.to_thread(tool, *args, **kwargs))
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            events.append({"type": "tool.failed", "tool": tool_name, "call": call_number,
                           "error_type": type(exc).__name__})
            raise
        events.append({"type": "tool.completed", "tool": tool_name, "call": call_number})
        return result

    @staticmethod
    def _matches(fault: ToolFault, call_number: int) -> bool:
        return call_number % fault.every_n_calls == 0


def instrument_tool_calls(*faults: ToolFault) -> ToolFaultController:
    """Create a scoped controller; it changes behavior only while explicitly armed."""
    return ToolFaultController(faults=tuple(faults))


class _ArmRequest(BaseModel):
    tool: str = Field(min_length=1, max_length=100)
    kind: FaultKind
    delay_seconds: float = Field(default=0, ge=0, le=30)
    every_n_calls: int = Field(default=1, ge=1, le=100)


def instrument(app: FastAPI, *, token: str, enabled: bool = False) -> ToolFaultController:
    """Install opt-in, bearer-protected control routes and session context middleware.

    The agent must pass ``X-BreakMyBot-Session`` into this app on each invocation,
    and its tool calls must use the returned controller's ``invoke`` method.
    """
    if not enabled:
        return ToolFaultController()
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", token):
        raise ValueError("instrumentation token must be 32-256 URL-safe ASCII characters")
    controller = ToolFaultController()
    router = APIRouter(prefix=CONTROL_PREFIX, include_in_schema=False)

    def authorize(authorization: str | None) -> None:
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def validate_session_id(session_id: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise HTTPException(status_code=422, detail="Invalid instrumentation session identifier")

    @router.get("/capabilities")
    async def capabilities():
        return {"contract": "breakmybot-instrumentation-v1", "tool_fault_injection": True,
                "event_provenance": True, "session_scoped": True}

    @router.put("/sessions/{session_id}")
    async def arm(session_id: str, request: _ArmRequest, authorization: str | None = Header(default=None)):
        authorize(authorization)
        validate_session_id(session_id)
        try:
            controller.arm(session_id, (ToolFault(tool=request.tool, kind=request.kind,
                delay_seconds=request.delay_seconds, every_n_calls=request.every_n_calls),))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"session_id": session_id, "armed": True}

    @router.get("/sessions/{session_id}/events")
    async def events(session_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        validate_session_id(session_id)
        try:
            return {"session_id": session_id, "events": controller.read_events(session_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc

    @router.delete("/sessions/{session_id}")
    async def disarm(session_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        validate_session_id(session_id)
        if not controller.disarm(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"session_id": session_id, "cleaned_up": True}

    @app.middleware("http")
    async def breakmybot_session_scope(request, call_next):
        session_id = request.headers.get("X-BreakMyBot-Session")
        if session_id is None:
            return await call_next(request)
        try:
            context_token = controller.session_context(session_id)
        except KeyError:
            return JSONResponse(status_code=409, content={"detail": "Unknown BreakMyBot session"})
        try:
            return await call_next(request)
        finally:
            controller.reset_session_context(context_token)

    app.include_router(router)
    return controller


async def verify_remote_instrumentation(client: httpx.AsyncClient, control_url: str) -> bool:
    response = await client.get(f"{control_url}/capabilities")
    response.raise_for_status()
    manifest = response.json()
    return (manifest.get("contract") == INSTRUMENTATION_CONTRACT
            and manifest.get("tool_fault_injection") is True
            and manifest.get("event_provenance") is True
            and manifest.get("session_scoped") is True)


async def arm_remote_fault(client: httpx.AsyncClient, control_url: str, token: str,
                           session_id: str, fault: dict[str, Any]) -> None:
    response = await client.put(f"{control_url}/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"}, json=fault)
    response.raise_for_status()


async def read_remote_events(client: httpx.AsyncClient, control_url: str, token: str,
                             session_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"{control_url}/sessions/{session_id}/events",
        headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()
    events = response.json().get("events")
    if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
        raise ValueError("Instrumentation returned malformed event evidence")
    return events


async def cleanup_remote_session(client: httpx.AsyncClient, control_url: str,
                                 token: str, session_id: str) -> bool:
    response = await client.delete(f"{control_url}/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"})
    return response.is_success and response.json().get("cleaned_up") is True
