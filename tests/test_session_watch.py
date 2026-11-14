from __future__ import annotations

import importlib.util
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gowa_watch_test_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
assert SPEC and SPEC.loader
PLUGIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLUGIN
SPEC.loader.exec_module(PLUGIN)
WATCH = importlib.import_module("gowa_watch_test_plugin.session_watch")

SLOT = "slot-a"
CHAT = "15551234567@s.whatsapp.net"
KEY = "agent:main:discord:dm:1"
SESSION = "session-one"


class FakeContext:
    def __init__(self, directory: str, **config) -> None:
        self.state = SimpleNamespace(data_dir=Path(directory))
        self.config = config
        self.inject_ok = True
        self.injected: list[tuple[str | None, str]] = []
        self.tools: list[str] = []
        self.cli_commands: list[str] = []
        self.slash_commands: list[str] = []
        self.hooks: list[tuple[str, object]] = []
        self.unload: list[object] = []
        self.tasks: list[object] = []

    def get_config(self, key: str, default=None):
        return self.config.get(key, default)

    def inject_message(self, content: str, role: str = "user", *, session_key=None, origin=None) -> bool:
        if not self.inject_ok:
            return False
        self.injected.append((session_key, content))
        return True

    def spawn_task(self, coro, *, name=None):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task

    def on_unload(self, callback) -> None:
        self.unload.append(callback)

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs["name"])

    def register_cli_command(self, **kwargs) -> None:
        self.cli_commands.append(kwargs["name"])

    def register_command(self, name: str, handler, **kwargs) -> None:
        self.slash_commands.append(name)

    def register_hook(self, name: str, handler, **kwargs) -> None:
        self.hooks.append((name, handler))


class FakeStore:
    def __init__(self, session_id: str | None) -> None:
        self.session_id = session_id

    def lookup_by_session_key(self, key: str):
        if self.session_id is None:
            return None
        return SimpleNamespace(session_id=self.session_id)


def webhook_event(
    *,
    slot: str = SLOT,
    chat: str = CHAT,
    message_id: str = "msg-1",
    body: str = "hello from WhatsApp",
    is_from_me: bool | None = False,
    event_type: str = "message",
    route: str = "gowa-session-watch",
    platform="webhook",
) -> SimpleNamespace:
    payload = {
        "id": message_id,
        "chat_id": chat,
        "from": chat,
        "body": body,
        "is_from_me": is_from_me,
        "sender_display_name": "Private Sender",
    }
    raw = {"event": event_type, "device_id": "15550000000@s.whatsapp.net", "session_id": slot, "payload": payload}
    return SimpleNamespace(
        raw_message=raw,
        source=SimpleNamespace(platform=platform, chat_name=f"webhook/{route}"),
    )


class SessionWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        WATCH.shutdown()
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ctx = FakeContext(self.temp.name, watch_enabled=True)
        WATCH.configure(self.ctx)
        self.store = FakeStore(SESSION)
        self.assertTrue(WATCH.arm_watch(SLOT, CHAT, 60, session_key=KEY, session_id=SESSION).startswith("watch armed"))

    def tearDown(self) -> None:
        WATCH.shutdown()
        self.temp.cleanup()

    async def hook(self, event) -> object:
        return await WATCH.pre_gateway_dispatch(event=event, gateway=None, session_store=self.store)

    def watch_tool(self, args: dict) -> dict:
        # Unit tests run without Hermes; the integration class tests real binding.
        context = SimpleNamespace(
            bound_session_key=lambda: KEY,
            get_session_env=lambda name: SESSION if name == "HERMES_SESSION_ID" else None,
        )
        with mock.patch.dict(sys.modules, {"gateway.session_context": context}):
            return json.loads(WATCH.tool(args))

    async def test_matching_message_injects_once_and_dedupes(self) -> None:
        first = await self.hook(webhook_event())
        second = await self.hook(webhook_event())
        self.assertEqual(first, {"action": "skip", "reason": "gowa session watch"})
        self.assertEqual(second, {"action": "skip", "reason": "gowa session watch"})
        self.assertEqual(len(self.ctx.injected), 1)
        key, content = self.ctx.injected[0]
        self.assertEqual(key, KEY)
        self.assertIn("hello from WhatsApp", content)
        self.assertIn("untrusted", content)
        self.assertNotIn("Private Sender", content)
        status = await WATCH.command("status", session_key=KEY, session_id=SESSION)
        self.assertIn("scheduled messages 1", status)

    async def test_schedule_refusal_is_not_marked_scheduled_and_can_retry(self) -> None:
        self.ctx.inject_ok = False
        await self.hook(webhook_event())
        status = await WATCH.command("status", session_key=KEY, session_id=SESSION)
        self.assertIn("scheduled messages 0", status)
        self.ctx.inject_ok = True
        await self.hook(webhook_event())
        self.assertEqual(len(self.ctx.injected), 1)

    async def test_outbound_group_wrong_slot_wrong_chat_and_non_message_are_consumed_without_injection(self) -> None:
        cases = [
            webhook_event(is_from_me=True),
            webhook_event(is_from_me=None),
            webhook_event(chat="120363000000000000@g.us"),
            webhook_event(slot="other-slot"),
            webhook_event(chat="15559876543@s.whatsapp.net"),
            webhook_event(event_type="message.ack"),
            webhook_event(body=""),
        ]
        for event in cases:
            with self.subTest(event=event.raw_message):
                result = await self.hook(event)
                self.assertEqual(result, {"action": "skip", "reason": "gowa session watch"})
        self.assertEqual(self.ctx.injected, [])

    async def test_missing_session_id_fails_closed(self) -> None:
        event = webhook_event()
        del event.raw_message["session_id"]
        self.assertEqual(await self.hook(event), {"action": "skip", "reason": "gowa session watch"})
        self.assertEqual(self.ctx.injected, [])

    async def test_other_routes_and_platforms_are_not_consumed(self) -> None:
        self.assertIsNone(await self.hook(webhook_event(route="other-route")))
        self.assertIsNone(await self.hook(webhook_event(route="gowa-session-watch-extra")))
        self.assertIsNone(await self.hook(webhook_event(platform="discord")))
        self.assertEqual(self.ctx.injected, [])
        self.assertIn("watch active", await WATCH.command("status", session_key=KEY, session_id=SESSION))

    async def test_configured_route_is_the_only_one_consumed(self) -> None:
        self.ctx.config["watch_route"] = "custom-watch"
        self.assertIsNone(await self.hook(webhook_event(route="gowa-session-watch")))
        self.assertEqual(await self.hook(webhook_event(route="custom-watch")), {"action": "skip", "reason": "gowa session watch"})
        self.assertEqual(len(self.ctx.injected), 1)

    async def test_expired_watch_stops_delivering(self) -> None:
        armed = WATCH._now()
        with mock.patch.object(WATCH, "_now", return_value=armed + 61 * 60):
            self.assertEqual(await self.hook(webhook_event()), {"action": "skip", "reason": "gowa session watch"})
            self.assertEqual(await WATCH.command("status", session_key=KEY, session_id=SESSION), "no active watch")
        self.assertEqual(self.ctx.injected, [])

    async def test_session_change_after_new_drops_the_watch(self) -> None:
        self.store.session_id = "session-two"
        await self.hook(webhook_event())
        self.assertEqual(self.ctx.injected, [])
        self.assertEqual(await WATCH.command("status", session_key=KEY, session_id=SESSION), "no active watch")

    async def test_same_conversation_after_new_can_inspect_or_stop_its_stale_pin(self) -> None:
        self.store.session_id = "session-two"
        status = await WATCH.command("status", session_key=KEY, session_id="session-two")
        self.assertIn("pinned to the previous session", status)
        self.assertEqual(await WATCH.command("stop", session_key=KEY, session_id="session-two"), "watch stopped")

    async def test_missing_session_store_fails_closed(self) -> None:
        result = await WATCH.pre_gateway_dispatch(event=webhook_event(), gateway=None, session_store=None)
        self.assertEqual(result, {"action": "skip", "reason": "gowa session watch"})
        self.assertEqual(self.ctx.injected, [])

    async def test_cross_session_ownership_is_refused(self) -> None:
        other = {"session_key": "agent:main:telegram:dm:2", "session_id": "session-two"}
        start = await WATCH.command(f"start {SLOT} {CHAT} 60", **other)
        self.assertIn("another session owns", start)
        stop = await WATCH.command("stop", **other)
        self.assertIn("another session owns", stop)
        status = await WATCH.command("status", **other)
        self.assertNotIn(SLOT, status)
        self.assertNotIn(CHAT, status)

        self.assertEqual(await WATCH.command("stop", session_key=KEY, session_id=SESSION), "watch stopped")
        self.assertEqual(await WATCH.command("status", session_key=KEY, session_id=SESSION), "no active watch")
        self.assertTrue((await WATCH.command(f"start {SLOT} {CHAT} 1", **other)).startswith("watch armed"))

    async def test_arm_validation_and_usage(self) -> None:
        self.assertIn("usage", await WATCH.command("", session_key=KEY, session_id=SESSION))
        self.assertIn("between 1 and 1440", await WATCH.command(f"start {SLOT} {CHAT} 0", session_key=KEY, session_id=SESSION))
        self.assertIn("groups are not supported", await WATCH.command("start slot 120363000000000000@g.us", session_key=KEY, session_id=SESSION))
        self.assertIn("no trusted session_key", await WATCH.command(f"start {SLOT} {CHAT}", session_key="", session_id=SESSION))
        self.assertIn("session_id", await WATCH.command(f"start {SLOT} {CHAT}", session_key=KEY, session_id=""))
        self.assertIn("no trusted session context", await WATCH.command("status", session_key="", session_id=""))
        self.assertIn("no trusted session context", await WATCH.command("stop", session_key=KEY, session_id=""))

    async def test_disabled_watch_registers_nothing_on_the_route(self) -> None:
        self.ctx.config["watch_enabled"] = False
        self.assertIsNone(await self.hook(webhook_event()))
        self.assertIn("disabled", await WATCH.command("status", session_key=KEY, session_id=SESSION))

    async def test_body_is_bounded(self) -> None:
        event = webhook_event(body="x" * (WATCH.MAX_BODY + 500))
        await self.hook(event)
        self.assertEqual(len(self.ctx.injected), 1)
        content = self.ctx.injected[0][1]
        self.assertIn("[truncated]", content)
        encoded = content.split("Body (JSON-encoded external data):\n", 1)[1].split(
            "\n\nDecision rules:", 1
        )[0]
        self.assertLessEqual(len(json.loads(encoded)), WATCH.MAX_BODY + len("\n[truncated]"))
        self.assertLess(len(content), WATCH.MAX_BODY + 2000)

    async def test_body_control_characters_are_removed(self) -> None:
        await self.hook(webhook_event(body="hello\x00\x1bworld"))
        self.assertEqual(len(self.ctx.injected), 1)
        self.assertIn("helloworld", self.ctx.injected[0][1])
        self.assertNotIn("\x00", self.ctx.injected[0][1])
        self.assertNotIn("\x1b", self.ctx.injected[0][1])

    async def test_watch_prompt_is_static_trusted_json_with_wait_reply_report(self) -> None:
        body = 'hello "quoted"\nsecond line'
        await self.hook(webhook_event(body=body))
        content = self.ctx.injected[0][1]
        self.assertIn("[WHATSAPP WATCH MESSAGE — ", content)
        self.assertIn(f"Account slot: {SLOT}", content)
        self.assertIn(f"Chat: {CHAT}", content)
        self.assertIn("Message-ID: msg-1", content)
        self.assertIn("Received: ", content)
        self.assertIn("WAIT:", content)
        self.assertIn("REPLY:", content)
        self.assertIn("REPORT:", content)
        self.assertIn("NO_REPLY", content)
        self.assertIn("gowa_watch", content)
        self.assertIn("untrusted", content)
        self.assertIn(WATCH.INTERNAL_NOTIFICATION_FOOTER, content)
        marker = "Body (JSON-encoded external data):\n"
        encoded = content.split(marker, 1)[1].split("\n\nDecision rules:", 1)[0]
        self.assertEqual(json.loads(encoded), body)
        # Identity metadata and the body are data, never a second instruction channel.
        self.assertNotIn("Ignore previous", content)

    async def test_wait_arms_exactly_one_followup_after_the_last_inbound(self) -> None:
        self.assertEqual(WATCH.FOLLOWUP_SECONDS, 300)
        await self.hook(webhook_event())
        first = self.watch_tool({"action": "wait", "message_id": "msg-1"})
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["followup"]["message_id"], "msg-1")
        row = WATCH._active_watch()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(
            float(first["followup"]["due"]) - float(row["last_message_at"]),
            WATCH.FOLLOWUP_SECONDS,
            places=3,
        )
        self.assertIn("follow-up pending for message msg-1", await WATCH.command(
            "status", session_key=KEY, session_id=SESSION
        ))
        # A second WAIT for the same message is idempotent, not a new deadline.
        second = self.watch_tool({"action": "wait", "message_id": "msg-1"})
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["followup"]["already_armed"])
        self.assertEqual(second["followup"]["due"], first["followup"]["due"])

    async def test_gateway_task_delivers_the_followup_after_a_real_delay(self) -> None:
        with mock.patch.object(WATCH, "FOLLOWUP_SECONDS", 0.2):
            await self.hook(webhook_event())
            self.assertTrue(self.watch_tool({"action": "wait", "message_id": "msg-1"})["ok"])
            await asyncio.sleep(0.8)
        self.assertEqual(len(self.ctx.injected), 2)
        followup = self.ctx.injected[1][1]
        self.assertIn("[WHATSAPP WATCH FOLLOW-UP — ", followup)
        self.assertIn("Last message-ID: msg-1", followup)
        self.assertIn("read the exact account slot and chat", followup)
        self.assertIn('First call gowa_watch action="status"', followup)
        self.assertIn("queued follow-up is stale", followup)
        self.assertIn("If reading the chat fails", followup)
        self.assertIn("Do not repeat already reported", followup)
        self.assertIn("last message msg-1", WATCH.watch_status(session_key=KEY, session_id=SESSION))
        self.assertIn(WATCH.INTERNAL_NOTIFICATION_FOOTER, followup)
        self.assertIn("follow-up delivered for message msg-1", await WATCH.command(
            "status", session_key=KEY, session_id=SESSION
        ))
        # The same message cannot be re-armed indefinitely after its one follow-up.
        retry = self.watch_tool({"action": "wait", "message_id": "msg-1"})
        self.assertFalse(retry["ok"])
        self.assertIn("already delivered", retry["error"])

    async def test_fake_clock_advance_fires_the_pending_followup(self) -> None:
        clock = {"now": time.time()}
        with mock.patch.object(WATCH, "_now", side_effect=lambda: clock["now"]), \
                mock.patch.object(WATCH, "FOLLOWUP_SECONDS", 300):
            await self.hook(webhook_event())
            self.assertTrue(self.watch_tool({"action": "wait", "message_id": "msg-1"})["ok"])
            clock["now"] += 301
            WATCH._wake_supervisor()
            await asyncio.sleep(0.3)
        self.assertEqual(len(self.ctx.injected), 2)
        self.assertIn("[WHATSAPP WATCH FOLLOW-UP — ", self.ctx.injected[1][1])

    async def test_new_inbound_reschedules_the_pending_followup_to_the_new_message(self) -> None:
        with mock.patch.object(WATCH, "FOLLOWUP_SECONDS", 0.4):
            await self.hook(webhook_event())
            self.assertTrue(self.watch_tool({"action": "wait", "message_id": "msg-1"})["ok"])
            await asyncio.sleep(0.05)
            await self.hook(webhook_event(message_id="msg-2", body="newer"))
            await asyncio.sleep(0.8)
        self.assertEqual(len(self.ctx.injected), 3)
        followups = [text for _, text in self.ctx.injected if "WATCH FOLLOW-UP" in text]
        self.assertEqual(len(followups), 1)
        self.assertIn("Last message-ID: msg-2", followups[0])
        self.assertNotIn("Last message-ID: msg-1", followups[0])

    async def test_stale_message_and_generation_tokens_are_refused(self) -> None:
        with mock.patch.object(WATCH, "FOLLOWUP_SECONDS", 0.4):
            await self.hook(webhook_event())
            self.assertTrue(self.watch_tool({"action": "wait", "message_id": "msg-1"})["ok"])
            await self.hook(webhook_event(message_id="msg-2", body="newer"))
            stale_handled = self.watch_tool({"action": "handled", "message_id": "msg-1"})
            self.assertFalse(stale_handled["ok"])
            self.assertIn("stale", stale_handled["error"])
            stale_wait = self.watch_tool({"action": "wait", "message_id": "msg-1"})
            self.assertFalse(stale_wait["ok"])
            self.assertIn("stale", stale_wait["error"])
            self.assertIn("follow-up pending for message msg-2", await WATCH.command(
                "status", session_key=KEY, session_id=SESSION
            ))
            handled = self.watch_tool({"action": "handled", "message_id": "msg-2"})
            self.assertTrue(handled["ok"], handled)
            await asyncio.sleep(0.6)
        self.assertEqual(len(self.ctx.injected), 2)

    async def test_stop_handled_rearm_expiry_and_session_change_cancel_the_followup(self) -> None:
        with mock.patch.object(WATCH, "FOLLOWUP_SECONDS", 0.3):
            await self.hook(webhook_event())
            self.assertTrue(self.watch_tool({"action": "wait", "message_id": "msg-1"})["ok"])
            self.assertEqual(self.watch_tool({"action": "stop"})["message"], "watch stopped")
            await asyncio.sleep(0.5)
        self.assertEqual(len(self.ctx.injected), 1)

        with mock.patch.object(WATCH, "FOLLOWUP_SECONDS", 0.3):
            self.assertTrue(WATCH.arm_watch(SLOT, CHAT, 60, session_key=KEY, session_id=SESSION).startswith("watch armed"))
            await self.hook(webhook_event(message_id="msg-3"))
            self.assertTrue(self.watch_tool({"action": "wait", "message_id": "msg-3"})["ok"])
            self.assertTrue(self.watch_tool({"action": "handled", "message_id": "msg-3"})["ok"])
            await asyncio.sleep(0.5)
        self.assertEqual(len(self.ctx.injected), 2)

        with mock.patch.object(WATCH, "FOLLOWUP_SECONDS", 0.3):
            self.assertTrue(WATCH.arm_watch(SLOT, CHAT, 60, session_key=KEY, session_id=SESSION).startswith("watch armed"))
            await self.hook(webhook_event(message_id="msg-4"))
            self.assertTrue(self.watch_tool({"action": "wait", "message_id": "msg-4"})["ok"])
            self.assertTrue(WATCH.arm_watch(SLOT, CHAT, 60, session_key=KEY, session_id=SESSION).startswith("watch armed"))
            await asyncio.sleep(0.5)
        self.assertEqual(len(self.ctx.injected), 3)

        with mock.patch.object(WATCH, "FOLLOWUP_SECONDS", 0.2):
            self.assertTrue(WATCH.arm_watch(SLOT, CHAT, 60, session_key=KEY, session_id=SESSION).startswith("watch armed"))
            await self.hook(webhook_event(message_id="msg-5"))
            self.assertTrue(self.watch_tool({"action": "wait", "message_id": "msg-5"})["ok"])
            with WATCH._db() as conn:
                conn.execute("UPDATE watch SET expires_at = ? WHERE id = 1", (time.time() - 1,))
            WATCH._wake_supervisor()
            await asyncio.sleep(0.5)
        self.assertEqual(len(self.ctx.injected), 4)

        with mock.patch.object(WATCH, "FOLLOWUP_SECONDS", 0.2):
            self.assertTrue(WATCH.arm_watch(SLOT, CHAT, 60, session_key=KEY, session_id=SESSION).startswith("watch armed"))
            await self.hook(webhook_event(message_id="msg-6"))
            self.assertTrue(self.watch_tool({"action": "wait", "message_id": "msg-6"})["ok"])
            self.store.session_id = "session-two"
            await asyncio.sleep(0.5)
        self.assertEqual(len(self.ctx.injected), 5)
        self.assertEqual(await WATCH.command("status", session_key=KEY, session_id=SESSION), "no active watch")

    async def test_followup_scheduling_and_delivery_failures_are_reported(self) -> None:
        await self.hook(webhook_event())
        WATCH.shutdown()
        result = self.watch_tool({"action": "wait", "message_id": "msg-1"})
        self.assertFalse(result["ok"])
        self.assertIn("scheduler is not running", result["error"])

        with mock.patch.object(WATCH, "FOLLOWUP_SECONDS", 0.1), \
                mock.patch.object(WATCH, "FOLLOWUP_MAX_ATTEMPTS", 1), \
                mock.patch.object(WATCH, "FOLLOWUP_RETRY_SECONDS", 0.1):
            await self.hook(webhook_event(message_id="msg-7"))
            self.assertTrue(self.watch_tool({"action": "wait", "message_id": "msg-7"})["ok"])
            self.ctx.inject_ok = False
            await asyncio.sleep(0.5)
        self.assertIn("follow-up FAILED for message msg-7", await WATCH.command(
            "status", session_key=KEY, session_id=SESSION
        ))
        self.assertEqual(len(self.ctx.injected), 2)

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    async def test_watch_database_is_private(self) -> None:
        db = Path(self.temp.name) / "session-watch.sqlite3"
        self.assertEqual(db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(self.temp.name).stat().st_mode & 0o777, 0o700)

    async def test_registration_wires_real_hook_and_slash_command(self) -> None:
        context = FakeContext(self.temp.name, watch_enabled=True)
        PLUGIN.register(context)
        self.assertEqual(context.tools, ["gowa_devices", "gowa_device_login", "gowa_device_remove", "gowa_watch"])
        self.assertEqual(context.cli_commands, ["gowa"])
        self.assertEqual(context.slash_commands, ["gowa-watch"])
        self.assertEqual([(name, cb) for name, cb in context.hooks], [("pre_gateway_dispatch", WATCH.pre_gateway_dispatch)])
        self.assertEqual(len(context.unload), 1)
        self.assertIs(context.unload[0], WATCH.shutdown)


class HermesDispatchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Optional: drive the installed Hermes public hook dispatcher, not the callback directly."""

    async def asyncSetUp(self) -> None:
        try:
            from hermes_cli.lifecycle import ainvoke_hook
            from hermes_cli.plugins import get_plugin_manager, discover_plugins
            from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        except Exception as exc:  # pragma: no cover - only when Hermes is installed
            raise unittest.SkipTest(f"Hermes host not importable: {type(exc).__name__}")
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home_token = set_hermes_home_override(self.temp.name)
        self.reset_home = reset_hermes_home_override
        # Isolate discovery from the installed copy; finish lazy loading before inserting the test hook.
        discover_plugins()
        self.ainvoke_hook = ainvoke_hook
        self.manager = get_plugin_manager()
        self.original = self.manager._hooks.get("pre_gateway_dispatch")

        self.ctx = FakeContext(self.temp.name, watch_enabled=True)
        WATCH.configure(self.ctx)
        WATCH.arm_watch(SLOT, CHAT, 60, session_key=KEY, session_id=SESSION)
        self.store = FakeStore(SESSION)
        self.manager._hooks["pre_gateway_dispatch"] = [WATCH.pre_gateway_dispatch]

    async def asyncTearDown(self) -> None:
        WATCH.shutdown()
        if getattr(self, "manager", None) is not None:
            if self.original is None:
                self.manager._hooks.pop("pre_gateway_dispatch", None)
            else:
                self.manager._hooks["pre_gateway_dispatch"] = self.original
        if hasattr(self, "home_token"):
            self.reset_home(self.home_token)
        if getattr(self, "temp", None) is not None:
            self.temp.cleanup()

    async def test_public_hook_dispatch_returns_skip_and_injects(self) -> None:
        results = await self.ainvoke_hook(
            "pre_gateway_dispatch", event=webhook_event(), gateway=None, session_store=self.store
        )
        self.assertEqual(results, [{"action": "skip", "reason": "gowa session watch"}])
        self.assertEqual(len(self.ctx.injected), 1)

    async def test_tool_start_status_stop_and_hook_share_the_same_watch(self) -> None:
        import json
        from gateway.session_context import scoped_session_key, scoped_current_session_id
        with scoped_session_key(KEY), scoped_current_session_id(SESSION):
            started = json.loads(WATCH.tool({"action": "start", "device_id": SLOT, "chat_jid": CHAT, "minutes": 1}))
            self.assertTrue(started["ok"])
            await self.ainvoke_hook("pre_gateway_dispatch", event=webhook_event(), session_store=self.store)
            self.assertEqual(self.ctx.injected[0][0], KEY)
            status = json.loads(WATCH.tool({"action": "status"}))
            self.assertIn("scheduled messages 1", status["message"])
            self.assertEqual(json.loads(WATCH.tool({"action": "stop"}))["message"], "watch stopped")
            self.assertEqual(json.loads(WATCH.tool({"action": "status"}))["message"], "no active watch")

    async def test_tool_refuses_ambient_route_and_invalid_arguments(self) -> None:
        import json
        from contextvars import Context
        from gateway.session_context import scoped_session_key, scoped_current_session_id
        with mock.patch.dict(os.environ, {"HERMES_SESSION_KEY": KEY, "HERMES_SESSION_ID": SESSION}):
            result = Context().run(WATCH.tool, {"action": "status"})
            self.assertFalse(json.loads(result)["ok"])
        with scoped_session_key(KEY), scoped_current_session_id(SESSION):
            for args in (
                {"action": "start", "device_id": SLOT, "chat_jid": CHAT, "session_key": "other"},
                {"action": "start", "device_id": SLOT, "chat_jid": CHAT, "minutes": True},
                {"action": "start", "device_id": SLOT},
                {"action": "start", "device_id": SLOT, "chat_jid": CHAT, "message_id": "msg-1"},
                {"action": "stop", "session_id": "other"},
                {"action": "wait"},
                {"action": "wait", "message_id": "msg-1", "device_id": SLOT},
                {"action": "handled", "message_id": ""},
                {"action": "unknown"},
            ):
                with self.subTest(args=args):
                    self.assertFalse(json.loads(WATCH.tool(args))["ok"])
        with scoped_session_key(KEY), scoped_current_session_id(""):
            self.assertFalse(json.loads(WATCH.tool({"action": "status"}))["ok"])

    async def test_tool_cross_session_does_not_leak_or_stop(self) -> None:
        import json
        from gateway.session_context import scoped_session_key, scoped_current_session_id
        with scoped_session_key("another-route"), scoped_current_session_id("another-session"):
            status = json.loads(WATCH.tool({"action": "status"}))["message"]
            self.assertNotIn(CHAT, status)
            self.assertNotIn(SLOT, status)
            self.assertFalse(json.loads(WATCH.tool({"action": "stop"}))["ok"])
            self.assertFalse(json.loads(WATCH.tool({"action": "start", "device_id": SLOT, "chat_jid": CHAT}))["ok"])

    async def test_public_slash_dispatcher_supplies_the_trusted_session(self) -> None:
        from hermes_cli.plugin_command_dispatch import invoke_plugin_command

        result = invoke_plugin_command(
            WATCH.command,
            "status",
            source=SimpleNamespace(platform="discord"),
            session_id=SESSION,
            session_key=KEY,
            platform="discord",
        )
        self.assertIn("watch active", await result)


if __name__ == "__main__":
    unittest.main()
