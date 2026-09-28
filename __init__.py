"""Minimal Hermes tools for GOWA REST features missing from its native MCP."""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_CTX: Any = None
_MAX_JSON = 2 * 1024 * 1024
_MAX_QR = 1024 * 1024
_PHONE = re.compile(r"^\+?[1-9][0-9]{6,14}$")


class GowaError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


def _config(key: str, default: Any) -> Any:
    if _CTX is None:
        raise GowaError("GOWA plugin is not registered")
    return _CTX.get_config(key, default)


def _base_url() -> str:
    raw = str(_config("base_url", "http://127.0.0.1:3000")).strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise GowaError("base_url must be an http(s) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GowaError("base_url cannot contain credentials, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise GowaError("plain HTTP is allowed only on loopback; use HTTPS remotely")
    return raw


def _timeout(login: bool = False) -> float:
    if login:
        return 135.0
    try:
        value = float(_config("timeout_seconds", 15))
    except (TypeError, ValueError) as exc:
        raise GowaError("timeout_seconds must be numeric") from exc
    if not 1 <= value <= 60:
        raise GowaError("timeout_seconds must be between 1 and 60")
    return value


def _auth_header() -> str:
    from hermes_cli.config import get_env_value_prefer_dotenv

    value = (get_env_value_prefer_dotenv("GOWA_AUTH_HEADER") or "").strip()
    if not value or "\n" in value or "\r" in value:
        raise GowaError("GOWA_AUTH_HEADER is missing or invalid")
    if not value.startswith(("Basic ", "Bearer ")):
        raise GowaError("GOWA_AUTH_HEADER must be a complete Basic or Bearer Authorization value")
    return value


def _device_id(value: Any) -> str:
    device_id = str(value or "")
    if not device_id or device_id != device_id.strip() or len(device_id) > 256:
        raise GowaError("device_id must be non-empty, unpadded, and at most 256 characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in device_id):
        raise GowaError("device_id cannot contain control characters")
    return device_id


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _read(req: Request, timeout: float, limit: int) -> bytes:
    try:
        with _OPENER.open(req, timeout=timeout) as response:
            body = response.read(limit + 1)
    except HTTPError as exc:
        body = exc.read(4097)[:4096]
        message = f"GOWA returned HTTP {exc.code}"
        try:
            payload = json.loads(body.decode("utf-8"))
            detail = payload.get("message") or payload.get("error")
            if detail:
                message += f": {str(detail)[:300]}"
        except Exception:
            pass
        raise GowaError(message, exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise GowaError(f"GOWA is unavailable: {type(exc).__name__}") from exc
    if len(body) > limit:
        raise GowaError("GOWA response exceeded the allowed size")
    return body


def _request(method: str, path: str, body: dict[str, Any] | None = None, *, login: bool = False) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(
        _base_url() + "/" + path.lstrip("/"),
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": _auth_header(),
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    raw = _read(req, _timeout(login), _MAX_JSON)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GowaError("GOWA returned invalid JSON") from exc


def _download_qr(url: str) -> str:
    base = _base_url()
    if _origin(url) != _origin(base):
        raise GowaError("GOWA returned a QR URL on a different origin")
    req = Request(url, method="GET", headers={"Authorization": _auth_header()})
    png = _read(req, _timeout(), _MAX_QR)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise GowaError("GOWA QR response was not a PNG")
    state = getattr(getattr(_CTX, "state", None), "data_dir", None)
    target_dir = Path(state) if state else Path.home() / ".hermes/plugin-data/gowa"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "login-qr.png"
    # ponytail: keep one private QR per profile; add per-device files only if concurrent pairing matters.
    fd, temp_name = tempfile.mkstemp(prefix=".login-qr-", dir=target_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(png)
        os.replace(temp_name, target)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temp_name).unlink(missing_ok=True)
        raise
    return str(target)


def _safe(handler: Callable[[dict[str, Any]], Any]) -> Callable[..., str]:
    def wrapped(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        try:
            return json.dumps({"ok": True, "data": handler(args or {})}, ensure_ascii=False)
        except GowaError as exc:
            payload: dict[str, Any] = {"ok": False, "error": str(exc)}
            if exc.status is not None:
                payload["http_status"] = exc.status
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"Unexpected plugin failure: {type(exc).__name__}"})

    return wrapped


def _devices(args: dict[str, Any]) -> Any:
    action = args.get("action")
    if action == "list":
        return _request("GET", "/devices")
    if action == "get":
        device_id = quote(_device_id(args.get("device_id")), safe="")
        return _request("GET", f"/devices/{device_id}")
    raise GowaError("action must be list or get")


def _device_login(args: dict[str, Any]) -> Any:
    action = args.get("action")
    if action not in {"qr", "code"}:
        raise GowaError("action must be qr or code")
    raw_id = _device_id(args.get("device_id"))
    device_id = quote(raw_id, safe="")
    try:
        _request("GET", f"/devices/{device_id}")
    except GowaError as exc:
        if exc.status != 404 or args.get("create_if_missing") is not True:
            raise
        _request("POST", "/devices", {"device_id": raw_id})

    if action == "code":
        phone = str(args.get("phone") or "")
        if not _PHONE.fullmatch(phone):
            raise GowaError("phone must be an international number with 7-15 digits and optional leading +")
        return _request("POST", f"/devices/{device_id}/login/code?{urlencode({'phone': phone})}", login=True)

    payload = _request("GET", f"/devices/{device_id}/login", login=True)
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, dict):
        raise GowaError("GOWA login response did not include results")
    qr_url = results.pop("qr_link", None)
    if not isinstance(qr_url, str) or not qr_url:
        raise GowaError("GOWA login response did not include qr_link")
    results["qr_path"] = _download_qr(qr_url)
    results["sensitive"] = "Scan only from the intended WhatsApp account; do not share this QR."
    return payload


def _device_remove(args: dict[str, Any]) -> Any:
    if args.get("confirm") is not True:
        raise GowaError("confirm must be true after explicit user approval; deletion is irreversible")
    device_id = quote(_device_id(args.get("device_id")), safe="")
    return _request("DELETE", f"/devices/{device_id}")


DEVICES_SCHEMA = {
    "name": "gowa_devices",
    "description": "List GOWA device slots or retrieve one exact device. Results may contain phone/account metadata; do not store them in memory unless requested.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get"]},
            "device_id": {"type": "string", "description": "Required for get; exact slot id or JID."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

LOGIN_SCHEMA = {
    "name": "gowa_device_login",
    "description": "Begin user-requested WhatsApp pairing for a GOWA device slot. QR/code outputs are temporary credentials: reveal them only to the requesting user and never save them to memory. Set create_if_missing only when provisioning a new slot.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["qr", "code"]},
            "device_id": {"type": "string", "description": "Exact device slot id."},
            "phone": {"type": "string", "description": "Required for code: international number, 7-15 digits, optional leading +."},
            "create_if_missing": {"type": "boolean", "default": False},
        },
        "required": ["action", "device_id"],
        "additionalProperties": False,
    },
}

REMOVE_SCHEMA = {
    "name": "gowa_device_remove",
    "description": "Permanently unlink and purge one exact GOWA device slot and its stored session/data. Call only after explicit user confirmation in the current conversation.",
    "parameters": {
        "type": "object",
        "properties": {
            "device_id": {"type": "string", "description": "Exact device slot id; no default resolution."},
            "confirm": {"type": "boolean", "description": "Must be true after explicit user confirmation."},
        },
        "required": ["device_id", "confirm"],
        "additionalProperties": False,
    },
}


def register(ctx: Any) -> None:
    global _CTX
    _CTX = ctx
    for name, schema, handler, description, emoji in (
        ("gowa_devices", DEVICES_SCHEMA, _devices, "List or inspect GOWA WhatsApp device slots", "📱"),
        ("gowa_device_login", LOGIN_SCHEMA, _device_login, "Provision and pair a GOWA WhatsApp device", "🔗"),
        ("gowa_device_remove", REMOVE_SCHEMA, _device_remove, "Irreversibly remove a GOWA WhatsApp device", "🗑️"),
    ):
        ctx.register_tool(name=name, toolset="gowa", schema=schema, handler=_safe(handler), description=description, emoji=emoji)
    from . import installer

    ctx.register_cli_command(
        name="gowa",
        help="Install and configure the pinned local GOWA server",
        setup_fn=installer.register_cli,
        handler_fn=installer.cli_handler,
        description="Install GOWA v9.5.0, configure its user service, MCP endpoint, and device tools.",
    )


def _demo() -> None:
    """One stdlib-only end-to-end check against a fake REST server."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    devices: set[str] = set()
    server: ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return None

        def send_json(self, payload: Any, status: int = 200) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/devices":
                return self.send_json({"results": [{"id": item} for item in sorted(devices)]})
            if path == "/devices/demo":
                return self.send_json({"results": {"id": "demo"}}) if "demo" in devices else self.send_json({"message": "not found"}, 404)
            if path == "/devices/demo/login":
                return self.send_json({"results": {"device_id": "demo", "qr_link": f"http://127.0.0.1:{server.server_port}/statics/qrcode/demo.png", "qr_duration": 30}})
            if path == "/statics/qrcode/demo.png":
                raw = b"\x89PNG\r\n\x1a\nself-check"
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return None
            return self.send_json({"message": "not found"}, 404)

        def do_POST(self) -> None:
            if self.path == "/devices":
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                devices.add(body["device_id"])
                return self.send_json({"results": {"id": body["device_id"]}})
            return self.send_json({"message": "not found"}, 404)

        def do_DELETE(self) -> None:
            if self.path == "/devices/demo":
                devices.discard("demo")
                return self.send_json({"message": "Device removed"})
            return self.send_json({"message": "not found"}, 404)

    old_ctx = globals()["_CTX"]
    with tempfile.TemporaryDirectory() as tmp:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        class FakeCtx:
            state = SimpleNamespace(data_dir=Path(tmp))

            def get_config(self, key: str, default: Any = None) -> Any:
                return f"http://127.0.0.1:{server.server_port}" if key == "base_url" else default

        globals()["_CTX"] = FakeCtx()
        try:
            login = json.loads(_safe(_device_login)({"action": "qr", "device_id": "demo", "create_if_missing": True}))
            assert login["ok"] and Path(login["data"]["results"]["qr_path"]).is_file(), login
            refused = json.loads(_safe(_device_remove)({"device_id": "demo", "confirm": False}))
            assert not refused["ok"] and "irreversible" in refused["error"], refused
            removed = json.loads(_safe(_device_remove)({"device_id": "demo", "confirm": True}))
            assert removed["ok"] and "demo" not in devices, removed
        finally:
            server.shutdown()
            server.server_close()
            globals()["_CTX"] = old_ctx
    print("gowa plugin self-check OK")


if __name__ == "__main__":
    _demo()
