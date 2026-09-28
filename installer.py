"""One-shot setup behind ``hermes gowa setup``.

Setup either verifies an existing GOWA or installs the pinned binary as a
native per-user service. The plugin never owns the long-lived process.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import platform
import plistlib
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen

GOWA_VERSION = "v9.5.0"
_RELEASE_BASE = (
    "https://github.com/aldinokemal/go-whatsapp-web-multidevice/"
    f"releases/download/{GOWA_VERSION}"
)
_RELEASES = {
    ("Linux", "x86_64"): (
        "whatsapp_9.5.0_linux_amd64.zip",
        "850a109a5127339adafeca3bd55be0bf5be5a5a3a0e7e2ffdd223536d312138c",
        "linux-amd64",
    ),
    ("Darwin", "x86_64"): (
        "whatsapp_9.5.0_darwin_amd64.zip",
        "b57d6fa46bbef88fb3dd1708174d4e42cdcae7dea70250961a3f70f7c06e207b",
        "darwin-amd64",
    ),
    ("Darwin", "arm64"): (
        "whatsapp_9.5.0_darwin_arm64.zip",
        "0a5639e0608aaae3e1c7977a16303b782c2ea3ff7be8f73ecf0bed89adf7a444",
        "darwin-arm64",
    ),
    ("Windows", "amd64"): (
        "whatsapp_9.5.0_windows_amd64.zip",
        "611e66c5751657980b12a5a216af466f19548cba89e66b0b5a1c353e5a0ee825",
        "windows-amd64.exe",
    ),
}
# Linux defaults preserve the installer helper's existing test seam.
ARCHIVE_NAME, ARCHIVE_SHA256, ARCHIVE_MEMBER = _RELEASES[("Linux", "x86_64")]
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_BINARY_BYTES = 64 * 1024 * 1024
_LOCAL_OPENER = build_opener(ProxyHandler({}))
_TASK_NAME = "Hermes GOWA"
_LAUNCHD_LABEL = "com.hermes.gowa"
_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_EXTERNAL_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


class SetupError(RuntimeError):
    pass


def register_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="gowa_command")
    setup = subs.add_parser(
        "setup",
        help=f"Install pinned GOWA {GOWA_VERSION}, or connect an existing server",
    )
    setup.add_argument("--port", type=int, help="Local loopback port (default: kernel-selected free port)")
    setup.add_argument("--base-url", help="Existing GOWA origin; skips download and service setup")
    setup.add_argument("--auth-bearer", metavar="TOKEN", help="Bearer token for an existing GOWA server")
    parser.set_defaults(func=cli_handler)


def cli_handler(args: argparse.Namespace) -> int:
    if getattr(args, "gowa_command", None) != "setup":
        print("usage: hermes gowa setup [--port PORT] | --base-url URL [--auth-bearer TOKEN]")
        return 2
    try:
        result = install(port=args.port, base_url=args.base_url, auth_bearer=args.auth_bearer)
    except (SetupError, OSError, ValueError) as exc:
        print(f"gowa setup failed: {exc}")
        return 1
    details = [f"GOWA {result['version']} is ready", f"  mode   : {result['mode']}"]
    if result.get("service"):
        details.extend((f"  binary : {result['binary']}", f"  service: {result['service']}"))
    details.extend(
        (
            f"  REST   : {result['base_url']}",
            f"  MCP    : {result['base_url']}/mcp",
            "Open a new Hermes session, then ask it to create a device and start QR or phone-code pairing.",
        )
    )
    print("\n".join(details))
    return 0


def install(
    *,
    port: int | None = None,
    base_url: str | None = None,
    auth_bearer: str | None = None,
    _home: Path | None = None,
    _hermes_home: Path | None = None,
    _archive: bytes | None = None,
    _run_service: bool = True,
    _config_setter: Callable[..., Any] | None = None,
    _config_unsetter: Callable[[str], Any] | None = None,
) -> dict[str, str]:
    """Install and configure the pinned release; private arguments are test seams."""
    if auth_bearer is not None and base_url is None:
        raise SetupError("--auth-bearer requires --base-url")
    if base_url is not None and port is not None:
        raise SetupError("--port cannot be combined with --base-url")

    if _hermes_home is None:
        from hermes_constants import get_hermes_home

        hermes_home = Path(get_hermes_home())
    else:
        hermes_home = Path(_hermes_home)

    if base_url is not None:
        normalized = _normalize_base_url(base_url)
        header = _bearer_header(auth_bearer)
        host = (urlsplit(normalized).hostname or "").lower()
        if header is None and host not in {"127.0.0.1", "localhost", "::1"}:
            raise SetupError("a remote --base-url requires --auth-bearer")
        version = _verify_existing(normalized, header)
        _configure_hermes(normalized, header, _config_setter, _config_unsetter)
        return {
            "version": version,
            "mode": "existing server (not managed)",
            "service": "",
            "binary": "",
            "base_url": normalized,
            "hermes_home": str(hermes_home),
        }

    system = platform.system()
    machine = _normalized_machine(platform.machine())
    asset_name, asset_sha256, asset_member = _release_asset(system, machine)
    home = Path(_home) if _home else Path.home()
    managed_port = _managed_port(home, system)
    if port is not None:
        if not 1024 <= int(port) <= 65535:
            raise SetupError("port must be between 1024 and 65535")
        if port != managed_port:
            _ensure_port_free(port)

    archive = _archive if _archive is not None else _download_archive(asset_name)
    binary = _install_binary(home, archive, asset_sha256, asset_member, system)
    if port is None:
        port = managed_port or _pick_free_loopback_port()
    credential, header = _configure_files(home, port, system)
    del credential  # never retain or print the raw Basic credential
    base_url = f"http://127.0.0.1:{port}"
    service, service_name = _install_service(home, binary, system)

    if _run_service:
        _activate_service(service, system, port)
        _verify_service(base_url, header)
    _configure_hermes(base_url, header, _config_setter, _config_unsetter)

    return {
        "version": GOWA_VERSION,
        "mode": f"local {_service_kind(system)}",
        "service": service_name,
        "binary": str(binary),
        "base_url": base_url,
        "hermes_home": str(hermes_home),
    }


def _normalized_machine(machine: str) -> str:
    normalized = machine.strip().lower()
    return {"amd64": "amd64", "x86_64": "x86_64", "aarch64": "arm64"}.get(normalized, normalized)


def _release_asset(system: str, machine: str) -> tuple[str, str, str]:
    key = (system, machine)
    if system == "Linux" and machine == "amd64":
        key = (system, "x86_64")
    if system == "Darwin" and machine == "amd64":
        key = (system, "x86_64")
    if system == "Windows" and machine == "x86_64":
        key = (system, "amd64")
    try:
        return _RELEASES[key]
    except KeyError as exc:
        supported = "Linux x86_64, macOS x86_64/arm64, and Windows x86_64"
        raise SetupError(f"the pinned installer supports {supported}; got {system} {machine}") from exc


def _service_kind(system: str) -> str:
    return {"Linux": "systemd service", "Darwin": "launchd service", "Windows": "scheduled task"}[system]


def _normalize_base_url(raw: str) -> str:
    base_url = str(raw).strip().rstrip("/")
    parsed = urlsplit(base_url)
    try:
        parsed.port
    except ValueError as exc:
        raise SetupError("--base-url contains an invalid port") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SetupError("--base-url must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SetupError("--base-url cannot contain credentials, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise SetupError("plain HTTP is allowed only on loopback; use HTTPS remotely")
    return base_url


def _bearer_header(token: str | None) -> str | None:
    if token is None:
        return None
    if not token or token != token.strip() or len(token) > 8192 or any(ch.isspace() for ch in token):
        raise SetupError("--auth-bearer must be a non-empty token without whitespace")
    return f"Bearer {token}"


def _external_read(request: Request, auth_header: str | None, surface: str) -> bytes:
    if auth_header:
        request.add_header("Authorization", auth_header)
    try:
        with _EXTERNAL_OPENER.open(request, timeout=15) as response:
            body = response.read(1024 * 1024 + 1)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        if status in {401, 403}:
            if auth_header:
                raise SetupError(f"existing GOWA rejected --auth-bearer on {surface} (HTTP {status})") from exc
            raise SetupError(
                f"existing GOWA requires authentication on {surface}; rerun with --auth-bearer TOKEN"
            ) from exc
        raise SetupError(f"existing GOWA {surface} probe returned HTTP {status}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SetupError(f"could not connect to existing GOWA {surface}: {type(exc).__name__}") from exc
    if len(body) > 1024 * 1024:
        raise SetupError(f"existing GOWA {surface} response exceeded 1 MiB")
    return body


def _verify_existing(base_url: str, auth_header: str | None) -> str:
    info_request = Request(f"{base_url}/app/info", headers={"Accept": "application/json"})
    try:
        info = json.loads(_external_read(info_request, auth_header, "REST").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError("existing GOWA REST probe returned invalid JSON") from exc
    info_results = info.get("results") if isinstance(info, dict) else None
    version = info_results.get("version") if isinstance(info_results, dict) else None
    if not isinstance(version, str) or not version.startswith("v"):
        raise SetupError("existing server did not identify itself as GOWA on /app/info")

    devices_request = Request(f"{base_url}/devices", headers={"Accept": "application/json"})
    try:
        devices = json.loads(_external_read(devices_request, auth_header, "REST /devices").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError("existing GOWA /devices probe returned invalid JSON") from exc
    device_results = devices.get("results") if isinstance(devices, dict) else object()
    if not isinstance(devices, dict) or "results" not in devices or (
        device_results is not None and not isinstance(device_results, list)
    ):
        raise SetupError("existing server did not return a GOWA device list on /devices")

    initialize = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "hermes-go-whatsapp-setup", "version": "0.4.0"},
            },
        }
    ).encode("utf-8")
    mcp_request = Request(
        f"{base_url}/mcp",
        data=initialize,
        method="POST",
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
    )
    try:
        mcp = json.loads(_external_read(mcp_request, auth_header, "MCP").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError("existing GOWA MCP probe returned invalid JSON") from exc
    mcp_result = mcp.get("result") if isinstance(mcp, dict) else None
    server_info = mcp_result.get("serverInfo") if isinstance(mcp_result, dict) else None
    server_name = server_info.get("name") if isinstance(server_info, dict) else None
    if server_name != "WhatsApp Web Multidevice MCP Server":
        raise SetupError("existing server did not identify itself as GOWA on /mcp")
    return version


def _windows_root(home: Path) -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(home / "AppData/Local"))) / "gowa"


def _runtime_dir(home: Path, system: str) -> Path:
    if system == "Windows":
        return _windows_root(home) / "runtime"
    return home / ".local/share/gowa/runtime"


def _env_path(home: Path, system: str) -> Path:
    if system == "Linux":
        return home / ".config/gowa/gowa.env"
    return _runtime_dir(home, system) / ".env"


def _service_path(home: Path, system: str) -> Path:
    if system == "Linux":
        return home / ".config/systemd/user/gowa.service"
    if system == "Darwin":
        return home / f"Library/LaunchAgents/{_LAUNCHD_LABEL}.plist"
    return _windows_root(home) / "gowa-task.xml"


def _managed_port(home: Path, system: str = "Linux") -> int | None:
    service = _service_path(home, system)
    env_path = _env_path(home, system)
    if not service.exists():
        return None
    values = _env_values(env_path)
    try:
        port = int(values["APP_PORT"])
    except (KeyError, ValueError) as exc:
        raise SetupError(f"existing managed GOWA has an invalid APP_PORT in {env_path}") from exc
    if values.get("APP_HOST") != "127.0.0.1" or not 1024 <= port <= 65535:
        raise SetupError(f"existing managed GOWA has an invalid loopback address in {env_path}")
    return port


def _pick_free_loopback_port() -> int:
    # ponytail: let the kernel choose; reserving the socket through the supervisor is only needed if this race is observed.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _ensure_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise SetupError(f"loopback port {port} is already in use") from exc


def _wait_port_closed(port: int) -> None:
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                pass
        except OSError:
            return
        time.sleep(0.25)
    raise SetupError(f"previous GOWA process did not release loopback port {port}")


def _download_archive(asset_name: str = ARCHIVE_NAME) -> bytes:
    request = Request(
        f"{_RELEASE_BASE}/{asset_name}",
        headers={"User-Agent": "hermes-go-whatsapp-installer/0.4"},
    )
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


def _install_binary(
    home: Path,
    archive: bytes,
    expected_sha256: str = ARCHIVE_SHA256,
    member: str = ARCHIVE_MEMBER,
    system: str = "Linux",
) -> Path:
    digest = hashlib.sha256(archive).hexdigest()
    if digest != expected_sha256:
        raise SetupError(f"release checksum mismatch: expected {expected_sha256}, got {digest}")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            info = bundle.getinfo(member)
            if info.is_dir() or info.file_size > _MAX_BINARY_BYTES:
                raise SetupError("release binary has an invalid size")
            payload = bundle.read(info)
    except (KeyError, zipfile.BadZipFile) as exc:
        raise SetupError(f"release archive does not contain {member}") from exc
    if not payload or len(payload) > _MAX_BINARY_BYTES:
        raise SetupError("release binary has an invalid size")

    if system == "Windows":
        version_dir = _windows_root(home) / "lib" / GOWA_VERSION
        binary = version_dir / "whatsapp.exe"
        _atomic_write(binary, payload, 0o700)
        return binary

    version_dir = home / ".local/lib/gowa" / GOWA_VERSION
    binary = version_dir / "whatsapp"
    _atomic_write(binary, payload, 0o755)
    current = version_dir.parent / "current"
    if current.exists() and not current.is_symlink():
        raise SetupError(f"refusing to replace non-symlink path: {current}")
    temp_link = current.with_name(f".current-{secrets.token_hex(4)}")
    temp_link.symlink_to(version_dir, target_is_directory=True)
    os.replace(temp_link, current)
    return binary


def _configure_files(home: Path, port: int, system: str = "Linux") -> tuple[str, str]:
    runtime = _runtime_dir(home, system)
    for path in (
        runtime,
        runtime / "logs",
        runtime / "storages",
        runtime / "statics/qrcode",
        runtime / "statics/senditems",
        runtime / "statics/media",
    ):
        path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            path.chmod(0o700)

    env_path = _env_path(home, system)
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
    auth_header: str | None,
    setter: Callable[..., Any] | None = None,
    unsetter: Callable[[str], Any] | None = None,
) -> None:
    if setter is None:
        from hermes_cli.config import set_config_value

        setter = set_config_value
    if not auth_header:
        if unsetter is None:
            from hermes_cli.config import load_config, unset_config_value
            from hermes_cli.config_env_routing import remove_env_setting

            def default_unsetter(key: str) -> None:
                if key == "GOWA_AUTH_HEADER":
                    # remove_env_setting also clears the live process value and is a no-op if absent.
                    remove_env_setting(key)
                    return
                value: Any = load_config()
                for part in key.split("."):
                    if not isinstance(value, dict) or part not in value:
                        return
                    value = value[part]
                unset_config_value(key)

            unsetter = default_unsetter
        unsetter("GOWA_AUTH_HEADER")
        unsetter("mcp_servers.gowa.headers.Authorization")

    for key, value in (
        ("GOWA_BASE_URL", base_url),
        ("mcp_servers.gowa.url", f"{base_url}/mcp"),
        ("mcp_servers.gowa.connect_timeout", "15"),
        ("plugins.entries.gowa.settings.base_url", base_url),
    ):
        setter(key, value, force=True)
    if auth_header:
        setter("GOWA_AUTH_HEADER", auth_header, force=True)
        setter("mcp_servers.gowa.headers.Authorization", "${GOWA_AUTH_HEADER}", force=True)


def _install_service(home: Path, binary: Path, system: str) -> tuple[Path, str]:
    if system == "Linux":
        unit = _install_unit(home)
        return unit, "gowa.service (active, enabled)"
    if system == "Darwin":
        plist = _install_launch_agent(home)
        return plist, f"{_LAUNCHD_LABEL} (loaded)"
    user_id, sid = _windows_identity()
    task = _install_windows_task(home, binary, user_id)
    _secure_windows_tree(_windows_root(home), sid)
    return task, f"{_TASK_NAME} (running, logon-triggered)"


def _install_unit(home: Path) -> Path:
    unit_path = _service_path(home, "Linux")
    unit = """[Unit]
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


def _install_launch_agent(home: Path) -> Path:
    runtime = _runtime_dir(home, "Darwin").resolve()
    plist_path = _service_path(home, "Darwin")
    values = {
        "Label": _LAUNCHD_LABEL,
        "ProgramArguments": [str((home / ".local/lib/gowa/current/whatsapp").resolve()), "rest"],
        "WorkingDirectory": str(runtime),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ExitTimeOut": 20,
        "Umask": 0o077,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(runtime / "logs/gowa.err.log"),
    }
    _atomic_write(plist_path, plistlib.dumps(values, fmt=plistlib.FMT_XML, sort_keys=False), 0o600)
    return plist_path


def _ps_literal(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _windows_identity() -> tuple[str, str]:
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise SetupError(f"whoami failed: {(result.stderr or result.stdout).strip()[-500:]}")
    try:
        user_id, sid, *_ = next(csv.reader([result.stdout.strip()]))
    except (StopIteration, ValueError) as exc:
        raise SetupError("whoami returned an invalid current-user identity") from exc
    if not user_id or not sid.startswith("S-1-"):
        raise SetupError("whoami returned an invalid current-user identity")
    return user_id, sid


def _install_windows_task(home: Path, binary: Path, user_id: str) -> Path:
    root = _windows_root(home)
    runtime = _runtime_dir(home, "Windows")
    script = root / "run.ps1"
    stderr_log = runtime / "logs/gowa.err.log"
    script_text = (
        "$ErrorActionPreference = 'Stop'\r\n"
        f"Set-Location -LiteralPath {_ps_literal(runtime)}\r\n"
        f"& {_ps_literal(binary)} rest 1>$null 2>{_ps_literal(stderr_log)}\r\n"
        "exit $LASTEXITCODE\r\n"
    )
    _atomic_write(script, script_text.encode("utf-8-sig"), 0o600)

    ET.register_namespace("", _TASK_NS)
    q = lambda name: f"{{{_TASK_NS}}}{name}"
    task = ET.Element(q("Task"), {"version": "1.4"})
    registration = ET.SubElement(task, q("RegistrationInfo"))
    ET.SubElement(registration, q("Description")).text = "GOWA WhatsApp REST and MCP server"
    triggers = ET.SubElement(task, q("Triggers"))
    logon = ET.SubElement(triggers, q("LogonTrigger"))
    ET.SubElement(logon, q("Enabled")).text = "true"
    ET.SubElement(logon, q("UserId")).text = user_id
    watchdog = ET.SubElement(triggers, q("TimeTrigger"))
    ET.SubElement(watchdog, q("Enabled")).text = "true"
    ET.SubElement(watchdog, q("StartBoundary")).text = datetime.now().astimezone().isoformat(timespec="seconds")
    repetition = ET.SubElement(watchdog, q("Repetition"))
    ET.SubElement(repetition, q("Interval")).text = "PT1M"
    ET.SubElement(repetition, q("StopAtDurationEnd")).text = "false"
    principals = ET.SubElement(task, q("Principals"))
    principal = ET.SubElement(principals, q("Principal"), {"id": "Author"})
    ET.SubElement(principal, q("UserId")).text = user_id
    ET.SubElement(principal, q("LogonType")).text = "InteractiveToken"
    ET.SubElement(principal, q("RunLevel")).text = "LeastPrivilege"
    settings = ET.SubElement(task, q("Settings"))
    for name, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "true"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true"),
        ("Hidden", "true"),
        ("RunOnlyIfIdle", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT0S"),
        ("Priority", "7"),
    ):
        ET.SubElement(settings, q(name)).text = value
    actions = ET.SubElement(task, q("Actions"), {"Context": "Author"})
    execute = ET.SubElement(actions, q("Exec"))
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    ET.SubElement(execute, q("Command")).text = str(powershell)
    ET.SubElement(execute, q("Arguments")).text = (
        f'-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{script}"'
    )
    ET.SubElement(execute, q("WorkingDirectory")).text = str(runtime)
    ET.indent(task, space="  ")
    task_path = _service_path(home, "Windows")
    _atomic_write(task_path, ET.tostring(task, encoding="utf-16", xml_declaration=True), 0o600)
    return task_path


def _secure_windows_tree(root: Path, sid: str) -> None:
    result = subprocess.run(
        [
            "icacls",
            str(root),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
            "/T",
        ],
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown icacls error").strip()[-500:]
        raise SetupError(f"could not secure GOWA files with icacls: {detail}")


def _activate_service(service_path: Path, system: str, port: int) -> None:
    if system == "Linux":
        _activate_systemd(service_path)
    elif system == "Darwin":
        _activate_launchd(service_path, port)
    else:
        _activate_windows_task(service_path, port)


def _activate_systemd(unit_path: Path) -> None:
    if shutil.which("systemctl") is None:
        raise SetupError("systemctl is required for the pinned Linux installer")
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", unit_path.name],
        ["systemctl", "--user", "restart", unit_path.name],
        ["systemctl", "--user", "is-active", unit_path.name],
    ):
        _run_checked(command)


def _activate_launchd(plist_path: Path, port: int) -> None:
    if shutil.which("launchctl") is None:
        raise SetupError("launchctl is required for the pinned macOS installer")
    target = f"gui/{os.getuid()}"
    service = f"{target}/{_LAUNCHD_LABEL}"
    printed = subprocess.run(
        ["launchctl", "print", service],
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if printed.returncode == 0:
        _run_checked(["launchctl", "bootout", service])
        _wait_port_closed(port)
    _run_checked(["launchctl", "enable", service])
    _run_checked(["launchctl", "bootstrap", target, str(plist_path)])


def _activate_windows_task(task_path: Path, port: int) -> None:
    if shutil.which("schtasks") is None:
        raise SetupError("schtasks.exe is required for the pinned Windows installer")
    subprocess.run(
        ["schtasks", "/End", "/TN", _TASK_NAME],
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    _wait_port_closed(port)
    _run_checked(["schtasks", "/Create", "/TN", _TASK_NAME, "/XML", str(task_path), "/F"])
    _run_checked(["schtasks", "/Run", "/TN", _TASK_NAME])


def _run_checked(command: list[str]) -> None:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown command error").strip()[-500:]
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
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        if os.name != "nt":
            path.chmod(mode)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temp_name).unlink(missing_ok=True)
        raise
