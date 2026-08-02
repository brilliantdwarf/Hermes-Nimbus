"""Hermes plugin that publishes privacy-safe lifecycle events to Nimbus.

The hook callbacks never perform network I/O.  A single daemon worker keeps
event ordering intact and prevents a slow or unavailable dashboard from
blocking Hermes model/tool execution.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from typing import Any, Mapping
from urllib import error, parse, request


logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://127.0.0.1:8765/api/events"
_QUEUE: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=512)
_WORKER_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_PENDING_CONDITION = threading.Condition()
_PENDING_COUNT = 0
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_SCOPES: dict[str, dict[str, str]] = {}
_PROFILE_NAME = "default"
_SAFE_REASON_RE = re.compile(r"[^A-Za-z0-9_.:@/()=-]+")
_OPENER = request.build_opener(request.ProxyHandler({}))
_APPROVAL_LOCAL = threading.local()
_DELIVERY_ATTEMPTS = 3
_DELIVERY_TIMEOUT = 0.5
_RETRY_BASE_DELAY = 0.1
_FLUSH_TIMEOUT = 2.0
_HEARTBEAT_INTERVAL = 30.0
_ACTIVE_EVENT_TYPES = frozenset({
    "turn.started",
    "model.started",
    "output.delta",
    "tool.started",
    "input.requested",
    "compression.started",
})
_TERMINAL_EVENT_TYPES = frozenset({
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
    "legacy.idle",
})


def _text(value: Any, limit: int = 192) -> str:
    return str(value or "").strip()[:limit]


def _reason(value: Any, fallback: str) -> str:
    clean = _SAFE_REASON_RE.sub("_", _text(value, 160))
    return clean or fallback


def _session_id(values: Mapping[str, Any]) -> str:
    return _text(
        values.get("session_id")
        or values.get("session_key")
        or values.get("task_id")
        or f"{_PROFILE_NAME}:profile"
    )


def _request_id(values: Mapping[str, Any]) -> str:
    explicit = values.get("request_id") or values.get("approval_id")
    if explicit:
        return _text(explicit)
    return _text(
        "approval:"
        f"{values.get('session_key') or values.get('session_id') or _PROFILE_NAME}:"
        f"{values.get('pattern_key') or 'request'}"
    )


def _begin_approval_request() -> str:
    """Create a unique ID and retain it for the matching synchronous post-hook."""
    request_id = f"approval:{uuid.uuid4().hex}"
    stack = getattr(_APPROVAL_LOCAL, "request_ids", None)
    if stack is None:
        stack = []
        _APPROVAL_LOCAL.request_ids = stack
    stack.append(request_id)
    return request_id


def _end_approval_request(values: Mapping[str, Any]) -> str:
    stack = getattr(_APPROVAL_LOCAL, "request_ids", None)
    if stack:
        return stack.pop()
    # Defensive fallback for a post-hook delivered without its pre-hook.
    return _request_id(values)


def _event(kind: str, values: Mapping[str, Any], payload: Mapping[str, Any] | None = None):
    """Build an event without copying messages, args, results, or errors."""
    return {
        "schema_version": 1,
        "profile_id": _PROFILE_NAME,
        "type": kind,
        "occurred_at": time.time(),
        "session_id": _session_id(values),
        "turn_id": _text(values.get("turn_id")),
        "event_id": f"hook:{uuid.uuid4().hex}",
        "source": "hermes_hook",
        "payload": dict(payload or {}),
    }


def _endpoint() -> str:
    value = (
        os.environ.get("HERMES_NIMBUS_EVENT_URL")
        or os.environ.get("HERMES_HALO_EVENT_URL")
        or _DEFAULT_URL
    ).strip()
    parsed = parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return _DEFAULT_URL
    return value


def _deliver(event: Mapping[str, Any]) -> bool:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = (
        os.environ.get("HERMES_NIMBUS_EVENT_TOKEN")
        or os.environ.get("HERMES_HALO_EVENT_TOKEN", "")
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    outbound = request.Request(_endpoint(), data=data, headers=headers, method="POST")
    try:
        with _OPENER.open(outbound, timeout=_DELIVERY_TIMEOUT) as response:
            response.read(256)
            return 200 <= int(getattr(response, "status", 200)) < 300
    except (error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.debug("Hermes Nimbus event delivery failed: %s", type(exc).__name__)
        return False


def _deliver_with_retry(event: Mapping[str, Any]) -> bool:
    for attempt in range(_DELIVERY_ATTEMPTS):
        try:
            if _deliver(event):
                return True
        except Exception:
            logger.debug("Hermes Nimbus event delivery raised", exc_info=True)
        if attempt + 1 < _DELIVERY_ATTEMPTS:
            time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
    logger.debug(
        "Hermes Nimbus event dropped after retries: %s",
        event.get("type", "unknown"),
    )
    return False


def _track_activity(event: Mapping[str, Any]) -> None:
    session_id = _text(event.get("session_id"))
    if not session_id:
        return
    kind = str(event.get("type") or "")
    with _ACTIVE_LOCK:
        if kind in _TERMINAL_EVENT_TYPES:
            _ACTIVE_SCOPES.pop(session_id, None)
        elif kind in _ACTIVE_EVENT_TYPES:
            _ACTIVE_SCOPES[session_id] = {
                "session_id": session_id,
                "turn_id": _text(event.get("turn_id")),
            }


def _stop_activity(session_id: Any) -> None:
    value = _text(session_id)
    if not value:
        return
    with _ACTIVE_LOCK:
        _ACTIVE_SCOPES.pop(value, None)


def _heartbeat_events() -> list[dict[str, Any]]:
    with _ACTIVE_LOCK:
        scopes = [dict(value) for value in _ACTIVE_SCOPES.values()]
    return [_event("heartbeat", scope) for scope in scopes]


def _pending_finished() -> None:
    global _PENDING_COUNT
    with _PENDING_CONDITION:
        _PENDING_COUNT = max(0, _PENDING_COUNT - 1)
        if not _PENDING_COUNT:
            _PENDING_CONDITION.notify_all()


def _put_pending(event: dict[str, Any]) -> bool:
    global _PENDING_COUNT
    with _PENDING_CONDITION:
        try:
            _QUEUE.put_nowait(event)
        except queue.Full:
            return False
        _PENDING_COUNT += 1
        return True


def _drop_oldest_pending() -> bool:
    try:
        _QUEUE.get_nowait()
    except queue.Empty:
        return False
    _QUEUE.task_done()
    _pending_finished()
    return True


def _flush(timeout: float = _FLUSH_TIMEOUT) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout))
    with _PENDING_CONDITION:
        while _PENDING_COUNT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _PENDING_CONDITION.wait(remaining)
        return True


def _worker_loop() -> None:
    next_heartbeat = time.monotonic() + _HEARTBEAT_INTERVAL
    while True:
        wait_for = max(0.05, next_heartbeat - time.monotonic())
        try:
            item = _QUEUE.get(timeout=wait_for)
        except queue.Empty:
            item = None

        if item is not None:
            try:
                _deliver_with_retry(item)
            except Exception:
                # Observability must never affect the agent's lifecycle.
                logger.debug("Hermes Nimbus event worker failed", exc_info=True)
            finally:
                _QUEUE.task_done()
                _pending_finished()

        if time.monotonic() >= next_heartbeat:
            for heartbeat in _heartbeat_events():
                _deliver_with_retry(heartbeat)
            next_heartbeat = time.monotonic() + _HEARTBEAT_INTERVAL


def _ensure_worker() -> None:
    global _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(
            target=_worker_loop,
            name="hermes-nimbus-events",
            daemon=True,
        )
        _WORKER.start()


def _enqueue(event: dict[str, Any]) -> None:
    _ensure_worker()
    if _put_pending(event):
        return
    # Preserve the newest lifecycle boundary (especially turn completion)
    # when Halo has been unavailable long enough to fill the queue.
    if _drop_oldest_pending():
        _put_pending(event)


def _emit(kind: str, values: Mapping[str, Any], payload=None) -> None:
    try:
        event = _event(kind, values, payload)
        _track_activity(event)
        _enqueue(event)
    except Exception:
        logger.debug("Hermes Nimbus hook failed", exc_info=True)


def _flush_at_exit() -> None:
    if not _flush():
        logger.debug("Hermes Nimbus exit flush timed out")


atexit.register(_flush_at_exit)


def on_session_start(**kwargs: Any) -> None:
    _emit("heartbeat", kwargs)


def on_pre_llm_call(**kwargs: Any) -> None:
    _emit("turn.started", kwargs)


def on_post_llm_call(**kwargs: Any) -> None:
    _emit("turn.completed", kwargs, {"reason": "completed"})


def on_pre_api_request(**kwargs: Any) -> None:
    payload = {"api_request_id": _text(kwargs.get("api_request_id"))}
    _emit("model.started", kwargs, payload)


def on_post_api_request(**kwargs: Any) -> None:
    payload = {"api_request_id": _text(kwargs.get("api_request_id"))}
    _emit("model.completed", kwargs, payload)


def on_api_request_error(**kwargs: Any) -> None:
    category = kwargs.get("reason") or kwargs.get("status_code") or "provider_error"
    payload = {
        "api_request_id": _text(kwargs.get("api_request_id")),
        "reason": _reason(category, "provider_error"),
    }
    _emit("model.error", kwargs, payload)


def on_pre_tool_call(**kwargs: Any) -> None:
    tool_name = _text(kwargs.get("tool_name"), 96)
    tool_call_id = _text(kwargs.get("tool_call_id"))
    if tool_name.casefold() == "clarify":
        _emit(
            "input.requested",
            kwargs,
            {"request_id": tool_call_id, "kind": "clarify"},
        )
        return
    _emit(
        "tool.started",
        kwargs,
        {"tool_call_id": tool_call_id, "tool_name": tool_name},
    )


def on_post_tool_call(**kwargs: Any) -> None:
    tool_name = _text(kwargs.get("tool_name"), 96)
    tool_call_id = _text(kwargs.get("tool_call_id"))
    if tool_name.casefold() == "clarify":
        _emit(
            "input.resolved",
            kwargs,
            {"request_id": tool_call_id, "kind": "clarify"},
        )
        return
    status = _reason(kwargs.get("status"), "unknown")
    _emit(
        "tool.finished",
        kwargs,
        {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "status": status,
            "is_error": status in {"error", "blocked", "cancelled"},
        },
    )


def on_pre_approval_request(**kwargs: Any) -> None:
    _emit(
        "input.requested",
        kwargs,
        {"request_id": _begin_approval_request(), "kind": "approval"},
    )


def on_post_approval_response(**kwargs: Any) -> None:
    _emit(
        "input.resolved",
        kwargs,
        {
            "request_id": _end_approval_request(kwargs),
            "kind": "approval",
            "choice": _reason(kwargs.get("choice"), "unknown"),
        },
    )


def on_session_end(**kwargs: Any) -> None:
    if bool(kwargs.get("interrupted")):
        _emit(
            "turn.cancelled",
            kwargs,
            {"reason": _reason(kwargs.get("reason"), "interrupted")},
        )
    elif bool(kwargs.get("completed")):
        _emit("turn.completed", kwargs, {"reason": "completed"})
    else:
        _emit(
            "turn.failed",
            kwargs,
            {"reason": _reason(kwargs.get("reason"), "incomplete")},
        )


def on_session_finalize(**kwargs: Any) -> None:
    _stop_activity(kwargs.get("session_id"))
    if not _flush():
        logger.debug("Hermes Nimbus session finalize flush timed out")


def register(ctx) -> None:
    global _PROFILE_NAME
    _PROFILE_NAME = _text(getattr(ctx, "profile_name", None)) or "default"
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("api_request_error", on_api_request_error)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("pre_approval_request", on_pre_approval_request)
    ctx.register_hook("post_approval_response", on_post_approval_response)
