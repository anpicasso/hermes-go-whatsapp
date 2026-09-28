from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
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


def archive(payload: bytes = b"fake-gowa-binary") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(INSTALLER.ARCHIVE_MEMBER, payload)
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


if __name__ == "__main__":
    unittest.main()
