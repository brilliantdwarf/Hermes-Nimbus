import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from state_model import ProfileStateMachine, StateEvent  # noqa: E402


def event(kind, at, *, session="s1", source="hermes_hook", event_id="", **payload):
    return StateEvent(
        type=kind,
        occurred_at=float(at),
        source=source,
        session_id=session,
        event_id=event_id,
        payload=payload,
    )


class ProfileStateMachineTests(unittest.TestCase):
    def test_turn_lifecycle_preserves_existing_halo_states(self):
        machine = ProfileStateMachine(completed_hold=30)
        machine.ingest(event("turn.started", 100))
        self.assertEqual(machine.current_state(now=100), "thinking")

        machine.ingest(event("output.delta", 101))
        self.assertEqual(machine.current_state(now=101), "streaming")

        machine.ingest(event("turn.completed", 102))
        self.assertEqual(machine.current_state(now=110), "completed")
        self.assertEqual(machine.current_state(now=133), "idle")

    def test_parallel_tools_are_counted_independently(self):
        machine = ProfileStateMachine()
        machine.ingest(event("turn.started", 100))
        machine.ingest(event("tool.started", 101, tool_call_id="a"))
        machine.ingest(event("tool.started", 102, tool_call_id="b"))
        machine.ingest(event("tool.finished", 103, tool_call_id="a"))

        self.assertEqual(machine.current_state(now=103), "executing")
        self.assertEqual(machine.snapshot(now=103)["detail"]["running_tools"], 1)

        machine.ingest(event("tool.finished", 104, tool_call_id="b"))
        self.assertEqual(machine.current_state(now=104), "thinking")

    def test_parallel_tool_closures_can_arrive_out_of_session_order(self):
        machine = ProfileStateMachine()
        machine.ingest(event("turn.started", 100))
        machine.ingest(event("tool.started", 101, tool_call_id="a"))
        machine.ingest(event("tool.started", 102, tool_call_id="b"))
        machine.ingest(event("tool.finished", 104, tool_call_id="a"))

        accepted = machine.ingest(event("tool.finished", 103, tool_call_id="b"))

        self.assertTrue(accepted)
        self.assertEqual(machine.snapshot(now=104)["detail"]["running_tools"], 0)

    def test_late_start_for_same_tool_cannot_reopen_finished_tool(self):
        machine = ProfileStateMachine()
        machine.ingest(event("turn.started", 100))
        machine.ingest(event("tool.finished", 103, tool_call_id="a"))

        accepted = machine.ingest(event("tool.started", 102, tool_call_id="a"))

        self.assertFalse(accepted)
        self.assertEqual(machine.current_state(now=103), "thinking")

    def test_waiting_input_outranks_running_tool(self):
        machine = ProfileStateMachine()
        machine.ingest(event("turn.started", 100))
        machine.ingest(event("tool.started", 101, tool_call_id="a"))
        machine.ingest(event("input.requested", 102, request_id="approval-a"))
        self.assertEqual(machine.current_state(now=102), "input_needed")

        machine.ingest(event("input.resolved", 103, request_id="approval-a"))
        self.assertEqual(machine.current_state(now=103), "executing")

    def test_standalone_approval_scope_releases_synthetic_turn(self):
        machine = ProfileStateMachine()
        machine.ingest(
            event(
                "input.requested",
                100,
                session="gateway-session-key",
                request_id="approval-a",
            )
        )
        self.assertEqual(machine.current_state(now=100), "input_needed")

        machine.ingest(
            event(
                "input.resolved",
                101,
                session="gateway-session-key",
                request_id="approval-a",
            )
        )
        self.assertEqual(machine.current_state(now=101), "idle")

    def test_standalone_compression_returns_to_idle(self):
        machine = ProfileStateMachine()
        machine.ingest(event("compression.started", 100))
        self.assertEqual(machine.current_state(now=100), "compacting")

        machine.ingest(event("compression.finished", 101))
        self.assertEqual(machine.current_state(now=101), "idle")

    def test_turn_compression_returns_to_thinking(self):
        machine = ProfileStateMachine()
        machine.ingest(event("turn.started", 100))
        machine.ingest(event("compression.started", 101))
        machine.ingest(event("compression.finished", 102))

        self.assertEqual(machine.current_state(now=102), "thinking")

    def test_new_turn_immediately_overrides_completed_hold(self):
        machine = ProfileStateMachine(completed_hold=30)
        machine.ingest(event("turn.completed", 100))
        self.assertEqual(machine.current_state(now=105), "completed")

        machine.ingest(event("turn.started", 106))
        self.assertEqual(machine.current_state(now=106), "thinking")

    def test_interrupted_turn_is_not_reported_completed(self):
        machine = ProfileStateMachine()
        machine.ingest(event("turn.started", 100))
        machine.ingest(event("turn.cancelled", 101, reason="interrupted_by_user"))
        self.assertEqual(machine.current_state(now=101), "idle")

    def test_failed_turn_reports_error_for_hold_window(self):
        machine = ProfileStateMachine(completed_hold=30)
        machine.ingest(event("turn.failed", 100, reason="max_iterations_reached"))
        self.assertEqual(machine.current_state(now=120), "error")
        self.assertEqual(machine.current_state(now=131), "idle")

    def test_duplicate_event_id_is_ignored(self):
        machine = ProfileStateMachine()
        first = event("tool.started", 100, event_id="same", tool_call_id="a")
        self.assertTrue(machine.ingest(first))
        self.assertFalse(machine.ingest(first))
        self.assertEqual(machine.snapshot(now=100)["detail"]["running_tools"], 1)

    def test_out_of_order_event_does_not_reopen_completed_turn(self):
        machine = ProfileStateMachine(completed_hold=30)
        machine.ingest(event("turn.started", 100, event_id="start"))
        machine.ingest(event("turn.completed", 102, event_id="done"))

        accepted = machine.ingest(event("turn.started", 101, event_id="late-start"))

        self.assertFalse(accepted)
        self.assertEqual(machine.current_state(now=103), "completed")

    def test_session_cache_prunes_inactive_sessions_not_active_work(self):
        machine = ProfileStateMachine(session_cache_size=8)
        machine.ingest(event("turn.started", 1, session="active"))
        for index in range(12):
            machine.ingest(
                event("turn.completed", index + 2, session=f"done-{index}")
            )

        self.assertIn("active", machine.sessions)
        self.assertLessEqual(len(machine.sessions), 8)

    def test_session_cache_remains_bounded_if_terminal_events_are_lost(self):
        machine = ProfileStateMachine(session_cache_size=8)
        for index in range(12):
            machine.ingest(event("turn.started", index + 1, session=f"active-{index}"))

        self.assertEqual(len(machine.sessions), 8)
        self.assertNotIn("active-0", machine.sessions)
        self.assertIn("active-11", machine.sessions)

    def test_legacy_watchdog_does_not_clear_authoritative_events(self):
        machine = ProfileStateMachine()
        machine.ingest(event("turn.started", 100, session="legacy", source="log"))
        machine.ingest(event("turn.started", 100, session="official", source="hermes_hook"))
        machine.clear_stale_legacy(timeout=60, now=200)

        self.assertFalse(machine.sessions["legacy"].turn_active)
        self.assertTrue(machine.sessions["official"].turn_active)
        self.assertEqual(machine.current_state(now=200), "thinking")

    def test_authoritative_lease_expires_without_heartbeat(self):
        machine = ProfileStateMachine()
        machine.ingest(event("turn.started", 100, session="official"))

        expired = machine.clear_expired_authoritative(timeout=90, now=191)

        self.assertTrue(expired)
        self.assertEqual(machine.current_state(now=191), "idle")
        self.assertEqual(machine.last_reason, "authoritative_lease_timeout")

    def test_heartbeat_renews_authoritative_lease(self):
        machine = ProfileStateMachine()
        machine.ingest(event("turn.started", 100, session="official"))
        machine.ingest(event("heartbeat", 170, session="official"))

        expired = machine.clear_expired_authoritative(timeout=90, now=220)

        self.assertFalse(expired)
        self.assertEqual(machine.current_state(now=220), "thinking")

    def test_mapping_validation_rejects_unknown_event(self):
        with self.assertRaises(ValueError):
            StateEvent.from_mapping({"type": "made.up"}, now=100)


if __name__ == "__main__":
    unittest.main()
