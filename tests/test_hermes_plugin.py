import importlib.util
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "integrations" / "hermes-nimbus"


def load_plugin():
    name = f"hermes_nimbus_plugin_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeContext:
    profile_name = "profile-a"

    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class HermesNimbusPluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.events = []
        self.plugin._enqueue = self.events.append
        self.context = FakeContext()
        self.plugin.register(self.context)

    def test_registers_documented_observer_hooks(self):
        self.assertEqual(
            set(self.context.hooks),
            {
                "on_session_start",
                "on_session_end",
                "on_session_finalize",
                "pre_llm_call",
                "post_llm_call",
                "pre_api_request",
                "post_api_request",
                "api_request_error",
                "pre_tool_call",
                "post_tool_call",
                "pre_approval_request",
                "post_approval_response",
            },
        )

    def test_turn_event_excludes_conversation_content(self):
        self.context.hooks["pre_llm_call"](
            session_id="session-a",
            turn_id="turn-a",
            user_message="private user text",
            conversation_history=[{"content": "private history"}],
        )

        event = self.events[-1]
        self.assertEqual(event["profile_id"], "profile-a")
        self.assertEqual(event["type"], "turn.started")
        self.assertEqual(event["session_id"], "session-a")
        self.assertEqual(event["payload"], {})
        self.assertNotIn("private", repr(event))

    def test_tool_events_keep_only_lifecycle_metadata(self):
        self.context.hooks["pre_tool_call"](
            session_id="session-a",
            turn_id="turn-a",
            tool_call_id="call-a",
            tool_name="terminal",
            args={"command": "secret command"},
        )
        self.context.hooks["post_tool_call"](
            session_id="session-a",
            turn_id="turn-a",
            tool_call_id="call-a",
            tool_name="terminal",
            status="error",
            result="secret output",
            error_message="secret error",
        )

        started, finished = self.events[-2:]
        self.assertEqual(started["type"], "tool.started")
        self.assertEqual(finished["type"], "tool.finished")
        self.assertEqual(finished["payload"]["status"], "error")
        self.assertTrue(finished["payload"]["is_error"])
        self.assertNotIn("secret", repr(started) + repr(finished))

    def test_clarify_and_approval_map_to_waiting_lifecycle(self):
        self.context.hooks["pre_tool_call"](
            session_id="session-a", tool_name="clarify", tool_call_id="clarify-a"
        )
        self.context.hooks["post_tool_call"](
            session_id="session-a", tool_name="clarify", tool_call_id="clarify-a"
        )
        self.context.hooks["pre_approval_request"](
            session_key="session-a", pattern_key="dangerous-command"
        )
        self.context.hooks["post_approval_response"](
            session_key="session-a",
            pattern_key="dangerous-command",
            choice="deny",
        )

        self.assertEqual(
            [event["type"] for event in self.events],
            [
                "input.requested",
                "input.resolved",
                "input.requested",
                "input.resolved",
            ],
        )
        self.assertEqual(
            self.events[2]["payload"]["request_id"],
            self.events[3]["payload"]["request_id"],
        )

    def test_repeated_approvals_get_distinct_correlated_ids(self):
        pre = self.context.hooks["pre_approval_request"]
        post = self.context.hooks["post_approval_response"]

        pre(session_key="session-a", pattern_key="same-pattern")
        post(session_key="session-a", pattern_key="same-pattern", choice="allow")
        first_pair = self.events[-2:]
        pre(session_key="session-a", pattern_key="same-pattern")
        post(session_key="session-a", pattern_key="same-pattern", choice="allow")
        second_pair = self.events[-2:]

        self.assertEqual(
            first_pair[0]["payload"]["request_id"],
            first_pair[1]["payload"]["request_id"],
        )
        self.assertEqual(
            second_pair[0]["payload"]["request_id"],
            second_pair[1]["payload"]["request_id"],
        )
        self.assertNotEqual(
            first_pair[0]["payload"]["request_id"],
            second_pair[0]["payload"]["request_id"],
        )

    def test_session_end_distinguishes_terminal_outcomes(self):
        handler = self.context.hooks["on_session_end"]
        handler(session_id="a", completed=True, interrupted=False)
        handler(session_id="b", completed=False, interrupted=True, reason="shutdown")
        handler(session_id="c", completed=False, interrupted=False)

        self.assertEqual(
            [event["type"] for event in self.events],
            ["turn.completed", "turn.cancelled", "turn.failed"],
        )

    def test_invalid_event_url_falls_back_to_local_http(self):
        with patch.dict(os.environ, {"HERMES_NIMBUS_EVENT_URL": "file:///tmp/out"}):
            self.assertEqual(self.plugin._endpoint(), self.plugin._DEFAULT_URL)

    def test_delivery_retries_before_succeeding(self):
        event = {"type": "turn.completed"}
        with patch.object(
            self.plugin,
            "_deliver",
            side_effect=[False, False, True],
        ) as deliver:
            with patch.object(self.plugin.time, "sleep") as sleep:
                self.assertTrue(self.plugin._deliver_with_retry(event))

        self.assertEqual(deliver.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_finalize_stops_heartbeat_and_flushes(self):
        started = self.plugin._event(
            "turn.started",
            {"session_id": "session-a", "turn_id": "turn-a"},
        )
        self.plugin._track_activity(started)
        self.assertEqual(len(self.plugin._heartbeat_events()), 1)

        with patch.object(self.plugin, "_flush", return_value=True) as flush:
            self.context.hooks["on_session_finalize"](session_id="session-a")

        flush.assert_called_once()
        self.assertEqual(self.plugin._heartbeat_events(), [])

    def test_worker_flush_waits_for_delivery(self):
        plugin = load_plugin()
        with patch.object(plugin, "_deliver_with_retry", return_value=True):
            plugin._enqueue(plugin._event("heartbeat", {"session_id": "session-a"}))
            self.assertTrue(plugin._flush(timeout=1.0))


if __name__ == "__main__":
    unittest.main()
