"""Opt-in, in-process tool fault injection for agent developers and test fixtures.

This does not instrument arbitrary HTTP targets. The application must route its tool
calls through ``ToolFaultController.invoke`` for an injected fault to reach a tool.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Literal

FaultKind = Literal["timeout", "exception", "empty_response", "malformed_response", "delay"]


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
class ToolFaultController(AbstractAsyncContextManager["ToolFaultController"]):
    """Route selected instrumented tool calls through deterministic test faults."""

    faults: tuple[ToolFault, ...] = ()
    events: list[dict[str, Any]] = field(default_factory=list)
    _counts: defaultdict[str, int] = field(default_factory=lambda: defaultdict(int), init=False)
    _active: bool = field(default=False, init=False)

    async def __aenter__(self) -> ToolFaultController:
        if self._active:
            raise RuntimeError("fault controller is already active")
        self._active = True
        self.events.clear()
        self._counts.clear()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._active = False

    async def invoke(self, tool_name: str, tool: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if not self._active:
            raise RuntimeError("use ToolFaultController inside 'async with' before invoking tools")
        self._counts[tool_name] += 1
        call_number = self._counts[tool_name]
        fault = next((item for item in self.faults if item.tool == tool_name and self._matches(item, call_number)), None)
        if fault is not None:
            self.events.append({"type": "fault.injected", "tool": tool_name, "kind": fault.kind,
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

        self.events.append({"type": "tool.call", "tool": tool_name, "call": call_number,
                            "fault_injected": fault is not None})
        try:
            result = (await tool(*args, **kwargs) if inspect.iscoroutinefunction(tool)
                      else await asyncio.to_thread(tool, *args, **kwargs))
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            self.events.append({"type": "tool.failed", "tool": tool_name, "call": call_number,
                                "error_type": type(exc).__name__})
            raise
        self.events.append({"type": "tool.completed", "tool": tool_name, "call": call_number})
        return result

    @staticmethod
    def _matches(fault: ToolFault, call_number: int) -> bool:
        return call_number % fault.every_n_calls == 0


def instrument_tool_calls(*faults: ToolFault) -> ToolFaultController:
    """Create a scoped controller; it changes behavior only while explicitly armed."""
    return ToolFaultController(faults=tuple(faults))
