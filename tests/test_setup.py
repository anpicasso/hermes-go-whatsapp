from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import plistlib
import socket
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gowa_test_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
assert SPEC and SPEC.loader
PLUGIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLUGIN
SPEC.loader.exec_module(PLUGIN)
INSTALLER_SPEC = importlib.util.spec_from_file_location(
    "gowa_test_plugin.installer", ROOT / "installer.py"
)
assert INSTALLER_SPEC and INSTALLER_SPEC.loader
INSTALLER = importlib.util.module_from_spec(INSTALLER_SPEC)
sys.modules[INSTALLER_SPEC.name] = INSTALLER
INSTALLER_SPEC.loader.exec_module(INSTALLER)


def archive(payload: bytes = b"fake-gowa-binary", member: str = INSTALLER.ARCHIVE_MEMBER) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(member, payload)
    return buffer.getvalue()


@contextmanager
def existing_gowa(
    required_auth: str | None = None,
    *,
    info_results=None,
    server_name: str = "WhatsApp Web Multidevice MCP Server",
):
    info_results = {"version": "v9.5.0"} if info_results is None else info_results

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return None

        def _authorized(self) -> bool:
            if required_auth is None or self.headers.get("Authorization") == required_auth:
                return True
            self.send_response(401)
            self.end_headers()
            return False

        def _json(self, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if not self._authorized():
                return
            if self.path == "/app/info":
                self._json({"results": info_results})
                return
            if self.path == "/devices":
                self._json({"results": None})
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            if not self._authorized():
                return
            if self.path != "/mcp":
                self.send_response(404)
                self.end_headers()
                return
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            assert payload["method"] == "initialize"
            self._json(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {
                            "name": server_name,
                            "version": "v9.5.0",
                        },
                    },
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class FakeContext:
    def __init__(self) -> None:
        self.tools: list[str] = []
        self.commands: list[str] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs["name"])

    def register_cli_command(self, **kwargs) -> None:
        self.commands.append(kwargs["name"])


class PluginTests(unittest.TestCase):
    def test_registration_includes_three_tools_and_setup_cli(self) -> None:
        context = FakeContext()
        PLUGIN.register(context)
        self.assertEqual(
            context.tools,
            ["gowa_devices", "gowa_device_login", "gowa_device_remove"],
        )
        self.assertEqual(context.commands, ["gowa"])

    def test_code_login_validates_before_creating_and_refuses_duplicate_number(self) -> None:
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, body=None, *, login: bool = False):
            calls.append((method, path, body))
            if method == "GET" and path == "/devices/new-slot":
                raise PLUGIN.GowaError("not found", 404)
            if method == "GET" and path == "/devices":
                return {
                    "results": [
                        {
                            "id": "personal",
                            "phone_number": "",
                            "jid": "15551234567@s.whatsapp.net",
                            "state": "logged_in",
                        }
                    ]
                }
            if method == "POST" and path == "/devices":
                self.fail("duplicate-number preflight must run before slot creation")
            self.fail(f"unexpected request: {method} {path}")

        with mock.patch.object(PLUGIN, "_request", side_effect=request):
            with self.assertRaisesRegex(PLUGIN.GowaError, "already linked to device personal"):
                PLUGIN._device_login(
                    {
                        "action": "code",
                        "device_id": "new-slot",
                        "phone": "+15551234567",
                        "create_if_missing": True,
                    }
                )
        self.assertNotIn(("POST", "/devices", {"device_id": "new-slot"}), calls)

        calls.clear()
        with mock.patch.object(PLUGIN, "_request", side_effect=request):
            with self.assertRaisesRegex(PLUGIN.GowaError, "phone must be"):
                PLUGIN._device_login(
                    {
                        "action": "code",
                        "device_id": "new-slot",
                        "phone": "not-a-number",
                        "create_if_missing": True,
                    }
                )
        self.assertEqual(calls, [])

        calls.clear()

        def unique_request(method: str, path: str, body=None, *, login: bool = False):
            calls.append((method, path, body))
            if method == "GET" and path == "/devices/work":
                raise PLUGIN.GowaError("GOWA returned HTTP 500: device work not found", 500)
            if method == "GET" and path == "/devices":
                return {"results": [{"id": "personal", "phone_number": "+15550000000"}]}
            if method == "POST" and path == "/devices":
                return {"results": {"id": "work"}}
            if method == "POST" and path.startswith("/devices/work/login/code?"):
                return {"results": {"device_id": "work", "pair_code": "ABCD-EFGH"}}
            self.fail(f"unexpected request: {method} {path}")

        with mock.patch.object(PLUGIN, "_request", side_effect=unique_request):
            result = PLUGIN._device_login(
                {
                    "action": "code",
                    "device_id": "work",
                    "phone": "+15551234567",
                    "create_if_missing": True,
                }
            )
        self.assertEqual(result["results"]["pair_code"], "ABCD-EFGH")
        self.assertIn(("POST", "/devices", {"device_id": "work"}), calls)
        self.assertTrue(
            any(
                method == "POST"
                and path.startswith("/devices/work/login/code?phone=")
                and "%2B" not in path
                for method, path, _ in calls
            )
        )

        with mock.patch.object(
            PLUGIN,
            "_request",
            side_effect=PLUGIN.GowaError("GOWA returned HTTP 500: database unavailable", 500),
        ):
            with self.assertRaisesRegex(PLUGIN.GowaError, "database unavailable"):
                PLUGIN._device_login(
                    {
                        "action": "qr",
                        "device_id": "work",
                        "create_if_missing": True,
                    }
                )

    def test_code_login_checks_duplicates_for_existing_empty_slot(self) -> None:
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, body=None, *, login: bool = False):
            calls.append((method, path, body))
            if method == "GET" and path == "/devices/work":
                return {"results": {"id": "work", "state": "disconnected"}}
            if method == "GET" and path == "/devices":
                return {"results": [{"id": "personal", "jid": "15551234567@s.whatsapp.net"}]}
            self.fail(f"pairing should not start: {method} {path}")

        with mock.patch.object(PLUGIN, "_request", side_effect=request):
            with self.assertRaisesRegex(PLUGIN.GowaError, "already linked to device personal"):
                PLUGIN._device_login(
                    {"action": "code", "device_id": "work", "phone": "+15551234567"}
                )
        self.assertFalse(any(method == "POST" for method, _, _ in calls))

    def test_code_login_rejects_jid_and_malformed_success(self) -> None:
        with mock.patch.object(PLUGIN, "_request") as request:
            with self.assertRaisesRegex(PLUGIN.GowaError, "phone must be"):
                PLUGIN._device_login(
                    {"action": "code", "device_id": "work", "phone": "15551234567@s.whatsapp.net"}
                )
            request.assert_not_called()

        def malformed(method: str, path: str, body=None, *, login: bool = False):
            if method == "GET" and path == "/devices/work":
                return {"results": {"id": "work", "state": "disconnected"}}
            if method == "GET" and path == "/devices":
                return {"results": []}
            if method == "POST" and path.startswith("/devices/work/login/code?"):
                return {"results": None}
            self.fail(f"unexpected request: {method} {path}")

        with mock.patch.object(PLUGIN, "_request", side_effect=malformed):
            with self.assertRaisesRegex(PLUGIN.GowaError, "did not include pair_code"):
                PLUGIN._device_login(
                    {"action": "code", "device_id": "work", "phone": "+15551234567"}
                )

    def test_login_refuses_to_pair_an_already_linked_slot(self) -> None:
        def request(method: str, path: str, body=None, *, login: bool = False):
            if method == "GET" and path == "/devices/personal":
                return {
                    "results": {
                        "id": "personal",
                        "jid": "15551234567@s.whatsapp.net",
                        "state": "disconnected",
                    }
                }
            self.fail(f"pairing should not start: {method} {path}")

        with mock.patch.object(PLUGIN, "_request", side_effect=request):
            with self.assertRaisesRegex(PLUGIN.GowaError, "already linked"):
                PLUGIN._device_login({"action": "qr", "device_id": "personal"})


class InstallerTests(unittest.TestCase):
    def test_verified_binary_install_is_atomic_and_versioned(self) -> None:
        payload = archive()
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            binary = INSTALLER._install_binary(home, payload, digest)
            self.assertEqual(binary.read_bytes(), b"fake-gowa-binary")
            self.assertTrue(os.access(binary, os.X_OK))
            self.assertEqual((home / ".local/lib/gowa/current").resolve(), binary.parent)

    def test_checksum_mismatch_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(INSTALLER.SetupError, "checksum mismatch"):
                INSTALLER._install_binary(Path(temp), archive(), "0" * 64)

    def test_file_configuration_is_private_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            first_credential, first_header = INSTALLER._configure_files(home, 3456)
            second_credential, second_header = INSTALLER._configure_files(home, 3456)
            self.assertEqual(first_credential, second_credential)
            self.assertEqual(first_header, second_header)
            env_path = home / ".config/gowa/gowa.env"
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            values = INSTALLER._env_values(env_path)
            self.assertEqual(values["APP_HOST"], "127.0.0.1")
            self.assertEqual(values["APP_PORT"], "3456")
            self.assertEqual(values["APP_UI_ENABLED"], "false")
            self.assertNotEqual(values["WHATSAPP_WEBHOOK_SECRET"], "secret")
            self.assertEqual((home / ".local/share/gowa/runtime").stat().st_mode & 0o777, 0o700)

    def test_hermes_configuration_uses_secret_placeholder(self) -> None:
        writes: list[tuple[str, str, bool]] = []

        def setter(key: str, value: str, *, force: bool = False) -> None:
            writes.append((key, value, force))

        INSTALLER._configure_hermes("http://127.0.0.1:3456", "Basic obviously-fake", setter)
        values = {key: value for key, value, _ in writes}
        self.assertEqual(values["mcp_servers.gowa.url"], "http://127.0.0.1:3456/mcp")
        self.assertEqual(values["plugins.entries.go-whatsapp.settings.base_url"], "http://127.0.0.1:3456")
        self.assertEqual(
            values["mcp_servers.gowa.headers.Authorization"], "${GOWA_AUTH_HEADER}"
        )
        self.assertTrue(all(force for _, _, force in writes))

    def test_existing_gowa_with_bearer_skips_download_and_service(self) -> None:
        writes: list[tuple[str, str]] = []

        def setter(key: str, value: str, *, force: bool = False) -> None:
            self.assertTrue(force)
            writes.append((key, value))

        with tempfile.TemporaryDirectory() as temp, existing_gowa("Bearer valid-token") as base_url:
            with mock.patch.object(INSTALLER, "_download_archive", side_effect=AssertionError("downloaded")):
                result = INSTALLER.install(
                    base_url=base_url,
                    auth_bearer="valid-token",
                    _home=Path(temp),
                    _hermes_home=Path(temp) / "hermes",
                    _config_setter=setter,
                    _config_unsetter=lambda key: self.fail(f"unexpected unset: {key}"),
                )
            self.assertFalse((Path(temp) / ".local/lib/gowa").exists())

        values = dict(writes)
        self.assertEqual(result["mode"], "existing server (not managed)")
        self.assertEqual(values["GOWA_AUTH_HEADER"], "Bearer valid-token")
        self.assertEqual(values["mcp_servers.gowa.url"], f"{base_url}/mcp")

    def test_existing_gowa_missing_or_bad_bearer_fails_before_config(self) -> None:
        writes: list[tuple[str, str]] = []

        def setter(key: str, value: str, *, force: bool = False) -> None:
            writes.append((key, value))

        with tempfile.TemporaryDirectory() as temp, existing_gowa("Bearer valid-token") as base_url:
            kwargs = {"base_url": base_url, "_hermes_home": Path(temp), "_config_setter": setter}
            with self.assertRaisesRegex(INSTALLER.SetupError, "requires authentication"):
                INSTALLER.install(**kwargs)
            with self.assertRaisesRegex(INSTALLER.SetupError, "rejected --auth-bearer"):
                INSTALLER.install(auth_bearer="wrong-token", **kwargs)
        self.assertEqual(writes, [])

    def test_existing_gowa_without_auth_clears_stale_auth_config(self) -> None:
        writes: list[tuple[str, str]] = []
        removals: list[str] = []

        with tempfile.TemporaryDirectory() as temp, existing_gowa() as base_url:
            result = INSTALLER.install(
                base_url=base_url,
                _hermes_home=Path(temp),
                _config_setter=lambda key, value, force=False: writes.append((key, value)),
                _config_unsetter=removals.append,
            )
        self.assertEqual(result["version"], "v9.5.0")
        self.assertEqual(
            removals,
            ["GOWA_AUTH_HEADER", "mcp_servers.gowa.headers.Authorization"],
        )
        self.assertNotIn("GOWA_AUTH_HEADER", dict(writes))

    def test_existing_probe_rejects_wrong_json_shapes_and_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with existing_gowa(info_results=["not", "gowa"]) as base_url:
                with self.assertRaisesRegex(INSTALLER.SetupError, "did not identify"):
                    INSTALLER.install(base_url=base_url, _hermes_home=Path(temp))
            with existing_gowa(server_name="another MCP server") as base_url:
                with self.assertRaisesRegex(INSTALLER.SetupError, "did not identify"):
                    INSTALLER.install(base_url=base_url, _hermes_home=Path(temp))

    def test_external_option_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            hermes_home = Path(temp)
            with self.assertRaisesRegex(INSTALLER.SetupError, "requires --base-url"):
                INSTALLER.install(auth_bearer="token", _hermes_home=hermes_home)
            with self.assertRaisesRegex(INSTALLER.SetupError, "cannot be combined"):
                INSTALLER.install(
                    base_url="http://127.0.0.1:3000",
                    port=3456,
                    _hermes_home=hermes_home,
                )
            for url, message in (
                ("http://192.0.2.1:3000", "plain HTTP"),
                ("https://example.com:bad", "invalid port"),
                ("https://user:pass@example.com", "cannot contain credentials"),
                ("https://example.com", "requires --auth-bearer"),
            ):
                with self.subTest(url=url), self.assertRaisesRegex(INSTALLER.SetupError, message):
                    INSTALLER.install(base_url=url, _hermes_home=hermes_home)

    def test_managed_port_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            INSTALLER._install_unit(home)
            env_path = home / ".config/gowa/gowa.env"
            INSTALLER._update_env(env_path, {"APP_HOST": "127.0.0.1", "APP_PORT": "45678"})
            self.assertEqual(INSTALLER._managed_port(home), 45678)

    def test_kernel_selected_port_is_bindable_and_explicit_collision_fails(self) -> None:
        port = INSTALLER._pick_free_loopback_port()
        with tempfile.TemporaryDirectory() as temp, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as claimed:
            claimed.bind(("127.0.0.1", port))
            with self.assertRaisesRegex(INSTALLER.SetupError, "already in use"):
                INSTALLER.install(
                    port=port,
                    _home=Path(temp),
                    _hermes_home=Path(temp) / "hermes",
                    _archive=b"unused",
                )

    def test_unit_suppresses_upstream_settings_dump(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            unit = INSTALLER._install_unit(Path(temp)).read_text()
            self.assertIn("StandardOutput=null", unit)
            self.assertIn("ProtectSystem=strict", unit)
            self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", unit)
            self.assertIn("WorkingDirectory=%h/.local/share/gowa/runtime", unit)
            self.assertNotIn('WorkingDirectory="', unit)

    def test_release_assets_cover_supported_native_hosts(self) -> None:
        expected = {
            ("Linux", "amd64"): ("whatsapp_9.5.0_linux_amd64.zip", "linux-amd64"),
            ("Darwin", "x86_64"): ("whatsapp_9.5.0_darwin_amd64.zip", "darwin-amd64"),
            ("Darwin", "aarch64"): ("whatsapp_9.5.0_darwin_arm64.zip", "darwin-arm64"),
            ("Windows", "AMD64"): ("whatsapp_9.5.0_windows_amd64.zip", "windows-amd64.exe"),
        }
        for (system, machine), (asset, member) in expected.items():
            with self.subTest(system=system, machine=machine):
                name, digest, actual_member = INSTALLER._release_asset(
                    system, INSTALLER._normalized_machine(machine)
                )
                self.assertEqual((name, actual_member), (asset, member))
                self.assertEqual(len(digest), 64)
        with self.assertRaisesRegex(INSTALLER.SetupError, "supports Linux"):
            INSTALLER._release_asset("Windows", "arm64")

    def test_launch_agent_is_private_restart_on_failure_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            credential, _ = INSTALLER._configure_files(home, 3456, "Darwin")
            plist_path = INSTALLER._install_launch_agent(home)
            values = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(values["Label"], "com.hermes.gowa")
            self.assertEqual(values["ProgramArguments"][-1], "rest")
            self.assertEqual(values["WorkingDirectory"], str(home / ".local/share/gowa/runtime"))
            self.assertEqual(values["KeepAlive"], {"SuccessfulExit": False})
            self.assertEqual(values["ExitTimeOut"], 20)
            self.assertEqual(values["Umask"], 0o077)
            self.assertEqual(values["StandardOutPath"], "/dev/null")
            self.assertNotIn(credential, plist_path.read_text())
            self.assertEqual(plist_path.stat().st_mode & 0o777, 0o600)
            self.assertTrue((home / ".local/share/gowa/runtime/.env").exists())
            self.assertEqual(INSTALLER._managed_port(home, "Darwin"), 3456)

    def test_windows_task_uses_interactive_token_and_secret_free_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            local = home / "LocalAppData"
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False):
                credential, _ = INSTALLER._configure_files(home, 3456, "Windows")
                payload = archive(member="windows-amd64.exe")
                binary = INSTALLER._install_binary(
                    home,
                    payload,
                    hashlib.sha256(payload).hexdigest(),
                    "windows-amd64.exe",
                    "Windows",
                )
                task_path = INSTALLER._install_windows_task(home, binary, r"DOMAIN\User")
                raw = task_path.read_bytes()
                self.assertTrue(raw.startswith((b"\xff\xfe", b"\xfe\xff")))
                root = ET.fromstring(raw)
                ns = {"t": INSTALLER._TASK_NS}
                self.assertEqual(root.findtext(".//t:LogonType", namespaces=ns), "InteractiveToken")
                self.assertEqual(root.findtext(".//t:RunLevel", namespaces=ns), "LeastPrivilege")
                self.assertEqual(root.findtext(".//t:ExecutionTimeLimit", namespaces=ns), "PT0S")
                self.assertEqual(root.findtext(".//t:TimeTrigger/t:Repetition/t:Interval", namespaces=ns), "PT1M")
                self.assertIsNone(root.find(".//t:TimeTrigger/t:Repetition/t:Duration", ns))
                arguments = root.findtext(".//t:Arguments", namespaces=ns) or ""
                self.assertIn("-File", arguments)
                self.assertNotIn(credential, arguments)
                script = (local / "gowa/run.ps1").read_text(encoding="utf-8-sig")
                self.assertIn("1>$null", script)
                self.assertIn("2>", script)
                self.assertNotIn(credential, script)
                env_path = local / "gowa/runtime/.env"
                self.assertEqual(INSTALLER._env_values(env_path)["APP_PORT"], "3456")
                self.assertEqual(INSTALLER._managed_port(home, "Windows"), 3456)
                self.assertFalse((local / "gowa/lib/current").exists())

    def test_windows_identity_and_acl_use_sid_without_secrets(self) -> None:
        identity = INSTALLER.subprocess.CompletedProcess(
            [], 0, stdout='"DOMAIN\\User","S-1-5-21-123"\r\n', stderr=""
        )
        secured = INSTALLER.subprocess.CompletedProcess([], 0, stdout="processed", stderr="")
        with mock.patch.object(INSTALLER.subprocess, "run", side_effect=[identity, secured]) as run:
            user, sid = INSTALLER._windows_identity()
            INSTALLER._secure_windows_tree(Path(r"C:\Users\User\AppData\Local\gowa"), sid)
        self.assertEqual((user, sid), (r"DOMAIN\User", "S-1-5-21-123"))
        acl = run.call_args_list[1].args[0]
        self.assertIn("*S-1-5-21-123:(OI)(CI)F", acl)
        self.assertIn("*S-1-5-18:(OI)(CI)F", acl)
        self.assertIn("/inheritance:r", acl)
        self.assertIn("/T", acl)


if __name__ == "__main__":
    unittest.main()
