#!/usr/bin/env python3
"""Hermes Nimbus state detector.

Runtime events are authoritative. Hermes logs and ``state.db`` remain supported
as lower-confidence compatibility and restart-recovery sources.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from state_model import ProfileStateMachine, SOURCE_CONFIDENCE, StateEvent

try:
    import pwd
except ImportError:  # Windows
    pwd = None


def real_user_home() -> Path:
    """Resolve the real login home, not a profile-scoped HOME sandbox."""
    explicit = (
        os.environ.get("HERMES_NIMBUS_HOME")
        or os.environ.get("HERMES_HALO_HOME", "")
    ).strip()
    if explicit:
        return Path(explicit).expanduser()

    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if sudo_user and pwd is not None:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except (KeyError, OSError):
            pass

    if pwd is not None and hasattr(os, "getuid"):
        try:
            return Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (KeyError, OSError):
            pass

    home = Path(os.environ.get("HOME", "") or Path.home())
    parts = home.parts
    if ".hermes" in parts and "profiles" in parts:
        try:
            index = parts.index(".hermes")
            if index > 0:
                return Path(*parts[:index])
        except (ValueError, OSError):
            pass
    return home


def expand_user_path(path: str | os.PathLike) -> Path:
    """Expand ``~`` against the login home instead of a profile sandbox."""
    text = str(path)
    if text == "~":
        return real_user_home()
    if text.startswith("~/") or text.startswith("~\\"):
        return real_user_home() / text[2:]
    return Path(text).expanduser()


DEFAULT_CONFIG_PATH = expand_user_path(
    os.environ.get("HERMES_NIMBUS_CONFIG")
    or os.environ.get("HERMES_HALO_CONFIG")
    or str(real_user_home() / ".hermes" / "hermes-nimbus" / "config.json")
)

DEFAULT_PROFILE_COLORS = [
    "#3399ff", "#ff8830", "#33cc55", "#9944ff", "#ee3333",
    "#ff66aa", "#00cccc", "#ffaa00", "#6666ff", "#cc66ff",
]
DEFAULT_PROFILE_ICONS = [
    "🤖", "👨‍💼", "🔬", "👤", "👔", "✍️", "🛠️", "📊", "🎯", "🧩",
]

_LOG_TIME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
_SESSION_RE = re.compile(r"\bsession=([^\s,]+)")
_BRACKET_SESSION_RE = re.compile(r"\s\[([A-Za-z0-9_.:@/-]+)\]\s")
_TURN_RE = re.compile(r"\bturn(?:_id)?=([^\s,]+)")
_TOOL_CALL_RE = re.compile(r"\b(?:tool_call_id|call_id)=([^\s,]+)")
_REQUEST_RE = re.compile(r"\b(?:request_id|approval_id)=([^\s,]+)")
_TOOL_NAME_RE = re.compile(
    r"\b[Tt]ool\s+([A-Za-z0-9_.:-]+)\s+"
    r"(?:started|completed|failed|cancelled|returned\s+error)"
)


class HermesInstance:
    """State detector for one Hermes profile."""

    # Visual values are intentionally unchanged.
    STATES = {
        "idle": {
            "color": "#aaaaaa", "halo": "#cccccc", "period": 6.0,
            "dashes": [60, 30], "ms": 0, "md": 0,
            "amin": 0.30, "amax": 0.42, "br": 0, "rp": 0, "rpperiod": 0,
            "label": "空闲", "description": "正在等待任务",
        },
        "thinking": {
            "color": "#ff8830", "halo": "#ffdbb8", "period": 2.4,
            "dashes": [70, 35, 45, 30, 25, 20], "ms": 0.6, "md": 0.4,
            "amin": 0.45, "amax": 0.90, "br": 5.2, "rp": 0, "rpperiod": 0,
            "label": "思考中", "description": "正在推理分析",
        },
        "streaming": {
            "color": "#e8b100", "halo": "#fff0aa", "period": 2.0,
            "dashes": [60, 25, 45, 20, 35, 15], "ms": 0.7, "md": 0.30,
            "amin": 0.50, "amax": 0.85, "br": 3.5, "rp": 0, "rpperiod": 0,
            "label": "输出中", "description": "正在生成回复",
        },
        "executing": {
            "color": "#3399ff", "halo": "#bbddff", "period": 1.3,
            "dashes": [50, 25, 20, 20, 35, 25, 25, 22], "ms": 1.2, "md": 0.28,
            "amin": 0.60, "amax": 0.90, "br": 0, "rp": 0, "rpperiod": 0,
            "label": "执行中", "description": "正在调用工具",
        },
        "input_needed": {
            "color": "#ee3333", "halo": "#ffcccc", "period": 2.8,
            "dashes": [80, 50, 30, 25], "ms": 1.8, "md": 0.5,
            "amin": 0.52, "amax": 0.94, "br": 2.0, "rp": 0, "rpperiod": 0,
            "label": "等待输入", "description": "需要用户确认",
        },
        "completed": {
            "color": "#33cc55", "halo": "#bbffcc", "period": 5.0,
            "dashes": [70, 35, 45, 30, 25, 20], "ms": 0.5, "md": 0.3,
            "amin": 0.38, "amax": 0.84, "br": 6.0, "rp": 0, "rpperiod": 0,
            "label": "已完成", "description": "任务已完成",
        },
        "error": {
            "color": "#ff4444", "halo": "#ffcccc", "period": 1.5,
            "dashes": [40, 20, 30, 15], "ms": 2.0, "md": 0.6,
            "amin": 0.50, "amax": 0.95, "br": 1.5, "rp": 0, "rpperiod": 0,
            "label": "错误", "description": "出现错误",
        },
        "compacting": {
            "color": "#9944ff", "halo": "#ddccff", "period": 2.1,
            "dashes": [35, 20, 35, 20, 35, 20], "ms": 0.4, "md": 0.25,
            "amin": 0.38, "amax": 0.80, "br": 4.0, "rp": 0.12, "rpperiod": 1.6,
            "label": "压缩中", "description": "正在整理上下文",
        },
    }

    # Real provider calls and context compression in Hermes can legitimately
    # take several minutes.  A short CPU-style watchdog produced false idle
    # transitions during those operations; this timeout applies only to the
    # compatibility log source.
    IDLE_TIMEOUT = 600.0
    COMPLETED_HOLD = 30.0
    ACTIVE_WINDOW = 600.0
    DB_CHECK_INTERVAL = 10.0
    AUTHORITATIVE_SOURCE_TTL = 300.0
    AUTHORITATIVE_LEASE_TIMEOUT = 90.0

    def __init__(self, config: Mapping[str, Any]):
        self.id = str(config["id"])
        self.name = str(config["name"])
        self.description = str(config.get("description", ""))
        self.icon = str(config.get("icon", "🤖"))
        self.color = str(config.get("color", "#3399ff"))
        self.log_path = expand_user_path(config["log_path"])
        self.db_path = expand_user_path(config["db_path"]) if config.get("db_path") else None

        self.current_state = "idle"
        self.last_check: Optional[datetime] = None
        self.state_history: List[Dict[str, str]] = []
        self.machine = ProfileStateMachine(completed_hold=self.COMPLETED_HOLD)

        self._last_log_position = 0
        self._log_identity: Optional[tuple[int, int]] = None
        self._log_remainder = ""
        self._last_db_check = 0.0
        self._last_db_state: Optional[str] = None
        self._last_db_observed_at = 0.0
        self._last_official_event_at = 0.0
        self._effective_source = "unknown"
        self._effective_confidence = 0.0
        self._effective_observed_at = 0.0
        self._availability = "unknown"
        self._last_error = ""

        # Compatibility fields retained for the existing --debug output.
        self._last_activity_time = 0.0
        self._turn_ended_time = 0.0
        self._last_event: Optional[str] = None
        self._api_call_active = False
        self._streaming_active = False
        self._tool_executing = False

        self._init_from_recent_logs()

    def _profile_home(self) -> Path:
        if self.db_path:
            return self.db_path.parent
        if self.log_path.parent.name == "logs":
            return self.log_path.parent.parent
        return self.log_path.parent

    def _find_log_path(self) -> Path:
        if self.log_path.exists():
            return self.log_path
        root = real_user_home() / ".hermes"
        if self.id == "default":
            candidates = [root / "logs" / "agent.log", root / "logs" / "gateway.log"]
        else:
            candidates = [root / "profiles" / self.id / "logs" / "agent.log"]
        for candidate in candidates:
            if candidate.exists():
                self.log_path = candidate
                break
        return self.log_path

    def _find_db_path(self) -> Optional[Path]:
        if self.db_path and self.db_path.exists():
            return self.db_path
        root = real_user_home() / ".hermes"
        candidate = (
            root / "state.db"
            if self.id == "default"
            else root / "profiles" / self.id / "state.db"
        )
        if candidate.exists():
            self.db_path = candidate
        return self.db_path

    @staticmethod
    def _tail_lines(path: Path, limit: int = 300, max_bytes: int = 262_144) -> List[str]:
        try:
            size = path.stat().st_size
            start = max(0, size - max_bytes)
            with path.open("rb") as handle:
                handle.seek(start)
                data = handle.read()
            text = data.decode("utf-8", errors="ignore")
            lines = text.splitlines()
            if start and lines:
                lines = lines[1:]
            return lines[-limit:]
        except OSError:
            return []

    @staticmethod
    def _extract_log_timestamp(line: str) -> Optional[float]:
        match = _LOG_TIME_RE.match(line.strip())
        if not match:
            return None
        value = match.group(1).replace(",", ".")
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None

    def _log_session_id(self, line: str) -> str:
        bracket = _BRACKET_SESSION_RE.search(line)
        if bracket:
            return bracket.group(1)[:192]
        match = _SESSION_RE.search(line)
        if match:
            return match.group(1)[:192]

        # Provider client log records are sometimes emitted without a session
        # field. They are safe to associate only when one legacy turn is
        # active; otherwise ignoring the ambiguous record is preferable to
        # creating a synthetic turn that remains stuck after the real turn ends.
        active = [
            runtime.session_id
            for runtime in self.machine.sessions.values()
            if runtime.turn_active and runtime.last_source == "log"
        ]
        return active[0] if len(active) == 1 else ""

    @staticmethod
    def _match_value(pattern: re.Pattern[str], line: str) -> str:
        match = pattern.search(line)
        return match.group(1)[:192] if match else ""

    @staticmethod
    def _turn_end_type(reason: str) -> str:
        lowered = reason.lower()
        if "interrupted" in lowered or "cancelled" in lowered:
            return "turn.cancelled"
        failure_markers = (
            "max_iterations", "budget_exhausted", "error", "failed",
            "exhausted", "guardrail_halt", "context_too_small",
            "empty_response", "ollama_runtime",
        )
        if any(marker in lowered for marker in failure_markers):
            return "turn.failed"
        return "turn.completed"

    def _log_event(self, line: str, *, fallback_time: Optional[float] = None) -> Optional[StateEvent]:
        stripped = line.strip()
        if not stripped:
            return None
        occurred_at = self._extract_log_timestamp(stripped)
        if occurred_at is None:
            occurred_at = time.time() if fallback_time is None else fallback_time
        session_id = self._log_session_id(stripped)
        turn_id = self._match_value(_TURN_RE, stripped)
        payload: Dict[str, Any] = {}
        event_type = ""

        if "Turn ended" in stripped:
            match = re.search(r"\breason=(.*?)(?:\s+model=|\s+api_calls=|$)", stripped)
            reason = (match.group(1).strip() if match else "unknown")[:240]
            payload["reason"] = reason
            event_type = self._turn_end_type(reason)
        elif "conversation turn:" in stripped:
            event_type = "turn.started"
        elif re.search(r"context compression done|compression completed|compaction complete", stripped, re.I):
            event_type = "compression.finished"
        elif re.search(r"context compression started|compacting context|context compression", stripped, re.I):
            event_type = "compression.started"
        elif "tool_executor" in stripped:
            lowered = stripped.lower()
            tool_name = self._match_value(_TOOL_NAME_RE, stripped)
            tool_call_id = self._match_value(_TOOL_CALL_RE, stripped)
            if tool_name:
                payload["tool_name"] = tool_name
            if tool_call_id:
                payload["tool_call_id"] = tool_call_id
            if any(word in lowered for word in (" completed", " failed", " cancelled", "returned error")):
                payload["is_error"] = "failed" in lowered or "error" in lowered
                event_type = "tool.finished"
            elif re.search(r"\bstarted\b", lowered):
                event_type = "tool.started"
        elif "stream_request_complete" in stripped or "OpenAI client closed" in stripped:
            event_type = "model.completed"
        elif "OpenAI client created" in stripped:
            event_type = "model.started"
        elif re.search(r"approval request", stripped, re.I):
            request_id = self._match_value(_REQUEST_RE, stripped)
            if request_id:
                payload["request_id"] = request_id
            event_type = "input.requested"
        elif re.search(r"approval (?:response|resolved)", stripped, re.I):
            request_id = self._match_value(_REQUEST_RE, stripped)
            if request_id:
                payload["request_id"] = request_id
            event_type = "input.resolved"

        if not event_type:
            return None
        if not session_id:
            return None
        digest = hashlib.sha1(stripped.encode("utf-8", errors="ignore")).hexdigest()[:16]
        return StateEvent(
            type=event_type,
            occurred_at=occurred_at,
            source="log",
            session_id=session_id,
            turn_id=turn_id,
            event_id=f"log:{occurred_at:.6f}:{digest}",
            payload=payload,
        )

    def _parse_log_line(self, line: str) -> Optional[str]:
        """Compatibility helper used by diagnostics/tests."""
        event = self._log_event(line)
        return event.type if event else None

    def _init_from_recent_logs(self) -> None:
        log_path = self._find_log_path()
        self._find_db_path()
        if not log_path.exists():
            return
        try:
            stat = log_path.stat()
            self._last_log_position = stat.st_size
            self._log_identity = (stat.st_dev, stat.st_ino)
        except OSError:
            return

        now = time.time()
        for line in self._tail_lines(log_path):
            event = self._log_event(line, fallback_time=now)
            if event is None:
                continue
            age = max(0.0, now - event.occurred_at)
            if age <= self.ACTIVE_WINDOW:
                self.machine.ingest(event)
                self._last_event = event.type
        self.machine.clear_stale_legacy(self.IDLE_TIMEOUT, now=now)
        self.current_state = self.machine.current_state(now=now)
        self._sync_compatibility_flags()

    def _read_new_log_lines(self) -> List[str]:
        log_path = self._find_log_path()
        if not log_path.exists():
            return []
        try:
            stat = log_path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self._log_identity != identity or stat.st_size < self._last_log_position:
                self._last_log_position = 0
                self._log_remainder = ""
                self._log_identity = identity
            if stat.st_size <= self._last_log_position:
                return []
            with log_path.open("rb") as handle:
                handle.seek(self._last_log_position)
                data = handle.read()
                self._last_log_position = handle.tell()
            text = self._log_remainder + data.decode("utf-8", errors="ignore")
            if text and not text.endswith(("\n", "\r")):
                parts = text.splitlines(keepends=True)
                if parts and not parts[-1].endswith(("\n", "\r")):
                    self._log_remainder = parts.pop()
                else:
                    self._log_remainder = ""
                return [part.rstrip("\r\n") for part in parts]
            self._log_remainder = ""
            return text.splitlines()
        except OSError as exc:
            self._last_error = f"log:{type(exc).__name__}"
            return []

    def _authoritative_source_active(self, now: float) -> bool:
        return bool(
            self._last_official_event_at
            and now - self._last_official_event_at <= self.AUTHORITATIVE_SOURCE_TTL
        )

    def detect_from_log(self) -> Optional[str]:
        """Consume every new log event in order; never discard the batch tail."""
        lines = self._read_new_log_lines()
        if not lines:
            return None
        now = time.time()
        authoritative = self._authoritative_source_active(now)
        ingested = False
        for line in lines:
            event = self._log_event(line, fallback_time=now)
            if event is None:
                continue
            self._last_event = event.type
            self._last_activity_time = max(self._last_activity_time, event.occurred_at)
            # Continue advancing the log offset while an official source is
            # healthy, but do not let duplicate low-confidence events override it.
            if authoritative and event.type not in {
                "compression.started", "compression.finished"
            }:
                continue
            ingested = self.machine.ingest(event) or ingested
        if not ingested:
            return None
        self.machine.clear_stale_legacy(self.IDLE_TIMEOUT, now=now)
        self._sync_compatibility_flags()
        return self.machine.current_state(now=now)

    def _detect_from_db(self) -> Optional[str]:
        """Recover a low-confidence snapshot from Hermes' persistence DB."""
        db_path = self._find_db_path()
        if not db_path or not db_path.exists():
            return None
        now = time.time()
        if now - self._last_db_check < self.DB_CHECK_INTERVAL:
            return self._last_db_state
        self._last_db_check = now

        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
                conn.row_factory = sqlite3.Row
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
                }
                if not {"id", "session_id", "role", "timestamp"}.issubset(columns):
                    raise sqlite3.DatabaseError("messages schema is missing required columns")
                optional = [
                    name for name in ("content", "tool_calls", "tool_name", "finish_reason")
                    if name in columns
                ]
                select_columns = ["m.role", "m.timestamp", *[f"m.{name}" for name in optional]]
                filters = []
                if "active" in columns:
                    filters.append("COALESCE(m.active, 1) = 1")
                if "compacted" in columns:
                    filters.append("COALESCE(m.compacted, 0) = 0")
                where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
                query = (
                    f"SELECT {', '.join(select_columns)} FROM messages m "
                    f"{where_clause} ORDER BY m.timestamp DESC, m.id DESC LIMIT 1"
                )
                latest = conn.execute(query).fetchone()
        except (sqlite3.Error, OSError) as exc:
            self._last_error = f"state_db:{type(exc).__name__}"
            self._availability = "degraded"
            self._last_db_state = None
            return None

        if latest is None:
            self._last_db_state = "idle"
            self._last_db_observed_at = 0.0
            return "idle"

        observed_at = self._coerce_timestamp(latest["timestamp"])
        age = max(0.0, now - observed_at)
        role = str(latest["role"] or "")
        finish_reason = str(latest["finish_reason"] or "") if "finish_reason" in optional else ""
        tool_calls = str(latest["tool_calls"] or "") if "tool_calls" in optional else ""

        state = "idle"
        if role == "user" and age <= 15:
            state = "thinking"
        elif role == "tool" and age <= 15:
            # A tool row is a persisted result, not an in-flight execution.
            state = "thinking"
        elif role == "assistant" and tool_calls.strip() not in {"", "[]", "{}", "null"} and age <= 15:
            # Structured tool calls show intent, but cannot prove a tool is
            # still running, so the conservative fallback is generic activity.
            state = "thinking"
        elif role == "assistant" and age <= self.COMPLETED_HOLD:
            if finish_reason.lower() in {"error", "content_filter", "failed"}:
                state = "error"
            else:
                state = "completed"

        self._last_db_state = state
        self._last_db_observed_at = observed_at
        self._last_error = ""
        return state

    @staticmethod
    def _coerce_timestamp(value: Any) -> float:
        try:
            parsed = float(value or 0.0)
        except (TypeError, ValueError):
            text = str(value or "").strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(text).timestamp()
            except ValueError:
                return 0.0
        # Tolerate millisecond Unix timestamps in future schema variants.
        return parsed / 1000.0 if parsed > 10_000_000_000 else parsed

    @staticmethod
    def _process_start_time(pid: int) -> Optional[int]:
        try:
            return int(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21])
        except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
            return None

    @classmethod
    def _pid_alive(cls, pid: int, expected_start: Optional[int] = None) -> bool:
        if pid <= 1:
            return False
        try:
            os.kill(pid, 0)
        except PermissionError:
            pass
        except (ProcessLookupError, OSError):
            return False
        live_start = cls._process_start_time(pid)
        if expected_start is not None and live_start is not None:
            return live_start == expected_start
        return True

    @classmethod
    def _gateway_pid_alive(cls, pid_path: Path) -> Optional[bool]:
        try:
            raw = pid_path.read_text(encoding="utf-8").strip()
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        except (OSError, UnicodeError):
            return None

        if isinstance(value, Mapping):
            if value.get("kind") not in {None, "hermes-gateway"}:
                return False
            try:
                pid = int(value.get("pid"))
                expected_start = (
                    int(value["start_time"])
                    if value.get("start_time") is not None
                    else None
                )
            except (TypeError, ValueError):
                return False
            return cls._pid_alive(pid, expected_start)
        try:
            return cls._pid_alive(int(value))
        except (TypeError, ValueError):
            return False

    def _detect_availability(self) -> str:
        now = time.time()
        if self._last_official_event_at and now - self._last_official_event_at <= 60:
            return "online"
        pid_path = self._profile_home() / "gateway.pid"
        if pid_path.exists():
            alive = self._gateway_pid_alive(pid_path)
            if alive is None:
                return "degraded"
            return "online" if alive else "offline"
        if self._last_error:
            return "degraded"
        log_path = self._find_log_path()
        if log_path.exists():
            try:
                if now - log_path.stat().st_mtime <= 30:
                    return "online"
            except OSError:
                return "degraded"
        if log_path.exists() or (self.db_path and self.db_path.exists()):
            return "unknown"
        return "offline"

    def _detect_from_process(self) -> Optional[str]:
        """Deprecated compatibility hook.

        Process state is now used only for availability; CPU usage is not a
        valid proxy for model/tool activity.
        """
        return None

    def _sync_compatibility_flags(self) -> None:
        snapshot = self.machine.snapshot()
        state = snapshot["state"]
        self._api_call_active = state == "thinking"
        self._streaming_active = state == "streaming"
        self._tool_executing = snapshot["detail"]["running_tools"] > 0
        self._last_activity_time = self.machine.last_event_at
        recent_terminals = [
            runtime.terminal_at
            for runtime in self.machine.sessions.values()
            if runtime.terminal_state == "completed"
        ]
        self._turn_ended_time = max(recent_terminals, default=0.0)

    def ingest_event(self, value: Mapping[str, Any]) -> bool:
        """Ingest an authoritative normalized event for this profile."""
        event = StateEvent.from_mapping(value, default_source="hermes_hook")
        accepted = self.machine.ingest(event)
        if accepted:
            self._last_official_event_at = max(self._last_official_event_at, event.occurred_at)
            self._effective_source = event.source
            self._effective_confidence = SOURCE_CONFIDENCE.get(event.source, 0.9)
            self._effective_observed_at = event.occurred_at
            self.current_state = self.machine.current_state()
            self._sync_compatibility_flags()
        return accepted

    def detect_state(self) -> str:
        now = time.time()
        if self.machine.clear_expired_authoritative(
            self.AUTHORITATIVE_LEASE_TIMEOUT,
            now=now,
        ):
            # Let logs/DB recover the snapshot immediately when the official
            # producer disappears without a terminal event.
            self._last_official_event_at = 0.0
        self.detect_from_log()
        self.machine.clear_stale_legacy(self.IDLE_TIMEOUT, now=now)
        machine_state = self.machine.current_state(now=now)
        machine_age = now - self.machine.last_event_at if self.machine.last_event_at else float("inf")

        if (
            machine_state != "idle"
            or self.machine.last_source in {"hermes_hook", "tui_gateway", "runs_api", "manual"}
            or machine_age <= self.COMPLETED_HOLD
        ):
            state = machine_state
            self._effective_source = self.machine.last_source
            self._effective_confidence = SOURCE_CONFIDENCE.get(self.machine.last_source, 0.5)
            self._effective_observed_at = self.machine.last_event_at
        else:
            db_state = self._detect_from_db()
            if db_state is not None:
                state = db_state
                self._effective_source = "state_db"
                self._effective_confidence = SOURCE_CONFIDENCE["state_db"]
                self._effective_observed_at = self._last_db_observed_at
            else:
                state = "idle"
                self._effective_source = self.machine.last_source or "unknown"
                self._effective_confidence = SOURCE_CONFIDENCE.get(self._effective_source, 0.0)
                self._effective_observed_at = self.machine.last_event_at

        self.current_state = state
        self._availability = self._detect_availability()
        self._sync_compatibility_flags()
        return state

    @staticmethod
    def _iso_timestamp(value: float) -> Optional[str]:
        if not value:
            return None
        try:
            return datetime.fromtimestamp(value).astimezone().isoformat()
        except (OSError, OverflowError, ValueError):
            return None

    def get_state_info(self) -> Dict[str, Any]:
        state = self.detect_state()
        self.last_check = datetime.now().astimezone()
        if not self.state_history or self.state_history[-1]["state"] != state:
            self.state_history.append({
                "state": state,
                "timestamp": self.last_check.isoformat(),
            })
        self.state_history = self.state_history[-50:]

        runtime = self.machine.snapshot()
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "color": self.color,
            "state": state,
            "state_config": self.STATES.get(state, self.STATES["idle"]),
            "timestamp": self.last_check.isoformat(),
            "history": self.state_history[-10:],
            # Additive metadata; existing frontends safely ignore these fields.
            "availability": self._availability,
            "stale": self._availability in {"offline", "degraded"} and state != "idle",
            "source": self._effective_source,
            "confidence": self._effective_confidence,
            "observed_at": self._iso_timestamp(self._effective_observed_at),
            "reason": runtime["reason"],
            "detail": runtime["detail"],
            "diagnostic": self._last_error or None,
        }

    def set_state(self, state: str) -> bool:
        """Set a transient manual override, cleared by the next real event."""
        if state not in self.STATES:
            return False
        self.machine.set_manual_state(state)
        self.current_state = state
        self._effective_source = "manual"
        self._effective_confidence = SOURCE_CONFIDENCE["manual"]
        self._effective_observed_at = time.time()
        return True


class HermesMultiDetector:
    """Discover and manage multiple Hermes profile detectors."""

    def __init__(self, config_path: str | os.PathLike | None = None):
        self.config_path = expand_user_path(config_path or DEFAULT_CONFIG_PATH)
        self.instances: Dict[str, HermesInstance] = {}
        self.load_config()
        self._auto_discover_profiles()

    def load_config(self) -> None:
        try:
            if self.config_path.exists():
                with self.config_path.open("r", encoding="utf-8") as handle:
                    config = json.load(handle)
                instances = config.get("instances", [])
                if not isinstance(instances, list):
                    raise ValueError("config.instances must be an array")
                for instance_config in instances:
                    instance = HermesInstance(instance_config)
                    self.instances[instance.id] = instance
                if not self.instances:
                    self._create_default_config()
                    self._save_config()
            else:
                self._create_default_config()
                self._save_config()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"加载配置失败: {exc}")
            self.instances.clear()
            self._create_default_config()

    def _save_config(self) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            home = str(real_user_home())
            instances = []
            for instance in self.instances.values():
                log_path = str(instance.log_path).replace(home, "~", 1)
                db_path = str(instance.db_path).replace(home, "~", 1) if instance.db_path else ""
                instances.append({
                    "id": instance.id,
                    "name": instance.name,
                    "description": instance.description,
                    "log_path": log_path,
                    "db_path": db_path,
                    "color": instance.color,
                    "icon": instance.icon,
                })
            with self.config_path.open("w", encoding="utf-8") as handle:
                json.dump({"instances": instances}, handle, indent=2, ensure_ascii=False)
        except OSError as exc:
            print(f"保存配置失败: {exc}")

    def _auto_discover_profiles(self) -> None:
        profiles_dir = real_user_home() / ".hermes" / "profiles"
        if not profiles_dir.exists():
            return
        new_count = 0
        try:
            profile_dirs = sorted(profiles_dir.iterdir())
        except OSError:
            return
        for profile_dir in profile_dirs:
            if not profile_dir.is_dir():
                continue
            profile_id = profile_dir.name
            if profile_id in self.instances:
                continue
            log_path = profile_dir / "logs" / "agent.log"
            db_path = profile_dir / "state.db"
            if not log_path.exists() and not db_path.exists():
                continue
            index = len(self.instances)
            name = profile_id.capitalize()
            instance = HermesInstance({
                "id": profile_id,
                "name": name,
                "description": f"{name} 实例",
                "log_path": f"~/.hermes/profiles/{profile_id}/logs/agent.log",
                "db_path": f"~/.hermes/profiles/{profile_id}/state.db",
                "color": DEFAULT_PROFILE_COLORS[index % len(DEFAULT_PROFILE_COLORS)],
                "icon": DEFAULT_PROFILE_ICONS[index % len(DEFAULT_PROFILE_ICONS)],
            })
            self.instances[profile_id] = instance
            new_count += 1
            print(f"  [自动发现] 新增 Profile: {profile_id}")
        if new_count:
            self._save_config()

    def _create_default_config(self) -> None:
        root = real_user_home() / ".hermes"
        instance = HermesInstance({
            "id": "default",
            "name": "默认",
            "description": "默认 Hermes 节点",
            "log_path": str(root / "logs" / "agent.log"),
            "db_path": str(root / "state.db"),
            "color": "#3399ff",
            "icon": "🤖",
        })
        self.instances["default"] = instance
        self._auto_discover_profiles()

    def get_all_states(self) -> List[Dict[str, Any]]:
        return [instance.get_state_info() for instance in self.instances.values()]

    def get_instance_state(self, instance_id: str) -> Optional[Dict[str, Any]]:
        instance = self.instances.get(instance_id)
        return instance.get_state_info() if instance else None

    def set_instance_state(self, instance_id: str, state: str) -> bool:
        instance = self.instances.get(instance_id)
        return instance.set_state(state) if instance else False

    def ingest_event(self, instance_id: str, event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        instance = self.instances.get(instance_id)
        if instance is None:
            return None
        instance.ingest_event(event)
        return instance.get_state_info()

    def get_instance_ids(self) -> List[str]:
        return list(self.instances.keys())


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Hermes 多实例状态检测器")
    parser.add_argument("--list", action="store_true", help="列出所有实例状态")
    parser.add_argument("--instance", help="查看指定实例状态")
    parser.add_argument("--watch", action="store_true", help="持续监控所有实例")
    parser.add_argument("--debug", action="store_true", help="显示调试信息")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    args = parser.parse_args()

    detector = HermesMultiDetector(args.config)
    if args.debug:
        for instance in detector.instances.values():
            print(f"\n[{instance.id}] {instance.name}")
            print(f"日志: {instance._find_log_path()}")
            print(f"数据库: {instance._find_db_path()}")
            print(f"状态: {instance.detect_state()}")
            print(f"来源: {instance._effective_source}")
            print(f"可用性: {instance._detect_availability()}")
        return 0
    if args.instance:
        state = detector.get_instance_state(args.instance)
        if state is None:
            print(f"未找到实例: {args.instance}")
            return 1
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return 0
    if args.watch:
        last_states: Dict[str, str] = {}
        try:
            while True:
                for state in detector.get_all_states():
                    instance_id = state["id"]
                    if last_states.get(instance_id) != state["state"]:
                        print(
                            f"[{state['timestamp']}] {state['name']}: "
                            f"{last_states.get(instance_id, '?')} -> {state['state']}"
                        )
                        last_states[instance_id] = state["state"]
                time.sleep(0.5)
        except KeyboardInterrupt:
            return 0
    print(json.dumps(detector.get_all_states(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
