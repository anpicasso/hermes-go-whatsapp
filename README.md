# Hermes Go WhatsApp

[![CI](https://github.com/anpicasso/hermes-go-whatsapp/actions/workflows/ci.yml/badge.svg)](https://github.com/anpicasso/hermes-go-whatsapp/actions/workflows/ci.yml)

A Hermes Agent plugin that installs a pinned local [GOWA](https://github.com/aldinokemal/go-whatsapp-web-multidevice) server, connects its native MCP endpoint, and adds the device-management operations that MCP does not expose.

## What it provides

- `hermes gowa setup`: installs and verifies **GOWA v9.5.0** on Linux x86_64.
- A persistent user `systemd` service bound to `127.0.0.1` with generated Basic Auth.
- Native GOWA MCP tools for sending, messages, chats, groups, schedules, and session status.
- Three Hermes tools for the REST gaps:
  - `gowa_devices` — list or inspect device slots.
  - `gowa_device_login` — create a slot if needed, then start QR or phone-code pairing.
  - `gowa_device_remove` — permanently unlink and purge a device after explicit confirmation.

The plugin does **not** supervise the daemon from Hermes' runtime. Hermes loads plugins in CLI, gateway, cron, and worker processes; tying a long-lived server to any one of them creates duplicate-process and orphan-cleanup problems. The one-shot setup command installs it, while `systemd` owns its lifetime.

## Requirements

- Linux x86_64 with a working user `systemd` manager.
- Hermes Agent 0.21.5 or newer.
- A WhatsApp account able to link another device.
- Network access to GitHub Releases and WhatsApp.

## Install

```bash
hermes plugins install anpicasso/hermes-go-whatsapp --enable
hermes gowa setup
```

Setup is idempotent: rerunning it reinstalls the same verified release, preserves existing generated credentials, updates configuration, and restarts the service.

To use another loopback port:

```bash
hermes gowa setup --port 3456
```

Start a new Hermes session after setup so its tool catalog includes the plugin and MCP tools.

## What setup changes

The installer downloads exactly:

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

user systemd ───────────► owns and restarts the GOWA process
```

This deliberately avoids duplicating the large MCP surface in Python.

## Pair a WhatsApp account

In a new Hermes session, ask:

> Create a GOWA device named `personal` and start QR pairing.

The plugin creates the missing device slot, requests the QR, downloads it to a private plugin-data file, and returns that local path. Scan it from WhatsApp's **Linked devices** screen. QR images and phone pairing codes are temporary credentials—do not post them publicly or save them to memory.

For phone-code pairing, provide an international number with 7–15 digits and an optional leading `+`.

## Tool safety

### `gowa_devices`

Read-only. Device responses can contain phone/account metadata.

### `gowa_device_login`

Creates a device slot only when `create_if_missing: true`. Pairing outputs grant access to the linked WhatsApp session and must be shown only to the requesting user.

### `gowa_device_remove`

Requires an exact `device_id` and `confirm: true`. Removal unlinks the account and purges the device's stored session and chat data. There is no default-device fallback.

## Intentionally not exposed

The REST API is larger than the native MCP surface, but more tools are not automatically better. This plugin does not expose:

- Device webhook reads/writes, which can reveal secrets or exfiltrate messages.
- Passkey/WebAuthn flows, which belong in an interactive browser.
- Chat history synchronization, participant exports, newsletters, or Chatwoot administration.
- Profile/avatar/privacy mutations, presence simulation, or a generic arbitrary REST tool.

Possible future additions, if a real workflow requires them, are read-only number validation and group-link inspection.

## Configuration

Plugin settings live under `plugins.entries.gowa.settings`:

```yaml
plugins:
  entries:
    gowa:
      settings:
        base_url: http://127.0.0.1:3000
        timeout_seconds: 15
```

Plain HTTP is accepted only for loopback. Remote servers must use HTTPS. Authentication is read through Hermes' profile-aware credential chain from `GOWA_AUTH_HEADER`; it is never stored inside the plugin directory.

## Verify

```bash
systemctl --user is-active gowa.service
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

If setup reports an occupied port, rerun it with a free `--port`. If the service starts but WhatsApp cannot connect, inspect the journal and confirm the host can reach WhatsApp without a proxy.

## Remove

First preserve the linked-device database unless you explicitly want to destroy it:

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

Delete `runtime.backup` only after deciding that the WhatsApp session keys and local chat data are no longer needed. The versioned binary and service environment can then be removed from `~/.local/lib/gowa/` and `~/.config/gowa/`.

## Security and limitations

Read [docs/SECURITY.md](docs/SECURITY.md) before linking an account.

- GOWA and `whatsmeow` are unofficial WhatsApp integrations. Meta may change the protocol or restrict an account.
- Session databases are bearer credentials: filesystem access can become account access.
- GOWA serves `/statics` before Basic Auth. Loopback-only binding contains that exposure to the host, and this plugin copies QR images into a private file rather than returning the public URL.
- Basic Auth authorizes every configured device; `device_id` is routing, not tenant isolation.
- The installer is intentionally pinned. Upgrading GOWA requires reviewing a new release and updating the version, asset, checksum, tests, and documentation here.

## License

MIT. GOWA is a separate project distributed under its own license; this repository does not redistribute its binary.
