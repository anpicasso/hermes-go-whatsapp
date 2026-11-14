# Hermes Go WhatsApp

[![CI](https://github.com/anpicasso/hermes-go-whatsapp/actions/workflows/ci.yml/badge.svg)](https://github.com/anpicasso/hermes-go-whatsapp/actions/workflows/ci.yml)

A Hermes Agent `go-whatsapp` plugin that connects an existing [GOWA](https://github.com/aldinokemal/go-whatsapp-web-multidevice) server or installs a pinned local one, registers its native MCP endpoint, and adds the device-management operations that MCP does not expose.

## What it provides

- `hermes gowa setup`: installs and verifies **GOWA v9.9.0** on Linux x86_64, macOS x86_64/arm64, or Windows x86_64, using a kernel-selected free loopback port.
- `hermes gowa setup --base-url ...`: verifies and connects an existing GOWA without downloading or installing anything.
- For local setup, a persistent native user service bound to `127.0.0.1` with generated Basic Auth: `systemd --user`, a macOS LaunchAgent, or Windows Task Scheduler.
- Native GOWA MCP tools for sending, messages, chats, groups, schedules, session status, and newsletters.
- Three Hermes tools for the REST gaps:
  - `gowa_devices` — list or inspect device slots.
  - `gowa_device_login` — create a slot if needed, then start QR or phone-code pairing.
  - `gowa_device_remove` — purge a device and request unlinking after explicit confirmation.
- An agent-callable `gowa_watch` tool (with optional `/gowa-watch` slash command) that routes one exact GOWA webhook slot/chat into the conversation that armed it. It consumes a dedicated signed route, supports an explicit WAIT/REPORT follow-up, and never configures or replaces webhooks.

The plugin does **not** supervise the daemon from Hermes' runtime. Hermes loads plugins in CLI, gateway, cron, and worker processes; tying a long-lived server to any one of them creates duplicate-process and orphan-cleanup problems. The one-shot setup command installs it, while the operating system's native user supervisor owns its lifetime.

## Scope: outbound tools plus one opt-in inbound watch

By default this plugin is an outbound-control integration: it lets an agent use the user's linked WhatsApp account to send messages to multiple recipients and manage the GOWA device slots needed for that work. It does **not** subscribe to or ingest the account's incoming message stream unless the explicit watch below is enabled.

Webhook configuration remains intentionally not implemented. This plugin never writes GOWA webhook settings, never creates a listener, and never replaces an existing device webhook. The only inbound path is the opt-in, single-watch bridge described next, fed by a dedicated Hermes route the operator configures. General inbound WhatsApp conversations still belong in Hermes' built-in WhatsApp gateway.

## Same-session webhook watch (opt-in, WAIT/REPORT)

The watch continues the **existing Hermes conversation that armed it** instead of creating a new webhook session for every WhatsApp message. A dedicated signed Hermes route is consumed by the `pre_gateway_dispatch` hook only when its `source.chat_name` is exactly `webhook/<watch_route>`; matching events are deduplicated by `(session_id, message id)` and scheduled into the arming session with `ctx.inject_message`. No LLM turn runs for the webhook itself, no HTTP server is started, and the plugin itself has no WhatsApp send path.

### Prerequisites (operator-owned)

1. Configure a GOWA webhook target for the dedicated route. Replacing an existing device webhook requires explicit operator consent.
2. Configure the Hermes webhook route (the plugin does not write this):

```yaml
# gateway webhook route name: gowa-session-watch
# path: /webhooks/gowa-session-watch
signature: github          # GOWA signs the raw body in X-Hub-Signature-256
filters:
  field: session_id
  equals: hermes-watch-test   # the GOWA slot this route accepts
toolsets: []
deliver: log
prompt: "GOWA watch fallback: no action."   # only reached if the plugin is disabled
```

Use the route's real HMAC secret in both GOWA (`WHATSAPP_WEBHOOK_SECRET`) and the Hermes route configuration. Unsigned requests are rejected before the hook runs.

3. Enable the watch and the separate injection permission:

```bash
hermes config set plugins.entries.go-whatsapp.settings.watch_enabled true
hermes config set plugins.entries.go-whatsapp.settings.watch_route gowa-session-watch
hermes config set plugins.entries.go-whatsapp.allow_gateway_injection true
```

`allow_gateway_injection` is deliberately separate from `watch_enabled`: the watch can only schedule into an existing session after the operator grants that capability. The plugin must run in-process (`plugins.isolation: in_process`, the default); under `plugins.isolation: host` the hook receives a placeholder session store and consumes without injecting.

### Operate

Ask Hermes naturally: **“Listen to this WhatsApp conversation for an hour”** or **“Stop following that chat.”** Hermes resolves the account slot and contact, then calls `gowa_watch` with `action: start|status|stop`. Start takes `device_id` (the GOWA slot), `chat_jid`, and optional `minutes`. The destination is derived from the calling session, never supplied by the model. Listening does not authorize sending replies.

Each delivered watch message asks the **same conversation** to choose one of three outcomes:

- **WAIT** — call `gowa_watch` with `action: "wait"` and the prompt's `message_id` exactly once. This arms one follow-up five minutes after the last inbound message; a newer inbound message reschedules it. After a successful registration the model replies exactly `NO_REPLY`, which the host's native response filter suppresses.
- **REPLY** — only through the existing WhatsApp tools, and only if the user already authorized replies in this session. The plugin has no send tool.
- **REPORT** — report back in the Hermes conversation exactly as the user asked (verbatim or summary).

After a reply or report, `action: "handled"` with that `message_id` closes the token and cancels any pending follow-up. `status` reports the watch reference, generation, and the exact message id of a pending, delivered, handled, or failed follow-up.

The five-minute follow-up is delivered by one supervised gateway task started from `pre_gateway_dispatch`. The tool writes the deadline and wakes that task (starting it only when the hook's task is absent); it does not create a timer in a tool worker or a new daemon. The task is cancelled by `ctx.on_unload`, and any inbound hook can restart it after a plugin reload so an overdue persisted follow-up is delivered. If the gateway process exits and no further inbound event arrives, no follow-up can be delivered; this is a bounded best-effort seam, not a durable scheduler.

The slash commands remain optional; users do not need to type them. From the conversation that should receive the messages:

```text
/gowa-watch start <session_id> <chat_jid> [minutes]
/gowa-watch status
/gowa-watch stop
```

- `<session_id>` is the top-level GOWA **slot** id from the webhook payload (`session_id`), not `device_id` (that field is the account's own WhatsApp JID). A payload without `session_id` is ignored; the plugin does not guess.
- `<chat_jid>` must be a direct chat JID (`...@s.whatsapp.net` or `...@lid`). Groups, newsletters, and broadcasts are refused.
- `minutes` defaults to 60 and is capped at 1440 (24 hours).
- Exactly one watch exists per profile, owned by the arming conversation's session key. Another conversation is refused without leaking the watched target; after `/new` the same conversation can inspect or stop the stale pin.

Accepted payload shape ([GOWA v9.9.0 webhook docs](https://github.com/aldinokemal/go-whatsapp-web-multidevice/blob/v9.9.0/docs/webhook-payload.md)):

```json
{
  "event": "message",
  "device_id": "15550000000@s.whatsapp.net",
  "session_id": "hermes-watch-test",
  "payload": {
    "id": "3EB0...",
    "chat_id": "15551234567@s.whatsapp.net",
    "is_from_me": false,
    "body": "hello"
  }
}
```

Only `event: message`, `is_from_me: false`, an exact slot/chat match, and a non-empty text `body` are scheduled. The body is truncated at 4000 characters. Injected text has a trusted static header and metadata, carries the body as a JSON-encoded string labelled external and untrusted, and ends with the gateway's native internal-notification footer. The prompt tells the agent to treat the body as data, not instructions; this framing is not a guarantee against prompt injection.

### Limits

- The watch is a convenience bridge, not a general WhatsApp gateway. It has no media, reactions, receipts, edits, groups, or outbound send. Replies, if any, use the separately installed GOWA MCP tools under the user's existing authorization.
- Every matching text message schedules a turn; there is no batching, rate limit, or quiet hours. Arm it for a bounded trial rather than leaving it on a busy chat.
- Exactly one follow-up is pending per watch. `wait` for the same message is idempotent (it does not extend the deadline); a new inbound message reschedules the pending follow-up to itself; `stop`, re-arm, expiry, `/new`, and `handled` cancel it. A delivered or handled message cannot be re-armed, so one message cannot self-rewait indefinitely. Stale `message_id` or follow-up tokens are refused and reported.
- The follow-up task exists only in the live in-process gateway. It survives an exiting tool worker, but not a gateway process exit: after a restart, an overdue follow-up is delivered on the next inbound event that reaches `pre_gateway_dispatch`; without one it remains pending until expiry. Under `plugins.isolation: host` injection is unavailable, so the watch consumes without delivering.
- The session id is captured at arming and rechecked against the live `SessionStore` before every delivery, including the follow-up. `/new` (or any session-id change) stops the watch instead of silently delivering into a different transcript. The check and the host's scheduling are separate steps, so a `/new` racing the schedule can still land in the new transcript; this is best-effort pinning, not a lock.
- Dedupe uses a SQLite claim keyed by `(session_id, message id)` and is retained for 24 hours. A failed schedule releases the claim; a rejected message is never marked scheduled.
- `ctx.inject_message` returning `True` means the host accepted the message for **scheduling**, not that a turn ran or a reply was delivered. Delivery is not transactional or exactly-once: a crash after scheduling can lose one message, and a crash after a claim but before scheduling delays retry by up to 5 minutes.
- A follow-up injection refusal is retried at most three times, one minute apart, then reported as `follow-up FAILED` in status. `wait` returns `ok: false` when no live supervisor can be started; it never silently arms an undeliverable deadline.
- GOWA sees HTTP 202 when the route is consumed even if injection was refused. `202` means "not sent to a fallback webhook LLM", not "delivered to the session"; check `/gowa-watch status` and the gateway log.
- Expiry is checked lazily on the next matching event, status, command, or supervisor wake. There is no persistent queue and no new background daemon.

### Verify

```bash
python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate .
```

The test suite shortens `FOLLOWUP_SECONDS` to sub-second values and also advances a fake clock; production remains 300 seconds. The host-dispatcher tests run when Hermes is importable (`PYTHONPATH` pointing at the installed source) and skip otherwise.

## Requirements

- For local installation: Linux x86_64 with a user `systemd` manager; macOS x86_64/arm64 in a logged-in GUI session; or Windows 10/11 x86_64 in a logged-in interactive session.
- Hermes Agent 0.23.1 or newer.
- A WhatsApp account able to link another device.
- Network access to WhatsApp; local installation also needs GitHub Releases access.

## Install

```bash
hermes plugins install anpicasso/hermes-go-whatsapp --enable
hermes gowa setup
```

Setup is idempotent: rerunning it reinstalls the same verified release, preserves existing generated credentials, updates configuration, and restarts the service.

Upgrading from plugin ID `gowa` (v0.4.0 or earlier) is a one-time rename:

```bash
hermes plugins disable gowa
hermes plugins remove gowa
hermes plugins install anpicasso/hermes-go-whatsapp --enable
hermes gowa setup
```

The `gowa` CLI command, MCP server name, `GOWA_*` credentials, and native service names intentionally remain unchanged; only the Hermes plugin ID becomes `go-whatsapp`.

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

## Upgrading to GOWA v9.9.0

Back up the runtime directory and service environment before running setup. Existing local credentials and the selected loopback port are preserved.

GOWA v9.8+ rejects private-address media and link-preview URL fetches. A temporary localhost file server is no longer a supported attachment source; use a reachable public HTTPS URL or the upstream authenticated multipart REST upload path. Do not disable the upstream URL guard. The native MCP catalog now includes `whatsapp_newsletter` (seven tools total). See the [upstream upgrade notes](https://github.com/aldinokemal/go-whatsapp-web-multidevice/releases/tag/v9.8.0) for remote authentication, trusted browser origins, and TLS changes.

## What setup changes

In existing-server mode, setup changes only the active Hermes profile: `GOWA_BASE_URL`, optional `GOWA_AUTH_HEADER`, `mcp_servers.gowa`, and `plugins.entries.go-whatsapp.settings.base_url`. It does not download GOWA, write native service files, or manage that server's lifetime.

In local-install mode, the installer chooses one hard-coded release asset for the current supported platform and verifies its SHA-256 before reading the exact ZIP member:

- Linux x86_64: `whatsapp_9.9.0_linux_amd64.zip` / `linux-amd64` / `b3a0afc3cd2721198de60032a21f7f0aa78e58d0a478a7b00566d1604b7aa574`
- macOS x86_64: `whatsapp_9.9.0_darwin_amd64.zip` / `darwin-amd64` / `ead32e63f9338019f315d6e70159b44154bc6fc75637faa2c4deae2b90e38398`
- macOS arm64: `whatsapp_9.9.0_darwin_arm64.zip` / `darwin-arm64` / `ac1d77adb15a31de558b8dca7871d0fbc7d05fdaab82ed49d9de357cdfe0e1b6`
- Windows x86_64: `whatsapp_9.9.0_windows_amd64.zip` / `windows-amd64.exe` / `51fa01ee9379cb8201c4ae07d54fca1b05aee0770a20de91773255dbd3913b69`

Linux and macOS use a versioned binary plus an atomic `current` symlink under `~/.local/lib/gowa/`. Windows writes the versioned executable under `%LOCALAPPDATA%\gowa\lib\v9.9.0\`; the scheduled-task wrapper names that exact path so updates never overwrite a running executable.

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
    <string>&lt;HOME&gt;/.local/lib/gowa/v9.9.0/whatsapp</string><string>rest</string>
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

Setup uses modern domain-aware commands: `launchctl bootout gui/$UID/com.hermes.gowa` when loaded, waits for the old port to close, then runs `launchctl enable gui/$UID/com.hermes.gowa` and `launchctl bootstrap gui/$UID <absolute-plist>`. `RunAtLoad` starts it immediately; `KeepAlive.SuccessfulExit=false` matches systemd's restart-on-failure behavior. The `.env` remains in the private working directory, never in the plist or process arguments. Stdout remains discarded as defense in depth; GOWA v9.9.0 also redacts secrets from its startup settings dump.

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
  │    ├─ whatsapp_app
  │    └─ whatsapp_newsletter
  │
  └─ go-whatsapp plugin ─► GOWA REST
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

- Device webhook reads/writes or automatic webhook setup. The only inbound path is the explicit, opt-in same-session watch above; it never configures GOWA and never replaces an existing device webhook. General inbound WhatsApp conversations remain out of scope.
- Passkey/WebAuthn flows, which belong in an interactive browser.
- Extra REST wrappers for chat history synchronization, participant exports, newsletters, or Chatwoot administration. GOWA v9.9.0 itself exposes newsletter operations through native MCP.
- Profile/avatar/privacy mutations, presence simulation, or a generic arbitrary REST tool.

The remaining read-only candidates were also reviewed and deliberately omitted. `/user/check` duplicates GOWA's default recipient validation on every send; `/group/info-from-link` serves pre-join browsing rather than outbound messaging; `/app/info` is already consumed by setup and service verification. Add one only if a concrete workflow appears.

## Configuration

Plugin settings live under `plugins.entries.go-whatsapp.settings`:

```yaml
plugins:
  entries:
    go-whatsapp:
      settings:
        base_url: http://127.0.0.1:49152  # setup writes the selected or external URL
        timeout_seconds: 15
        watch_enabled: false               # opt-in; see the same-session watch section
        watch_route: gowa-session-watch    # exact route name; no other route is consumed
      allow_gateway_injection: false       # separate permission required to schedule into a session
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
hermes plugins doctor ~/.hermes/plugins/go-whatsapp --ci
hermes tools list
```

Expected MCP discovery: seven tools. Before account pairing, `gowa_devices` should succeed with an empty list. Setup itself is stricter than supervisor status: it sends Basic Auth to `/app/info` and requires the response version to equal `v9.9.0`.

## Logs and troubleshooting

- Linux: `journalctl --user -u gowa.service -n 100 --no-pager`
- macOS: `tail -n 100 ~/.local/share/gowa/runtime/logs/gowa.err.log`
- Windows PowerShell: `Get-Content "$env:LOCALAPPDATA\gowa\runtime\logs\gowa.err.log" -Tail 100`

GOWA v9.9.0 redacts sensitive values in its startup settings dump. Every native service still discards stdout as defense in depth; stderr remains available through the platform-specific path above.

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
hermes config unset plugins.entries.go-whatsapp.settings.base_url
hermes config unset plugins.entries.gowa.settings.base_url  # stale v0.4.0-and-earlier key, if present
hermes plugins remove go-whatsapp
```

For an externally managed GOWA, skip all supervisor, runtime, binary, and service-environment steps. Delete the runtime backup only after deciding that the WhatsApp session keys and local chat data are no longer needed.

## Security and limitations

Read [docs/SECURITY.md](docs/SECURITY.md) before linking an account.

- GOWA and `whatsmeow` are unofficial WhatsApp integrations. Meta may change the protocol or restrict an account.
- Session databases are bearer credentials: filesystem access can become account access.
- The opt-in watch trusts the dedicated Hermes route only after Hermes validates its HMAC signature. It logs no sender identity or message body, injects a trusted header with a JSON-encoded untrusted body plus the native internal-notification footer, and has no outbound send path. `ctx.inject_message` acceptance means scheduling only, not delivery; dedupe is not a transactional exactly-once guarantee. A `wait` follow-up is one supervised gateway task, not a durable scheduler; after a gateway process exit it needs a later inbound event to recover.
- One watch exists per profile, owned by the arming session. Changing that session's id (`/new`) stops delivery rather than retargeting the new transcript. A pending follow-up carries the watch generation and exact message id, so stale `handled`/`wait` calls are refused rather than cancelling a newer follow-up.
- GOWA serves `/statics` before Basic Auth. Loopback-only binding contains that exposure to the host, and this plugin copies QR images into a private file rather than returning the public URL.
- Basic Auth authorizes every configured device; `device_id` is routing, not tenant isolation.
- macOS LaunchAgents exist only while that user's GUI login domain exists. A pre-login or headless daemon requires an administrator-installed LaunchDaemon and is intentionally out of scope.
- The Windows task uses `InteractiveToken`, so it also runs only while the user is logged in. A real boot-time Windows Service requires administrator rights. Runtime recovery can take up to one minute, and Task Scheduler `/End` is a forced stop rather than GOWA's graceful POSIX `SIGTERM` path.
- macOS stderr is not rotated; Windows keeps only the latest run's stderr. Add log rotation only if those logs become operationally noisy.
- The installer is intentionally pinned. Upgrading GOWA requires reviewing a new release and updating the version, asset, checksum, tests, and documentation here.
- Existing-server setup verifies identity and connectivity but does not pin or upgrade that external server.

## License

MIT. GOWA is a separate project distributed under its own license; this repository does not redistribute its binary.
