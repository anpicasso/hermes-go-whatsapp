# Security model

This plugin controls a local process that holds WhatsApp linked-device keys. Treat its runtime directory and pairing material as credentials.

## Trust boundaries

- **Hermes → GOWA:** authenticated HTTP on loopback. The generated Authorization value lives in the active Hermes profile `.env` and is referenced from MCP configuration through `${GOWA_AUTH_HEADER}`.
- **GOWA → WhatsApp:** an unofficial `whatsmeow` session. It is not operated, supported, or guaranteed by Meta.
- **Filesystem:** `whatsapp.db` contains device/session cryptographic material; `chatstorage.db` can contain message metadata and content.
- **Pairing:** a QR image or phone code authorizes a new linked device. Anyone who obtains it before expiry may link the account.

## Installer controls

`hermes gowa setup`:

1. Downloads one hard-coded GitHub release asset over HTTPS.
2. Rejects archives larger than 64 MiB.
3. Verifies the archive against the pinned SHA-256 before reading it.
4. Reads only the exact `linux-amd64` ZIP member; it never extracts paths from the archive.
5. Installs the binary atomically into a versioned directory.
6. Generates random Basic Auth and webhook secrets on first setup and preserves them on rerun.
7. Binds GOWA to `127.0.0.1` and disables the downloaded web UI, media auto-download, and presence pulses.
8. Creates runtime directories as `0700` and secret/database files under a `0077` service umask.
9. Suppresses startup stdout because upstream prints all Viper settings, including secrets.

## Known upstream hazards

- `/statics` is mounted before global Basic Auth. Do not expose port 3000 beyond loopback without an authenticated reverse proxy that also protects static paths.
- GOWA has no per-device authorization scopes. Any valid server credential can operate on every device in that instance.
- Device webhook configuration can forward message events to arbitrary URLs and its read endpoint returns the webhook secret. This plugin exposes neither operation.
- The REST/MCP API includes destructive actions such as logout, message deletion, group changes, and device purge. Hermes tool descriptions and confirmation gates reduce accidental use but are not a sandbox.
- SQLite data is not encrypted by this project. Host access controls and backups determine confidentiality.

## Tool policy

- `gowa_devices` is read-only but may return personal metadata.
- `gowa_device_login` is permitted only for an explicit user pairing request. It replaces the unauthenticated static QR URL with a private local file path.
- `gowa_device_remove` requires an exact identifier and `confirm: true` after explicit approval.
- There is no generic REST passthrough, webhook tool, passkey tool, profile mutation tool, or Chatwoot administration tool.

## Remote deployments

The plugin refuses plain HTTP for non-loopback hosts. If you point `base_url` at another machine:

- terminate TLS with a valid certificate;
- protect `/statics` as well as REST and MCP;
- use a dedicated GOWA instance for each authorization boundary;
- keep credentials out of repository files and shell history;
- set resource limits and rate limiting at the reverse proxy.

## Reporting

Open a GitHub security advisory for vulnerabilities in this plugin. Report vulnerabilities in GOWA or `whatsmeow` to their respective maintainers. Do not include live credentials, QR images, phone numbers, or session databases in an issue.
