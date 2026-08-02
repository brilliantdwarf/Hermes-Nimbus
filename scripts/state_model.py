#!/usr/bin/env python3
"""Hermes Nimbus runtime state model.

This module intentionally contains no filesystem, database, HTTP, or rendering
code.  It reduces normalized Hermes lifecycle events into the existing Halo
activity states, which makes the transition rules deterministic and testable.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Iterable, Mapping, Optional, Set


ACTIVITY_STATES = frozenset({
    "idle",
    "thinking",
    "streaming",
    "executing",
    "input_needed",
    "completed",
    "error",
    "compacting",
})

EVENT_TYPES = frozenset({
    "turn.started",
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
    "model.started",
    "model.completed",
    "model.error",
    "output.delta",
    "output.completed",
    "tool.started",
    "tool.finished",
    "input.requested",
    "input.resolved",
    "compression.started",
    "compression.finished",
    "heartbeat",
    "legacy.idle",
})

SOURCE_CONFIDENCE = {
    "tui_gateway": 1.0,
    "runs_api": 1.0,
    "hermes_hook": 0.95,
    "manual": 1.0,
    "log": 0.55,
    "state_db": 0.30,
    "unknown": 0.0,
}

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:@/-]+")


def _safe_id(value: Any, fallback: str, limit: int = 192) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    text = _SAFE_ID_RE.sub("_", text)
    return text[:limit] or fallback


def _event_time(value: Any, now: Optional[float] = None) -> float:
    fallback = time.time() if now is None else float(now)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    # Bad clocks must not pin a transient state indefinitely.
    if parsed <= 0 or parsed > fallback + 300:
        return fallback
    return parsed


@dataclass(frozen=True)
class StateEvent:
    """A privacy-safe, normalized runtime event."""

    type: str
    occurred_at: float
    source: str = "unknown"
    session_id: str = "profile"
    turn_id: str = ""
    event_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        default_source: str = "unknown",
        now: Optional[float] = None,
    ) -> "StateEvent":
        if not isinstance(value, Mapping):
            raise ValueError("event must be an object")
        event_type = str(value.get("type") or "").strip()
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported event type: {event_type or '<empty>'}")
        payload = value.get("payload") or {}
        if not isinstance(payload, Mapping):
            raise ValueError("event payload must be an object")
        source = _safe_id(value.get("source") or default_source, "unknown", 48)
        return cls(
            type=event_type,
            occurred_at=_event_time(value.get("occurred_at"), now=now),
            source=source,
            session_id=_safe_id(value.get("session_id"), "profile"),
            turn_id=_safe_id(value.get("turn_id"), "", 192) if value.get("turn_id") else "",
            event_id=_safe_id(value.get("event_id"), "", 192) if value.get("event_id") else "",
            payload=dict(payload),
        )


@dataclass
class SessionRuntime:
    """Mutable runtime facts for one Hermes session."""

    session_id: str
    turn_id: str = ""
    turn_active: bool = False
    streaming: bool = False
    compacting: bool = False
    compaction_owns_turn: bool = False
    active_tools: Set[str] = field(default_factory=set)
    tool_event_at: Dict[str, float] = field(default_factory=dict)
    anonymous_tools: int = 0
    pending_inputs: Set[str] = field(default_factory=set)
    input_event_at: Dict[str, float] = field(default_factory=dict)
    anonymous_inputs: int = 0
    input_owns_turn: bool = False
    terminal_state: Optional[str] = None
    terminal_at: float = 0.0
    terminal_reason: str = ""
    last_event_at: float = 0.0
    last_source: str = "unknown"

    @property
    def tool_count(self) -> int:
        return len(self.active_tools) + self.anonymous_tools

    @property
    def input_count(self) -> int:
        return len(self.pending_inputs) + self.anonymous_inputs

    def clear_inflight(self) -> None:
        self.turn_active = False
        self.streaming = False
        self.compacting = False
        self.compaction_owns_turn = False
        self.active_tools.clear()
        self.tool_event_at.clear()
        self.anonymous_tools = 0
        self.pending_inputs.clear()
        self.input_event_at.clear()
        self.anonymous_inputs = 0
        self.input_owns_turn = False


class ProfileStateMachine:
    """Reduce per-session events into one of Halo's existing activity states."""

    def __init__(
        self,
        completed_hold: float = 30.0,
        event_cache_size: int = 2048,
        session_cache_size: int = 256,
    ):
        self.completed_hold = max(0.0, float(completed_hold))
        self.sessions: Dict[str, SessionRuntime] = {}
        self.last_event_at = 0.0
        self.last_source = "unknown"
        self.last_reason = ""
        self._manual_state: Optional[str] = None
        self._manual_at = 0.0
        self._event_ids: Set[str] = set()
        self._event_order: Deque[str] = deque()
        self._event_cache_size = max(64, int(event_cache_size))
        self._session_cache_size = max(8, int(session_cache_size))

    def _remember_event(self, event_id: str) -> bool:
        if not event_id:
            return True
        if event_id in self._event_ids:
            return False
        if len(self._event_order) >= self._event_cache_size:
            oldest = self._event_order.popleft()
            self._event_ids.discard(oldest)
        self._event_order.append(event_id)
        self._event_ids.add(event_id)
        return True

    def _session(self, session_id: str) -> SessionRuntime:
        runtime = self.sessions.get(session_id)
        if runtime is None:
            if len(self.sessions) >= self._session_cache_size:
                inactive = [
                    item
                    for item in self.sessions.values()
                    if not (
                        item.turn_active
                        or item.streaming
                        or item.compacting
                        or item.tool_count
                        or item.input_count
                    )
                ]
                # Prefer completed sessions, but keep a hard upper bound even
                # if a producer loses terminal events for many session IDs.
                candidates = inactive or list(self.sessions.values())
                oldest = min(candidates, key=lambda item: item.last_event_at)
                self.sessions.pop(oldest.session_id, None)
            runtime = SessionRuntime(session_id=session_id)
            self.sessions[session_id] = runtime
        return runtime

    @staticmethod
    def _correlation_id(payload: Mapping[str, Any], names: Iterable[str]) -> str:
        for name in names:
            value = payload.get(name)
            if value:
                return _safe_id(value, "", 192)
        return ""

    def ingest(self, event: StateEvent) -> bool:
        """Apply *event* unless it is duplicate or stale for its session."""
        if not self._remember_event(event.event_id):
            return False

        runtime = self._session(event.session_id)
        kind = event.type
        payload = event.payload
        tool_id = (
            self._correlation_id(payload, ("tool_call_id", "call_id"))
            if kind in {"tool.started", "tool.finished"}
            else ""
        )
        request_id = (
            self._correlation_id(
                payload, ("request_id", "approval_id", "tool_call_id")
            )
            if kind in {"input.requested", "input.resolved"}
            else ""
        )

        if (
            event.turn_id
            and runtime.turn_id
            and event.turn_id != runtime.turn_id
            and kind != "turn.started"
        ):
            return False

        # HTTP retries and multiple observers can deliver an older lifecycle
        # event after a newer one. Correlated parallel tool/input events are
        # ordered independently; unrelated stale events must never reopen a
        # completed turn.
        if (
            runtime.last_event_at
            and event.occurred_at < runtime.last_event_at
            and not tool_id
            and not request_id
        ):
            return False
        if tool_id and event.occurred_at < runtime.tool_event_at.get(tool_id, 0.0):
            return False
        if request_id and event.occurred_at < runtime.input_event_at.get(request_id, 0.0):
            return False

        self._manual_state = None
        runtime.last_event_at = max(runtime.last_event_at, event.occurred_at)
        runtime.last_source = event.source
        if event.turn_id:
            runtime.turn_id = event.turn_id
        self.last_event_at = max(self.last_event_at, event.occurred_at)
        self.last_source = event.source

        reason = str(payload.get("reason") or "")[:240]
        if reason:
            self.last_reason = reason

        if kind == "turn.started":
            self.last_reason = ""
            runtime.clear_inflight()
            runtime.turn_active = True
            runtime.terminal_state = None
            runtime.terminal_at = 0.0
            runtime.terminal_reason = ""
        elif kind == "model.started":
            runtime.turn_active = True
            runtime.input_owns_turn = False
            runtime.streaming = False
            runtime.terminal_state = None
        elif kind == "model.completed":
            runtime.streaming = False
        elif kind == "model.error":
            # Provider retries/fallback may recover, so only the turn terminal
            # event is allowed to show the error state.
            runtime.streaming = False
        elif kind == "output.delta":
            runtime.turn_active = True
            runtime.input_owns_turn = False
            runtime.streaming = True
            runtime.terminal_state = None
        elif kind == "output.completed":
            runtime.streaming = False
        elif kind == "tool.started":
            runtime.turn_active = True
            runtime.input_owns_turn = False
            runtime.streaming = False
            runtime.terminal_state = None
            if tool_id:
                runtime.tool_event_at[tool_id] = event.occurred_at
                runtime.active_tools.add(tool_id)
            else:
                runtime.anonymous_tools += 1
        elif kind == "tool.finished":
            if tool_id:
                runtime.tool_event_at[tool_id] = event.occurred_at
                runtime.active_tools.discard(tool_id)
            elif runtime.anonymous_tools:
                runtime.anonymous_tools -= 1
        elif kind == "input.requested":
            if not runtime.turn_active:
                # Approval hooks can identify their scope with session_key,
                # which is not necessarily the agent's session_id. Mark that
                # synthetic activity so resolving the request can release it.
                runtime.input_owns_turn = True
            runtime.turn_active = True
            runtime.terminal_state = None
            if request_id:
                runtime.input_event_at[request_id] = event.occurred_at
                runtime.pending_inputs.add(request_id)
            else:
                runtime.anonymous_inputs += 1
        elif kind == "input.resolved":
            if request_id:
                runtime.input_event_at[request_id] = event.occurred_at
                runtime.pending_inputs.discard(request_id)
            elif runtime.anonymous_inputs:
                runtime.anonymous_inputs -= 1
            if (
                runtime.input_owns_turn
                and not runtime.input_count
                and not runtime.tool_count
                and not runtime.streaming
                and not runtime.compacting
            ):
                runtime.turn_active = False
                runtime.input_owns_turn = False
        elif kind == "compression.started":
            runtime.compaction_owns_turn = not runtime.turn_active
            runtime.turn_active = True
            runtime.compacting = True
            runtime.streaming = False
            runtime.terminal_state = None
        elif kind == "compression.finished":
            runtime.compacting = False
            if runtime.compaction_owns_turn:
                runtime.turn_active = False
            runtime.compaction_owns_turn = False
        elif kind == "turn.completed":
            runtime.clear_inflight()
            runtime.terminal_state = "completed"
            runtime.terminal_at = event.occurred_at
            runtime.terminal_reason = reason or "completed"
            self.last_reason = runtime.terminal_reason
        elif kind == "turn.failed":
            runtime.clear_inflight()
            runtime.terminal_state = "error"
            runtime.terminal_at = event.occurred_at
            runtime.terminal_reason = reason or "failed"
            self.last_reason = runtime.terminal_reason
        elif kind in {"turn.cancelled", "legacy.idle"}:
            runtime.clear_inflight()
            runtime.terminal_state = None
            runtime.terminal_at = event.occurred_at
            runtime.terminal_reason = reason or kind.rsplit(".", 1)[-1]
            self.last_reason = runtime.terminal_reason
        # heartbeat intentionally updates freshness only.
        return True

    def ingest_mapping(
        self,
        value: Mapping[str, Any],
        *,
        default_source: str = "unknown",
        now: Optional[float] = None,
    ) -> bool:
        return self.ingest(
            StateEvent.from_mapping(value, default_source=default_source, now=now)
        )

    def set_manual_state(self, state: str, now: Optional[float] = None) -> None:
        if state not in ACTIVITY_STATES:
            raise ValueError(f"unsupported state: {state}")
        self._manual_state = state
        self._manual_at = time.time() if now is None else float(now)
        self.last_event_at = self._manual_at
        self.last_source = "manual"

    def clear_stale_legacy(self, timeout: float, now: Optional[float] = None) -> None:
        """Release states inferred only from legacy logs after their watchdog.

        Authoritative integrations use a separate heartbeat lease, so a
        long-running official tool call is not mistaken for idle.
        """
        current = time.time() if now is None else float(now)
        for runtime in self.sessions.values():
            if runtime.last_source != "log" or not runtime.turn_active:
                continue
            if current - runtime.last_event_at <= timeout:
                continue
            runtime.clear_inflight()
            runtime.terminal_state = None
            runtime.terminal_reason = "legacy_timeout"

    def clear_expired_authoritative(
        self,
        timeout: float,
        now: Optional[float] = None,
    ) -> bool:
        """Release authoritative in-flight state after its heartbeat lease ends."""
        current = time.time() if now is None else float(now)
        expired = False
        for runtime in self.sessions.values():
            active = bool(
                runtime.turn_active
                or runtime.streaming
                or runtime.compacting
                or runtime.tool_count
                or runtime.input_count
            )
            if runtime.last_source not in {"hermes_hook", "tui_gateway", "runs_api"}:
                continue
            if not active or current - runtime.last_event_at <= timeout:
                continue
            runtime.clear_inflight()
            runtime.terminal_state = None
            runtime.terminal_reason = "authoritative_lease_timeout"
            expired = True
        if expired:
            self.last_reason = "authoritative_lease_timeout"
        return expired

    def current_state(self, now: Optional[float] = None) -> str:
        if self._manual_state:
            return self._manual_state

        runtimes = tuple(self.sessions.values())
        if any(item.input_count for item in runtimes):
            return "input_needed"
        if any(item.compacting for item in runtimes):
            return "compacting"
        if any(item.tool_count for item in runtimes):
            return "executing"
        if any(item.streaming for item in runtimes):
            return "streaming"
        if any(item.turn_active for item in runtimes):
            return "thinking"

        current = time.time() if now is None else float(now)
        recent_terminal = [
            item
            for item in runtimes
            if item.terminal_state
            and current - item.terminal_at <= self.completed_hold
        ]
        if recent_terminal:
            return max(recent_terminal, key=lambda item: item.terminal_at).terminal_state or "idle"
        return "idle"

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        runtimes = tuple(self.sessions.values())
        active_sessions = sum(
            1
            for item in runtimes
            if item.turn_active or item.tool_count or item.input_count or item.compacting
        )
        return {
            "state": self.current_state(now=now),
            "source": self.last_source,
            "confidence": SOURCE_CONFIDENCE.get(self.last_source, 0.5),
            "observed_at": self.last_event_at or None,
            "reason": self.last_reason,
            "detail": {
                "active_sessions": active_sessions,
                "thinking_sessions": sum(1 for item in runtimes if item.turn_active),
                "running_tools": sum(item.tool_count for item in runtimes),
                "pending_inputs": sum(item.input_count for item in runtimes),
                "compacting_sessions": sum(1 for item in runtimes if item.compacting),
            },
        }
