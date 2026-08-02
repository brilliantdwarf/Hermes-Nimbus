import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import aiohttp  # noqa: F401
except ImportError:  # The core detector tests do not require the web extra.
    aiohttp = None

if aiohttp is not None:
    from halo_server import HaloServer  # noqa: E402
else:
    HaloServer = None


class FakeRequest:
    def __init__(self, body, *, headers=None, remote="127.0.0.1"):
        self._body = body
        self.headers = headers or {}
        self.remote = remote

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)


def response_json(response):
    return json.loads(response.text)


@unittest.skipIf(aiohttp is None, "aiohttp is not installed in this interpreter")
class HaloServerEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.profile_home = Path(self.temp_dir.name) / "profile"
        log_path = self.profile_home / "logs" / "agent.log"
        log_path.parent.mkdir(parents=True)
        log_path.write_text("", encoding="utf-8")
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.config_path.write_text(
            json.dumps({
                "instances": [{
                    "id": "profile-a",
                    "name": "Profile A",
                    "log_path": str(log_path),
                    "db_path": "",
                }]
            }),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"HERMES_NIMBUS_EVENT_TOKEN": ""}):
            with patch("hermes_state.real_user_home", return_value=Path(self.temp_dir.name)):
                self.halo = HaloServer(host="127.0.0.1", port=0, config_path=self.config_path)
    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def event(kind="turn.started", **values):
        payload = {
            "schema_version": 1,
            "profile_id": "profile-a",
            "type": kind,
            "occurred_at": time.time(),
            "session_id": "session-a",
            "turn_id": "turn-a",
            "event_id": f"event-{time.time_ns()}",
        }
        payload.update(values)
        return payload

    async def test_valid_event_updates_existing_state_shape(self):
        response = await self.halo.events_handler(
            FakeRequest(self.event(source="manual"))
        )
        body = response_json(response)

        self.assertEqual(response.status, 200)
        self.assertTrue(body["accepted"])
        self.assertEqual(body["state"]["state"], "thinking")
        self.assertEqual(body["state"]["source"], "hermes_hook")
        self.assertEqual(body["state"]["state_config"]["color"], "#ff8830")

    async def test_duplicate_event_is_idempotent(self):
        event = self.event(event_id="stable-id")
        first = await self.halo.events_handler(FakeRequest(event))
        second = await self.halo.events_handler(FakeRequest(event))

        self.assertTrue(response_json(first)["accepted"])
        self.assertFalse(response_json(second)["accepted"])

    async def test_invalid_schema_event_and_profile_are_rejected(self):
        invalid_schema = await self.halo.events_handler(
            FakeRequest(self.event(schema_version=2))
        )
        invalid_event = await self.halo.events_handler(
            FakeRequest(self.event(kind="not.a.real.event"))
        )
        missing_event_id = await self.halo.events_handler(
            FakeRequest(self.event(event_id=""))
        )
        boolean_schema = await self.halo.events_handler(
            FakeRequest(self.event(schema_version=True))
        )
        unknown_profile = await self.halo.events_handler(
            FakeRequest(self.event(profile_id="missing"))
        )

        self.assertEqual(invalid_schema.status, 400)
        self.assertEqual(invalid_event.status, 400)
        self.assertEqual(missing_event_id.status, 400)
        self.assertEqual(boolean_schema.status, 400)
        self.assertEqual(unknown_profile.status, 404)

    async def test_snapshot_signature_ignores_poll_timestamp_only(self):
        first = {"id": "a", "state": "idle", "timestamp": "one"}
        second = {"id": "a", "state": "idle", "timestamp": "two"}
        changed = {"id": "a", "state": "thinking", "timestamp": "two"}

        self.assertEqual(
            self.halo._snapshot_signature(first),
            self.halo._snapshot_signature(second),
        )
        self.assertNotEqual(
            self.halo._snapshot_signature(first),
            self.halo._snapshot_signature(changed),
        )

    async def test_websocket_protocol_is_read_only(self):
        ws = FakeWebSocket()

        await self.halo.handle_message(ws, {
            "type": "set_state",
            "instance_id": "profile-a",
            "state": "executing",
        })

        self.assertEqual(ws.messages[-1]["type"], "error")
        self.assertIsNone(self.halo.detector.instances["profile-a"].machine._manual_state)

    async def test_websocket_rejects_non_object_messages(self):
        ws = FakeWebSocket()

        await self.halo.handle_message(ws, ["get_states"])

        self.assertEqual(ws.messages[-1]["type"], "error")

    async def test_client_and_origin_allowlists(self):
        with patch("hermes_state.real_user_home", return_value=Path(self.temp_dir.name)):
            halo = HaloServer(
                host="127.0.0.1",
                lan_host="192.0.2.10",
                port=8765,
                config_path=self.config_path,
                allowed_clients=["192.0.2.20"],
            )

        self.assertTrue(halo._client_request_allowed(FakeRequest({}, remote="127.0.0.1")))
        self.assertTrue(halo._client_request_allowed(FakeRequest({}, remote="192.0.2.20")))
        self.assertFalse(halo._client_request_allowed(FakeRequest({}, remote="192.0.2.21")))
        self.assertTrue(halo._websocket_origin_allowed(FakeRequest(
            {},
            remote="192.0.2.20",
            headers={"Origin": "http://192.0.2.10:8765"},
        )))
        self.assertFalse(halo._websocket_origin_allowed(FakeRequest(
            {},
            remote="192.0.2.20",
            headers={"Origin": "https://evil.example"},
        )))
        self.assertFalse(halo._websocket_origin_allowed(FakeRequest(
            {},
            remote="192.0.2.20",
        )))

    async def test_non_loopback_listener_requires_allowlist(self):
        with self.assertRaises(ValueError):
            HaloServer(
                host="127.0.0.1",
                lan_host="192.0.2.10",
                port=8765,
                config_path=self.config_path,
            )


@unittest.skipIf(aiohttp is None, "aiohttp is not installed in this interpreter")
class HaloServerTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_token_is_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "logs" / "agent.log"
            log_path.parent.mkdir(parents=True)
            log_path.write_text("", encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({
                    "instances": [{
                        "id": "profile-a",
                        "name": "Profile A",
                        "log_path": str(log_path),
                        "db_path": "",
                    }]
                }),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HERMES_NIMBUS_EVENT_TOKEN": "secret"}):
                with patch("hermes_state.real_user_home", return_value=root):
                    halo = HaloServer(host="127.0.0.1", port=0, config_path=config_path)
            event = HaloServerEventTests.event()

            missing = await halo.events_handler(FakeRequest(event))
            wrong = await halo.events_handler(
                FakeRequest(event, headers={"Authorization": "Bearer wrong"})
            )
            accepted = await halo.events_handler(
                FakeRequest(event, headers={"Authorization": "Bearer secret"})
            )

            self.assertEqual(missing.status, 401)
            self.assertEqual(wrong.status, 401)
            self.assertEqual(accepted.status, 200)


if __name__ == "__main__":
    unittest.main()
