# Hermes Go WhatsApp

[![CI](https://github.com/anpicasso/hermes-go-whatsapp/actions/workflows/ci.yml/badge.svg)](https://github.com/anpicasso/hermes-go-whatsapp/actions/workflows/ci.yml)

A Hermes Agent plugin that connects an existing [GOWA](https://github.com/aldinokemal/go-whatsapp-web-multidevice) server or installs a pinned local one, registers its native MCP endpoint, and adds the device-management operations that MCP does not expose.

## What it provides

- `hermes gowa setup`: installs and verifies **GOWA v9.5.0** on Linux x86_64, using a kernel-selected free loopback port.
- `hermes gowa setup --base-url ...`: verifies and connects an existing GOWA without downloading or installing anything.
- For local setup, a persistent user `systemd` service bound to `127.0.0.1` with generated Basic Auth.
- Native GOWA MCP tools for sending, messages, chats, groups, schedules, and session status.
- Three Hermes tools for the REST gaps:
  - `gowa_devices` — list or inspect device slots.
  - `gowa_device_login` — create a slot if needed, then start QR or phone-code pairing.
  - `gowa_device_remove` — purge a device and request unlinking after explicit confirmation.

The plugin does **not** supervise the daemon from Hermes' runtime. Hermes loads plugins in CLI, gateway, cron, and worker processes; tying a long-lived server to any one of them creates duplicate-process and orphan-cleanup problems. The one-shot setup command installs it, while `systemd` owns its lifetime.

## Scope: outbound agent messaging

This plugin is deliberately an outbound-control integration: it lets an agent use the user's linked WhatsApp account to send messages to multiple recipients and manage the GOWA device slots needed for that work. It does **not** subscribe to or ingest the account's incoming message stream.

Webhook configuration is intentionally not implemented. Once inbound WhatsApp messages should become Hermes conversations, use Hermes' built-in WhatsApp gateway instead; duplicating it through GOWA webhooks would add a second listener and an unnecessary prompt-injection surface outside this plugin's purpose.

## Requirements

- For local installation: Linux x86_64 with a working user `systemd` manager.
- Hermes Agent 0.21.5 or newer.
- A WhatsApp account able to link another device.
- Network access to WhatsApp; local installation also needs GitHub Releases access.

## Install

```bash
hermes plugins install anpicasso/hermes-go-whatsapp --enable
hermes gowa setup
```

Setup is idempotent: rerunning it reinstalls the same verified release, preserves existing generated credentials, updates configuration, and restarts the service.

The first local setup asks the kernel for an available ephemeral loopback port; it does not guess random ports or maintain a pool. The selected port is persisted in `~/.config/gowa/gowa.env` and reused on later runs. An explicit occupied `--port` fails before download. There is an unavoidable tiny bind/release/start race, so setup also verifies the service after startup and fails rather than silently connecting to the wrong process.

To use another loopback port:

```bash
hermes gowa setup --port 3456
```

### Connect an existing GOWA

To connect a GOWA that is already running, without downloading a binary or creating a service:

```bash
hermes gowa setup \
  --base-url https://gowa.example.com \
  --auth-bearer "$GOWA_TOKEN"
```

Setup probes `GET /app/info`, `GET /devices`, and the MCP `initialize` method on `POST /mcp` **before** changing Hermes configuration. A missing or rejected bearer token is an error, as is a non-GOWA response. `--auth-bearer` without `--base-url`, and `--port` together with `--base-url`, are rejected.

An unauthenticated existing server is supported only on loopback:

```bash
hermes gowa setup --base-url http://127.0.0.1:49152
```

Remote URLs require HTTPS and a bearer token. The token is saved as `GOWA_AUTH_HEADER` in the active Hermes profile's credential environment, not in this repository or the plugin directory. Be aware that GOWA's built-in OAuth bearer handling protects `/mcp`; its global Basic Auth protects REST separately. Because this plugin needs both MCP and `/devices`, a single bearer must be accepted by both surfaces—typically through a reverse proxy—or REST must be unauthenticated on trusted loopback. Setup tests both and refuses a half-working connection.

Start a new Hermes session after setup so its tool catalog includes the plugin and MCP tools.

## What setup changes

In existing-server mode, setup changes only the active Hermes profile: `GOWA_BASE_URL`, optional `GOWA_AUTH_HEADER`, `mcp_servers.gowa`, and `plugins.entries.gowa.settings.base_url`. It does not download GOWA, write systemd files, or manage that server's lifetime.

In local-install mode, the installer downloads exactly:

- Release: `v9.5.0`
- Asset: `whatsapp_9.5.0_linux_amd64.zip`
- SHA-256: `850a109a5127339adafeca3bd55be0bf5be5a5a3a0e7e2ffdd223536d312138c`

It then creates:

- `~/.local/lib/gowa/v9.5.0/whatsapp` — versioned binary.
- `~/.local/lib/gowa/current` — active-version symlink.
- `~/.local/share/gowa/runtime/` — databases and runtime data, mode `0700`.
- `~/.config/gowa/gowa.env` — service configuration and generated credentials, mode `0600`.
- `~/.config/systemd/user/gowa.service` — persistent user service.
- `GOWA_BASE_URL` and `GOWA_AUTH_HEADER` in the active Hermes profile's `.env`.
- `mcp_servers.gowa` in the active Hermes profile configuration.

Security-sensitive defaults are changed: loopback-only binding, Basic Auth, UI disabled, UI auto-update disabled, incoming-media auto-download disabled, presence pulses disabled, a random webhook secret, and private SQLite files.

## Architecture

```text
Hermes session
  ├─ Native MCP client ──► GOWA /mcp
  │    ├─ whatsapp_send
  │    ├─ whatsapp_message
  │    ├─ whatsapp_chat
  │    ├─ whatsapp_group
  │    ├─ whatsapp_schedule
  │    └─ whatsapp_app
  │
  └─ gowa plugin ───────► GOWA REST
       ├─ gowa_devices
       ├─ gowa_device_login
       └─ gowa_device_remove

user systemd ───────────► owns and restarts a locally installed GOWA process
                          (external GOWA remains externally managed)
```

This deliberately avoids duplicating the large MCP surface in Python.

## Pair a WhatsApp account

In a new Hermes session, ask:

> Create a GOWA device named `personal` and start QR pairing.

The plugin creates the missing device slot, requests the QR, downloads it to a private plugin-data file, and returns that local path. Scan it from WhatsApp's **Linked devices** screen. QR images and phone pairing codes are temporary credentials—do not post them publicly or save them to memory.

For phone-code pairing, provide an international number with 7–15 digits and an optional leading `+`. Before pairing—whether the slot is new or already exists empty—the plugin checks the registered devices' `phone_number` and WhatsApp JID and refuses when that number is already linked under another `device_id`. Invalid numbers are rejected before any slot is created. An existing slot that already has a session is not re-paired; use GOWA status/reconnect instead.

QR pairing cannot perform the same number check because the account is unknown until somebody scans the QR. It still refuses to start on a slot that already has a session. The phone preflight is best-effort rather than atomic: concurrent operators can race it, and GOWA remains the authority for the final pairing.

## Tool safety

### `gowa_devices`

Read-only. Device responses can contain phone/account metadata.

### `gowa_device_login`

Creates a device slot only when `create_if_missing: true`. Phone-code pairing checks for the same account number in existing device metadata first; QR creation can only check the requested slot because the scanner's number is not known yet. Pairing outputs grant access to the linked WhatsApp session and must be shown only to the requesting user.

### `gowa_device_remove`

Requires an exact `device_id` and `confirm: true`. Removal purges the locally stored session and chat data and asks WhatsApp to unlink the companion; upstream treats the remote unlink as best-effort, so the phone may still show a stale linked-device entry. There is no default-device fallback.

## Intentionally not exposed

The REST API is larger than the native MCP surface, but more tools are not automatically better. This plugin does not expose:

- Device webhook reads/writes or automatic webhook setup. Inbound WhatsApp conversations belong in Hermes' built-in WhatsApp gateway; this plugin is outbound-only.
- Passkey/WebAuthn flows, which belong in an interactive browser.
- Chat history synchronization, participant exports, newsletters, or Chatwoot administration.
- Profile/avatar/privacy mutations, presence simulation, or a generic arbitrary REST tool.

The remaining read-only candidates were also reviewed and deliberately omitted. `/user/check` duplicates GOWA's default recipient validation on every send; `/group/info-from-link` serves pre-join browsing rather than outbound messaging; `/app/info` is already consumed by setup and service verification. Add one only if a concrete workflow appears.

## Configuration

Plugin settings live under `plugins.entries.gowa.settings`:

```yaml
plugins:
  entries:
    gowa:
      settings:
        base_url: http://127.0.0.1:49152  # setup writes the selected or external URL
        timeout_seconds: 15
```

Plain HTTP and unauthenticated connections are accepted only for loopback. Remote servers must use HTTPS plus Bearer Auth. Authentication is read through Hermes' profile-aware credential chain from `GOWA_AUTH_HEADER`; it is never stored inside the plugin directory. When no auth is needed, setup removes stale GOWA Authorization configuration instead of sending an empty header.

## Verify

```bash
# Local-install mode only:
systemctl --user is-active gowa.service

# Both modes:
hermes mcp test gowa
hermes plugins doctor ~/.hermes/plugins/gowa --ci
hermes tools list
```

Expected MCP discovery: six tools. Before account pairing, `gowa_devices` should succeed with an empty list.

## Logs and troubleshooting

```bash
journalctl --user -u gowa.service -n 100 --no-pager
```

GOWA v9.5.0 prints its complete Viper settings to stdout during startup. The service intentionally sends stdout to `/dev/null` so Basic Auth and webhook secrets do not enter the journal; normal application logs on stderr remain available.

Automatic local setup selects a currently free port. If an explicit `--port` is occupied, omit it to let the kernel choose or pass another port. For an existing server, an authentication error identifies whether REST or MCP rejected the bearer. If the local service starts but WhatsApp cannot connect, inspect the journal and confirm the host can reach WhatsApp without a proxy.

## Remove

For a locally installed GOWA, first preserve the linked-device database unless you explicitly want to destroy it:

```bash
systemctl --user disable --now gowa.service
mv ~/.local/share/gowa/runtime ~/.local/share/gowa/runtime.backup
rm ~/.config/systemd/user/gowa.service
systemctl --user daemon-reload
hermes config unset mcp_servers.gowa
hermes config unset GOWA_BASE_URL
hermes config unset GOWA_AUTH_HEADER
hermes config unset plugins.entries.gowa.settings.base_url
hermes plugins remove gowa
```

For an externally managed GOWA, skip the `systemctl`, runtime, binary, and service-environment steps; only unset the Hermes configuration and remove the plugin. Delete `runtime.backup` only after deciding that the WhatsApp session keys and local chat data are no longer needed. The versioned binary and service environment can then be removed from `~/.local/lib/gowa/` and `~/.config/gowa/`.

## Security and limitations

Read [docs/SECURITY.md](docs/SECURITY.md) before linking an account.

- GOWA and `whatsmeow` are unofficial WhatsApp integrations. Meta may change the protocol or restrict an account.
- Session databases are bearer credentials: filesystem access can become account access.
- GOWA serves `/statics` before Basic Auth. Loopback-only binding contains that exposure to the host, and this plugin copies QR images into a private file rather than returning the public URL.
- Basic Auth authorizes every configured device; `device_id` is routing, not tenant isolation.
- The installer is intentionally pinned. Upgrading GOWA requires reviewing a new release and updating the version, asset, checksum, tests, and documentation here.
- Existing-server setup verifies identity and connectivity but does not pin or upgrade that external server.

## License

MIT. GOWA is a separate project distributed under its own license; this repository does not redistribute its binary.
