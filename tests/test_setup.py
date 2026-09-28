from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

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
