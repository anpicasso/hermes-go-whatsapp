# Security model

This plugin connects to a GOWA process that holds WhatsApp linked-device keys. It can install that process locally or use an existing server. Treat its runtime directory, access token, and pairing material as credentials.

## Trust boundaries

- **Hermes → local GOWA:** authenticated HTTP on loopback. The generated Authorization value lives in the active Hermes profile `.env` and is referenced from MCP configuration through `${GOWA_AUTH_HEADER}`.
- **Hermes → existing GOWA:** HTTPS plus Bearer Auth when remote; unauthenticated HTTP is allowed only on loopback. Setup verifies both REST and MCP before saving the connection.
- **GOWA → WhatsApp:** an unofficial `whatsmeow` session. It is not operated, supported, or guaranteed by Meta.
- **Filesystem:** `whatsapp.db` contains device/session cryptographic material; `chatstorage.db` can contain message metadata and content.
- **Pairing:** a QR image or phone code authorizes a new linked device. Anyone who obtains it before expiry may link the account.

## Installer controls

Local `hermes gowa setup`:

1. Selects one hard-coded GOWA v9.5.0 release asset for Linux x86_64, macOS x86_64/arm64, or Windows x86_64 and downloads it over HTTPS.
2. Rejects archives larger than 64 MiB.
3. Verifies the platform-specific pinned SHA-256 before reading it.
4. Reads only the exact platform binary member; it never extracts paths from the archive.
5. Installs the binary atomically into a versioned directory; Linux/macOS switch an atomic `current` symlink, while Windows points its wrapper at the exact versioned executable.
6. Generates random Basic Auth and webhook secrets on first setup and preserves them on rerun.
7. Uses a kernel-selected free port on `127.0.0.1` and disables the downloaded web UI, media auto-download, and presence pulses.
8. Stores secrets outside process arguments: Linux uses a mode-`0600` environment file; macOS uses a private working-directory `.env`; Windows uses a `.env` under an `icacls` tree limited to the current user SID and `SYSTEM`. Hermes keeps the derived Authorization header in the active profile credential environment.
9. Uses a `0077` supervisor umask on Linux/macOS and private runtime directories. SQLite remains unencrypted at rest.
10. Suppresses startup stdout on every platform because upstream prints all Viper settings, including secrets.
11. Restarts with the native user supervisor: systemd on failure, launchd on unsuccessful exit, and a one-minute Task Scheduler repetition watchdog with `IgnoreNew` on Windows.

Existing-server `hermes gowa setup --base-url ...` downloads nothing and creates no service. It rejects redirects, validates URL/auth inputs, caps probe responses at 1 MiB, and requires `/app/info`, `/devices`, and MCP `initialize` to identify a working GOWA connection before changing Hermes configuration. A remote URL must use HTTPS and `--auth-bearer`.

The macOS LaunchAgent and Windows `InteractiveToken` task deliberately run only while the user is logged in; boot-time/headless service requires an administrator-managed LaunchDaemon or Windows Service. Windows crash recovery can take up to one minute, and `schtasks /End` is forceful rather than a graceful POSIX signal. macOS stderr is not rotated; Windows overwrites its stderr log on each run.

## Known upstream hazards

- `/statics` is mounted before global Basic Auth. Do not expose a GOWA port beyond loopback without an authenticated reverse proxy that also protects static paths.
- GOWA has no per-device authorization scopes. Any valid server credential can operate on every device in that instance.
- Device webhook configuration can forward message events to arbitrary URLs and its read endpoint returns the webhook secret. This plugin exposes neither operation.
- The REST/MCP API includes destructive actions such as logout, message deletion, group changes, and device purge. Hermes tool descriptions and confirmation gates reduce accidental use but are not a sandbox.
- SQLite data is not encrypted by this project. Host access controls and backups determine confidentiality.

## Tool policy

- `gowa_devices` is read-only but may return personal metadata.
- `gowa_device_login` is permitted only for an explicit user pairing request. It validates phone-code input before creating or pairing a slot, rejects numbers already present in another device's metadata, and refuses to re-pair a slot that already has a session. QR pairing cannot deduplicate by number before the scan because the account is not known yet. The preflight is not an atomic server-side uniqueness constraint, so concurrent operators can still race it. The tool replaces the unauthenticated static QR URL with a private local file path.
- `gowa_device_remove` requires an exact identifier and `confirm: true` after explicit approval. Local purge errors fail closed; GOWA's remote WhatsApp unlink is best-effort.
- There is no generic REST passthrough, webhook tool, passkey tool, profile mutation tool, or Chatwoot administration tool.

## Remote deployments

The plugin refuses plain HTTP for non-loopback hosts. If you point `base_url` at another machine:

- terminate TLS with a valid certificate;
- require Bearer Auth and make the same token authorize REST and MCP; GOWA's native OAuth bearer alone covers MCP, not its Basic-protected REST routes;
- protect `/statics` as well as REST and MCP;
- use a dedicated GOWA instance for each authorization boundary;
- keep credentials out of repository files and shell history; note that a literal `--auth-bearer TOKEN` may be briefly visible in process arguments, so expand it from a short-lived environment variable and clear that variable afterward;
- set resource limits and rate limiting at the reverse proxy.

## Reporting

Open a GitHub security advisory for vulnerabilities in this plugin. Report vulnerabilities in GOWA or `whatsmeow` to their respective maintainers. Do not include live credentials, QR images, phone numbers, or session databases in an issue.
