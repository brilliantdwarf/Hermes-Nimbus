import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from hermes_state import HermesInstance  # noqa: E402


class HermesInstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.profile_home = Path(self.temp_dir.name) / "profile"
        self.log_path = self.profile_home / "logs" / "agent.log"
        self.log_path.parent.mkdir(parents=True)
        self.log_path.write_text("", encoding="utf-8")
        self.db_path = self.profile_home / "state.db"

    def instance(self, *, with_db=False):
        return HermesInstance({
            "id": "test-profile",
            "name": "Test Profile",
            "log_path": str(self.log_path),
            "db_path": str(self.db_path) if with_db else "",
        })

    def append_log(self, *lines):
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def test_log_batch_consumes_terminal_event_at_end(self):
        instance = self.instance()
        self.append_log(
            "INFO agent.turn_context: conversation turn: session=s1 model=test",
            "INFO run_agent: OpenAI client created (request)",
            "INFO agent.conversation_loop: Turn ended: "
            "reason=text_response(finish_reason=stop) model=test session=s1",
        )

        state = instance.detect_from_log()

        self.assertEqual(state, "completed")
        self.assertEqual(instance._last_event, "turn.completed")

    def test_partial_log_line_is_not_consumed_early(self):
        instance = self.instance()
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write("INFO conversation turn: session=s1")
        self.assertIsNone(instance.detect_from_log())

        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        self.assertEqual(instance.detect_from_log(), "thinking")

    def test_unscoped_provider_log_is_ignored_without_one_active_turn(self):
        instance = self.instance()
        event = instance._log_event("INFO run_agent: OpenAI client created (agent_init)")
        self.assertIsNone(event)

    def test_bracket_session_keeps_compression_pair_correlated(self):
        instance = self.instance()
        started = instance._log_event(
            "INFO [s1] agent.conversation_compression: "
            "context compression started: session=s1"
        )
        finished = instance._log_event(
            "INFO [s1] agent.conversation_compression: "
            "context compression done: session=new-session"
        )

        self.assertIsNotNone(started)
        self.assertIsNotNone(finished)
        self.assertEqual(started.session_id, "s1")
        self.assertEqual(finished.session_id, "s1")
        instance.machine.ingest(started)
        instance.machine.ingest(finished)
        self.assertEqual(instance.machine.current_state(), "idle")

    def test_turn_end_reasons_distinguish_cancel_failure_and_success(self):
        instance = self.instance()
        cases = {
            "interrupted_by_user": "turn.cancelled",
            "interrupted_during_api_call": "turn.cancelled",
            "max_iterations_reached(90/90)": "turn.failed",
            "text_response(finish_reason=stop)": "turn.completed",
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                event = instance._log_event(
                    f"INFO Turn ended: reason={reason} model=test session=s1"
                )
                self.assertIsNotNone(event)
                self.assertEqual(event.type, expected)

    def create_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    started_at REAL,
                    ended_at REAL
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls TEXT,
                    finish_reason TEXT,
                    timestamp REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    compacted INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO sessions(id, started_at, ended_at)
                VALUES ('open-but-stale', 1, NULL);
                INSERT INTO messages(session_id, role, timestamp)
                VALUES ('open-but-stale', 'user', 100);
                """
            )

    def test_db_does_not_treat_open_session_as_live_activity(self):
        self.create_db()
        instance = self.instance(with_db=True)

        with patch("hermes_state.time.time", return_value=1000):
            self.assertEqual(instance._detect_from_db(), "idle")

    def test_db_recent_assistant_message_is_completed_snapshot(self):
        self.create_db()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO messages(session_id, role, finish_reason, timestamp) "
                "VALUES (?, ?, ?, ?)",
                ("s2", "assistant", "stop", 995),
            )
        instance = self.instance(with_db=True)

        with patch("hermes_state.time.time", return_value=1000):
            self.assertEqual(instance._detect_from_db(), "completed")

    def test_gateway_json_pid_record_reports_online(self):
        instance = self.instance()
        pid_path = self.profile_home / "gateway.pid"
        pid_path.write_text(
            json.dumps({
                "pid": os.getpid(),
                "kind": "hermes-gateway",
                "start_time": instance._process_start_time(os.getpid()),
            }),
            encoding="utf-8",
        )

        self.assertEqual(instance._detect_availability(), "online")

        record = json.loads(pid_path.read_text(encoding="utf-8"))
        if record["start_time"] is not None:
            record["start_time"] += 1
            pid_path.write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(instance._detect_availability(), "offline")


if __name__ == "__main__":
    unittest.main()
