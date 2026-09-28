# Hermes Go WhatsApp

[![CI](https://github.com/anpicasso/hermes-go-whatsapp/actions/workflows/ci.yml/badge.svg)](https://github.com/anpicasso/hermes-go-whatsapp/actions/workflows/ci.yml)

A Hermes Agent plugin that connects an existing [GOWA](https://github.com/aldinokemal/go-whatsapp-web-multidevice) server or installs a pinned local one, registers its native MCP endpoint, and adds the device-management operations that MCP does not expose.

## What it provides

- `hermes gowa setup`: installs and verifies **GOWA v9.5.0** on Linux x86_64, macOS x86_64/arm64, or Windows x86_64, using a kernel-selected free loopback port.
- `hermes gowa setup --base-url ...`: verifies and connects an existing GOWA without downloading or installing anything.
- For local setup, a persistent native user service bound to `127.0.0.1` with generated Basic Auth: `systemd --user`, a macOS LaunchAgent, or Windows Task Scheduler.
- Native GOWA MCP tools for sending, messages, chats, groups, schedules, and session status.
- Three Hermes tools for the REST gaps:
  - `gowa_devices` — list or inspect device slots.
  - `gowa_device_login` — create a slot if needed, then start QR or phone-code pairing.
  - `gowa_device_remove` — purge a device and request unlinking after explicit confirmation.

The plugin does **not** supervise the daemon from Hermes' runtime. Hermes loads plugins in CLI, gateway, cron, and worker processes; tying a long-lived server to any one of them creates duplicate-process and orphan-cleanup problems. The one-shot setup command installs it, while the operating system's native user supervisor owns its lifetime.

## Scope: outbound agent messaging

This plugin is deliberately an outbound-control integration: it lets an agent use the user's linked WhatsApp account to send messages to multiple recipients and manage the GOWA device slots needed for that work. It does **not** subscribe to or ingest the account's incoming message stream.

Webhook configuration is intentionally not implemented. Once inbound WhatsApp messages should become Hermes conversations, use Hermes' built-in WhatsApp gateway instead; duplicating it through GOWA webhooks would add a second listener and an unnecessary prompt-injection surface outside this plugin's purpose.

## Requirements

- For local installation: Linux x86_64 with a user `systemd` manager; macOS x86_64/arm64 in a logged-in GUI session; or Windows 10/11 x86_64 in a logged-in interactive session.
- Hermes Agent 0.21.5 or newer.
- A WhatsApp account able to link another device.
- Network access to WhatsApp; local installation also needs GitHub Releases access.

## Install

```bash
hermes plugins install anpicasso/hermes-go-whatsapp --enable
hermes gowa setup
```

Setup is idempotent: rerunning it reinstalls the same verified release, preserves existing generated credentials, updates configuration, and restarts the service.

Setup adds `mcp_servers.gowa` to Hermes configuration, so reload MCP connections after it finishes. Run `/reload-mcp` in the current Hermes session, or restart the gateway from an external shell with `hermes gateway restart`. Then start a new Hermes session so its tool catalog includes both the native GOWA MCP tools and the plugin's device tools.

The first local setup asks the kernel for an available ephemeral loopback port; it does not guess random ports or maintain a pool. The selected port is persisted in the platform's private GOWA environment file and reused on later runs. An explicit occupied `--port` fails before download. There is an unavoidable tiny bind/release/start race, so setup also verifies the authenticated `/app/info` response and exact pinned version after startup; it fails rather than silently connecting to the wrong process.

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

## What setup changes

In existing-server mode, setup changes only the active Hermes profile: `GOWA_BASE_URL`, optional `GOWA_AUTH_HEADER`, `mcp_servers.gowa`, and `plugins.entries.gowa.settings.base_url`. It does not download GOWA, write native service files, or manage that server's lifetime.

In local-install mode, the installer chooses one hard-coded release asset for the current supported platform and verifies its SHA-256 before reading the exact ZIP member:

- Linux x86_64: `whatsapp_9.5.0_linux_amd64.zip` / `linux-amd64` / `850a109a5127339adafeca3bd55be0bf5be5a5a3a0e7e2ffdd223536d312138c`
- macOS x86_64: `whatsapp_9.5.0_darwin_amd64.zip` / `darwin-amd64` / `b57d6fa46bbef88fb3dd1708174d4e42cdcae7dea70250961a3f70f7c06e207b`
- macOS arm64: `whatsapp_9.5.0_darwin_arm64.zip` / `darwin-arm64` / `0a5639e0608aaae3e1c7977a16303b782c2ea3ff7be8f73ecf0bed89adf7a444`
- Windows x86_64: `whatsapp_9.5.0_windows_amd64.zip` / `windows-amd64.exe` / `611e66c5751657980b12a5a216af466f19548cba89e66b0b5a1c353e5a0ee825`

Linux and macOS use a versioned binary plus an atomic `current` symlink under `~/.local/lib/gowa/`. Windows writes the versioned executable under `%LOCALAPPDATA%\gowa\lib\v9.5.0\`; the scheduled-task wrapper names that exact path so updates never overwrite a running executable.

Runtime and service files:

- Linux: `~/.local/share/gowa/runtime/`, `~/.config/gowa/gowa.env`, and `~/.config/systemd/user/gowa.service`.
- macOS: `~/.local/share/gowa/runtime/`, its private `.env`, and `~/Library/LaunchAgents/com.hermes.gowa.plist`.
- Windows: `%LOCALAPPDATA%\gowa\runtime\`, its private `.env`, `%LOCALAPPDATA%\gowa\run.ps1`, and `%LOCALAPPDATA%\gowa\gowa-task.xml`.
- Every platform stores `GOWA_BASE_URL` and `GOWA_AUTH_HEADER` in the active Hermes profile credential environment and references the latter as `${GOWA_AUTH_HEADER}` from MCP configuration.

Security-sensitive defaults are changed: loopback-only binding, Basic Auth, UI disabled, UI auto-update disabled, incoming-media auto-download disabled, presence pulses disabled, a random webhook secret, and private SQLite files.

### Native service implementation

**Linux:** the existing `systemd --user` unit uses `Restart=on-failure`, `RestartSec=5`, `TimeoutStopSec=20`, `UMask=0077`, and systemd sandboxing. Setup runs `daemon-reload`, `enable`, `restart`, and `is-active`.

**macOS:** setup writes this LaunchAgent with every `<HOME>` placeholder replaced by an absolute path; `launchd` does not expand `~` or shell variables:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.hermes.gowa</string>
  <key>ProgramArguments</key><array>
    <string>&lt;HOME&gt;/.local/lib/gowa/v9.5.0/whatsapp</string><string>rest</string>
  </array>
  <key>WorkingDirectory</key><string>&lt;HOME&gt;/.local/share/gowa/runtime</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ExitTimeOut</key><integer>20</integer>
  <key>Umask</key><integer>63</integer>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>&lt;HOME&gt;/.local/share/gowa/runtime/logs/gowa.err.log</string>
</dict></plist>
```

Setup uses modern domain-aware commands: `launchctl bootout gui/$UID/com.hermes.gowa` when loaded, waits for the old port to close, then runs `launchctl enable gui/$UID/com.hermes.gowa` and `launchctl bootstrap gui/$UID <absolute-plist>`. `RunAtLoad` starts it immediately; `KeepAlive.SuccessfulExit=false` matches systemd's restart-on-failure behavior. The `.env` remains in the private working directory, never in the plist or process arguments. Stdout is discarded because GOWA v9.5.0 prints all Viper settings, including secrets, at startup.

**Windows:** there is no non-administrator equivalent of a boot-time Windows Service. Setup therefore creates the current user's `Hermes GOWA` Task Scheduler 2.0 task using UTF-16 XML and `schtasks.exe /Create /XML`. Its exact security/lifetime settings are:

```xml
<LogonTrigger><Enabled>true</Enabled><UserId>DOMAIN\User</UserId></LogonTrigger>
<TimeTrigger>
  <Enabled>true</Enabled><StartBoundary>INSTALL-TIME</StartBoundary>
  <Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
</TimeTrigger>
<Principal id="Author">
  <UserId>DOMAIN\User</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>
</Principal>
<Settings>
  <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
  <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
  <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
  <StartWhenAvailable>true</StartWhenAvailable>
  <AllowStartOnDemand>true</AllowStartOnDemand>
  <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
</Settings>
```

The task runs built-in Windows PowerShell hidden and non-interactively. A fixed `run.ps1` changes to the runtime directory, invokes the exact versioned `whatsapp.exe rest`, discards stdout, writes only the latest stderr log, waits for the child, and returns its exit code. No Basic credential appears in the task XML, script, or any command line. `InteractiveToken` deliberately avoids password storage and retains network access; Microsoft's passwordless `S4U` mode cannot access the network or encrypted files. The one-minute indefinite trigger is the crash watchdog—Task Scheduler does not reliably treat a successfully launched program's nonzero exit code as a restartable task failure—and `IgnoreNew` prevents duplicates while GOWA is alive. Setup ends the previous task, waits for its port to close, replaces the task, starts it with `schtasks.exe /Run`, and verifies authenticated readiness. `icacls` removes inherited access from the GOWA tree and grants full control only to the current user SID and `SYSTEM`.

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

native user supervisor ──► owns and restarts a locally installed GOWA process
                          (systemd, launchd, or Task Scheduler; external GOWA remains externally managed)
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

Linux:

```bash
systemctl --user is-active gowa.service
```

macOS:

```bash
launchctl print "gui/$(id -u)/com.hermes.gowa"
```

Windows PowerShell:

```powershell
schtasks.exe /Query /TN "Hermes GOWA"
```

Every platform then uses the same application checks:

```bash
hermes mcp test gowa
hermes plugins doctor ~/.hermes/plugins/gowa --ci
hermes tools list
```

Expected MCP discovery: six tools. Before account pairing, `gowa_devices` should succeed with an empty list. Setup itself is stricter than supervisor status: it sends Basic Auth to `/app/info` and requires the response version to equal `v9.5.0`.

## Logs and troubleshooting

- Linux: `journalctl --user -u gowa.service -n 100 --no-pager`
- macOS: `tail -n 100 ~/.local/share/gowa/runtime/logs/gowa.err.log`
- Windows PowerShell: `Get-Content "$env:LOCALAPPDATA\gowa\runtime\logs\gowa.err.log" -Tail 100`

GOWA v9.5.0 prints its complete Viper settings to stdout during startup. Every native service intentionally discards stdout so Basic Auth and webhook secrets do not enter logs; stderr remains available through the platform-specific path above.

Automatic local setup selects a currently free port. If an explicit `--port` is occupied, omit it to let the kernel choose or pass another port. For an existing server, an authentication error identifies whether REST or MCP rejected the bearer. If the local service starts but WhatsApp cannot connect, inspect stderr and confirm the host can reach WhatsApp without a proxy.

## Remove

First preserve the linked-device database unless you explicitly want to destroy it.

Linux:

```bash
systemctl --user disable --now gowa.service
mv ~/.local/share/gowa/runtime ~/.local/share/gowa/runtime.backup
rm ~/.config/systemd/user/gowa.service
systemctl --user daemon-reload
```

macOS:

```bash
launchctl bootout "gui/$(id -u)/com.hermes.gowa"
launchctl disable "gui/$(id -u)/com.hermes.gowa"
mv ~/.local/share/gowa/runtime ~/.local/share/gowa/runtime.backup
rm ~/Library/LaunchAgents/com.hermes.gowa.plist
```

Windows PowerShell:

```powershell
schtasks.exe /End /TN "Hermes GOWA"
schtasks.exe /Delete /TN "Hermes GOWA" /F
Move-Item "$env:LOCALAPPDATA\gowa\runtime" "$env:LOCALAPPDATA\gowa\runtime.backup"
```

Then remove the common Hermes configuration and plugin:

```bash
hermes config unset mcp_servers.gowa
hermes config unset GOWA_BASE_URL
hermes config unset GOWA_AUTH_HEADER
hermes config unset plugins.entries.gowa.settings.base_url
hermes plugins remove gowa
```

For an externally managed GOWA, skip all supervisor, runtime, binary, and service-environment steps. Delete the runtime backup only after deciding that the WhatsApp session keys and local chat data are no longer needed.

## Security and limitations

Read [docs/SECURITY.md](docs/SECURITY.md) before linking an account.

- GOWA and `whatsmeow` are unofficial WhatsApp integrations. Meta may change the protocol or restrict an account.
- Session databases are bearer credentials: filesystem access can become account access.
- GOWA serves `/statics` before Basic Auth. Loopback-only binding contains that exposure to the host, and this plugin copies QR images into a private file rather than returning the public URL.
- Basic Auth authorizes every configured device; `device_id` is routing, not tenant isolation.
- macOS LaunchAgents exist only while that user's GUI login domain exists. A pre-login or headless daemon requires an administrator-installed LaunchDaemon and is intentionally out of scope.
- The Windows task uses `InteractiveToken`, so it also runs only while the user is logged in. A real boot-time Windows Service requires administrator rights. Runtime recovery can take up to one minute, and Task Scheduler `/End` is a forced stop rather than GOWA's graceful POSIX `SIGTERM` path.
- macOS stderr is not rotated; Windows keeps only the latest run's stderr. Add log rotation only if those logs become operationally noisy.
- The installer is intentionally pinned. Upgrading GOWA requires reviewing a new release and updating the version, asset, checksum, tests, and documentation here.
- Existing-server setup verifies identity and connectivity but does not pin or upgrade that external server.

## License

MIT. GOWA is a separate project distributed under its own license; this repository does not redistribute its binary.
