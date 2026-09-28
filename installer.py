"""Pinned, one-shot installer behind ``hermes gowa setup``.

The plugin never owns the long-lived process. Setup installs the verified binary,
configures a user systemd service, and wires Hermes to GOWA's native MCP endpoint.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import platform
import secrets
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

GOWA_VERSION = "v9.5.0"
ARCHIVE_NAME = "whatsapp_9.5.0_linux_amd64.zip"
ARCHIVE_URL = (
    "https://github.com/aldinokemal/go-whatsapp-web-multidevice/"
    f"releases/download/{GOWA_VERSION}/{ARCHIVE_NAME}"
)
ARCHIVE_SHA256 = "850a109a5127339adafeca3bd55be0bf5be5a5a3a0e7e2ffdd223536d312138c"
ARCHIVE_MEMBER = "linux-amd64"
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_BINARY_BYTES = 64 * 1024 * 1024
_LOCAL_OPENER = build_opener(ProxyHandler({}))


class SetupError(RuntimeError):
    pass


def register_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="gowa_command")
    setup = subs.add_parser(
        "setup",
        help=f"Install pinned GOWA {GOWA_VERSION}, configure its user service, REST auth, and MCP",
    )
    setup.add_argument("--port", type=int, default=3000, help="Loopback port (default: 3000)")
    parser.set_defaults(func=cli_handler)


def cli_handler(args: argparse.Namespace) -> int:
    if getattr(args, "gowa_command", None) != "setup":
        print("usage: hermes gowa setup [--port PORT]")
        return 2
    try:
        result = install(port=args.port)
    except (SetupError, OSError, ValueError) as exc:
        print(f"gowa setup failed: {exc}")
        return 1
    print(
        f"GOWA {result['version']} is ready\n"
        f"  binary : {result['binary']}\n"
        f"  service: gowa.service (active, enabled)\n"
        f"  REST   : {result['base_url']}\n"
        f"  MCP    : {result['base_url']}/mcp\n"
        "Open a new Hermes session, then ask it to create a device and start QR or phone-code pairing."
    )
    return 0


def install(
    *,
    port: int = 3000,
    _home: Path | None = None,
    _hermes_home: Path | None = None,
    _archive: bytes | None = None,
    _run_service: bool = True,
    _config_setter: Callable[..., Any] | None = None,
) -> dict[str, str]:
    """Install and configure the pinned release; private arguments are test seams."""
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise SetupError("the pinned installer currently supports Linux x86_64 only")
    if not 1024 <= int(port) <= 65535:
        raise SetupError("port must be between 1024 and 65535")

    home = Path(_home) if _home else Path.home()
    if _hermes_home is None:
        from hermes_constants import get_hermes_home

        hermes_home = Path(get_hermes_home())
    else:
        hermes_home = Path(_hermes_home)

    archive = _archive if _archive is not None else _download_archive()
    binary = _install_binary(home, archive)
    credential, header = _configure_files(home, port)
    del credential  # never retain or print the raw Basic credential
    base_url = f"http://127.0.0.1:{port}"
    _configure_hermes(base_url, header, _config_setter)
    unit = _install_unit(home)

    if _run_service:
        _activate_service(unit)
        _verify_service(base_url, header)

    return {
        "version": GOWA_VERSION,
        "binary": str(binary),
        "base_url": base_url,
        "hermes_home": str(hermes_home),
    }


def _download_archive() -> bytes:
    request = Request(ARCHIVE_URL, headers={"User-Agent": "hermes-go-whatsapp-installer/0.2"})
    try:
        with urlopen(request, timeout=90) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > _MAX_ARCHIVE_BYTES:
                raise SetupError("release archive exceeds the download limit")
            archive = response.read(_MAX_ARCHIVE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise SetupError(f"could not download pinned release: {type(exc).__name__}") from exc
    if len(archive) > _MAX_ARCHIVE_BYTES:
        raise SetupError("release archive exceeds the download limit")
    return archive


def _install_binary(home: Path, archive: bytes, expected_sha256: str = ARCHIVE_SHA256) -> Path:
    digest = hashlib.sha256(archive).hexdigest()
    if digest != expected_sha256:
        raise SetupError(f"release checksum mismatch: expected {expected_sha256}, got {digest}")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            info = bundle.getinfo(ARCHIVE_MEMBER)
            if info.is_dir() or info.file_size > _MAX_BINARY_BYTES:
                raise SetupError("release binary has an invalid size")
            payload = bundle.read(info)
    except (KeyError, zipfile.BadZipFile) as exc:
        raise SetupError(f"release archive does not contain {ARCHIVE_MEMBER}") from exc
    if not payload or len(payload) > _MAX_BINARY_BYTES:
        raise SetupError("release binary has an invalid size")

    version_dir = home / ".local/lib/gowa" / GOWA_VERSION
    version_dir.mkdir(parents=True, exist_ok=True)
    binary = version_dir / "whatsapp"
    _atomic_write(binary, payload, 0o755)

    current = version_dir.parent / "current"
    if current.exists() and not current.is_symlink():
        raise SetupError(f"refusing to replace non-symlink path: {current}")
    temp_link = current.with_name(f".current-{secrets.token_hex(4)}")
    temp_link.symlink_to(version_dir, target_is_directory=True)
    os.replace(temp_link, current)
    return binary


def _configure_files(home: Path, port: int) -> tuple[str, str]:
    runtime = home / ".local/share/gowa/runtime"
    for path in (
        runtime,
        runtime / "storages",
        runtime / "statics/qrcode",
        runtime / "statics/senditems",
        runtime / "statics/media",
    ):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)

    env_path = home / ".config/gowa/gowa.env"
    existing = _env_values(env_path)
    raw_auth = existing.get("APP_BASIC_AUTH", "")
    if raw_auth:
        credential = raw_auth.split(",", 1)[0]
        if ":" not in credential or any(ch in credential for ch in "\r\n"):
            raise SetupError(f"invalid APP_BASIC_AUTH in {env_path}; fix it before rerunning setup")
    else:
        credential = "hermes:" + secrets.token_urlsafe(32)
    webhook_secret = existing.get("WHATSAPP_WEBHOOK_SECRET") or secrets.token_urlsafe(32)

    _update_env(
        env_path,
        {
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(port),
            "APP_DEBUG": "false",
            "APP_BASIC_AUTH": raw_auth or credential,
            "APP_CORS_ALLOWED_ORIGINS": f"http://127.0.0.1:{port}",
            "APP_UI_ENABLED": "false",
            "APP_UI_AUTO_UPDATE": "false",
            "MCP_ENABLED": "true",
            "MCP_OAUTH_ENABLED": "false",
            "DB_URI": "file:storages/whatsapp.db?_foreign_keys=on",
            "WHATSAPP_AUTO_DOWNLOAD_MEDIA": "false",
            "WHATSAPP_PRESENCE_ON_CONNECT": "unavailable",
            "WHATSAPP_PRESENCE_PULSE_ENABLED": "false",
            "WHATSAPP_WEBHOOK_INSECURE_SKIP_VERIFY": "false",
            "WHATSAPP_WEBHOOK_SECRET": webhook_secret,
            "CHATWOOT_ENABLED": "false",
        },
    )
    header = "Basic " + base64.b64encode(credential.encode("utf-8")).decode("ascii")
    return credential, header


def _configure_hermes(
    base_url: str,
    auth_header: str,
    setter: Callable[..., Any] | None = None,
) -> None:
    if setter is None:
        from hermes_cli.config import set_config_value

        setter = set_config_value
    for key, value in (
        ("GOWA_BASE_URL", base_url),
        ("GOWA_AUTH_HEADER", auth_header),
        ("mcp_servers.gowa.url", f"{base_url}/mcp"),
        ("mcp_servers.gowa.headers.Authorization", "${GOWA_AUTH_HEADER}"),
        ("mcp_servers.gowa.connect_timeout", "15"),
        ("plugins.entries.gowa.settings.base_url", base_url),
    ):
        setter(key, value, force=True)


def _install_unit(home: Path) -> Path:
    unit_path = home / ".config/systemd/user/gowa.service"
    unit = f"""[Unit]
Description=GOWA WhatsApp REST and MCP server
Documentation=https://github.com/aldinokemal/go-whatsapp-web-multidevice
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.local/share/gowa/runtime
EnvironmentFile=%h/.config/gowa/gowa.env
ExecStart=%h/.local/lib/gowa/current/whatsapp rest
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
KillMode=mixed
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.local/share/gowa/runtime
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
StandardOutput=null
StandardError=journal

[Install]
WantedBy=default.target
"""
    _atomic_write(unit_path, unit.encode("utf-8"), 0o644)
    return unit_path


def _activate_service(unit_path: Path) -> None:
    if shutil.which("systemctl") is None:
        raise SetupError("systemctl is required for the pinned Linux installer")
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", unit_path.name],
        ["systemctl", "--user", "restart", unit_path.name],
        ["systemctl", "--user", "is-active", unit_path.name],
    ):
        result = subprocess.run(command, text=True, capture_output=True, timeout=30, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown systemctl error").strip()[-500:]
            raise SetupError(f"{' '.join(command[:3])} failed: {detail}")


def _verify_service(base_url: str, auth_header: str) -> None:
    last_error = "service did not answer"
    for _ in range(20):
        try:
            request = Request(
                f"{base_url}/app/info",
                headers={"Accept": "application/json", "Authorization": auth_header},
            )
            with _LOCAL_OPENER.open(request, timeout=3) as response:
                payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
            version = ((payload.get("results") or {}).get("version") if isinstance(payload, dict) else None)
            if version != GOWA_VERSION:
                raise SetupError(f"GOWA answered with unexpected version {version!r}")
            return
        except SetupError:
            raise
        except Exception as exc:
            last_error = type(exc).__name__
            time.sleep(0.5)
    raise SetupError(f"service verification failed: {last_error}")


def _env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _update_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in updates:
            if key not in seen:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")
    _atomic_write(path, ("\n".join(output) + "\n").encode("utf-8"), 0o600)


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        path.chmod(mode)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temp_name).unlink(missing_ok=True)
        raise
