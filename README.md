<picture>
  <source media="(prefers-color-scheme: dark)" srcset="app/static/logo-white.svg">
  <img src="app/static/logo-black.svg" alt="Service Sentinel" width="120">
</picture>

# Service Sentinel

Service Sentinel watches your homelab's Docker containers and their compose files, and tells
you three things: what a pending update actually changes for your setup, whether anything in
your logs is genuinely broken, and whether your compose configuration has real security or
reliability issues.

Three independent features, each off by default. Nothing runs and no AI tokens are spent until
you enable a feature's schedule in Settings (or trigger a check yourself from the Overview
page).

## What it does

**Updates.** Checks your running containers against their registries on a schedule. When an
image has genuinely changed, your configured AI provider summarizes what's new and what might
break, checked against your own compose configuration rather than a generic changelog.

**Runtime health.** Pulls each container's recent logs on a schedule and filters them locally
down to lines that actually look suspicious. This happens before anything reaches an API, so a
clean container never costs a token. Only the flagged excerpts go to your AI provider, which
separates real problems from routine noise.

**Configuration health.** Hashes every compose file it can see. A file that's new or has
changed gets reviewed by your AI provider for security, reliability, and optimization issues,
with secrets redacted before anything leaves your network. A file that hasn't changed costs
nothing on repeat checks.

Findings from Runtime and Configuration health are deduplicated by fingerprint, so a recurring
issue updates its occurrence count instead of generating a new notification every day, and can
be silenced from the dashboard once you've seen it and don't need to be told again.

## What it needs

- Read-only access to the Docker socket, to list running containers, their images, and logs
- Read-only access to the folder where your compose files live
- An API key for Anthropic (Claude) or Google Gemini. Your key, your usage, configured from the
  Settings page in the app itself rather than the compose file
- Optionally, a GitHub token to raise the GitHub API rate limit used for fetching release notes
  (60 requests an hour unauthenticated, 5000 with a token), also set from Settings

## Per-container labels

Add these to a service in your compose file to override the default behavior:

```yaml
labels:
  servicesentinel.ignore: "true"                    # skip this container for update checking
  servicesentinel.logs.ignore: "true"               # skip this container for log watching
  servicesentinel.source: "owner/repo"               # force the GitHub repo used for release notes
  servicesentinel.changelog_url: "https://example.com/changelog"  # use this URL directly, skip auto-detection
```

## Running it

See `docker-compose.example.yml`. Copy it into your stacks folder, fill in `.env`, and deploy.
Once it's up, enable scheduling for whichever features you want under Settings → Scheduling
(each feature's card on the Overview page also has a Check Now button for a one-off run).
Everything starts off.

## Security

**Access control.** There is no login by default -- the app assumes it's running on a trusted
private network. The first time you open it, an onboarding prompt asks you to either set a
username and password or explicitly turn this off; you can change that choice later in
Settings → Access Control, which sits at the top of the page. Once set, your browser will
prompt for the credentials with its own standard sign-in dialog, and every request requires
them until you disable it. There's also an optional "skip login on the local network" toggle,
for keeping the gate off for your own LAN while still requiring it from anywhere else -- note
this checks the direct connection's own source address, so it isn't meaningful if the app sits
behind a reverse proxy (the proxy's address is what it would see, not the original visitor's).
For anything internet-facing, still prefer your own layer in front (a reverse proxy with auth,
a VPN, or similar) rather than relying on any single gate.

**Secrets at rest.** API keys, the Apprise notification URL, and the Access Control password
are stored in the SQLite database under `DATA_DIR`, always encrypted -- never as plain text.
By default the app generates a random key on first launch and keeps it at `DATA_DIR/secrets.key`
(owner-read-only). That protects against anything that exposes only the database file (a copied
or synced `.db`, an SQL-level read), but since the key lives in the same volume, not against
someone holding the entire volume. For that stronger case, set `SECRETS_ENCRYPTION_KEY` in your
`.env` -- it takes precedence over the key file and keeps the key out of the volume entirely;
see `.env.example` for details. Either way: lose the key, lose the secrets -- they're
unrecoverable without it, and you'd re-enter them in Settings.

**Secret redaction is best-effort.** Compose files sent for Configuration health review get
secret-looking values redacted first -- by key name (`PASSWORD`, `TOKEN`, and similar), by value
shape (connection-string passwords, long token-shaped strings), and across the `environment:`,
`labels:`, `command:`, and `secrets:` sections. Heuristics can't catch everything, though. If a
value is genuinely sensitive, keep it out of the compose file entirely (Docker secrets files or
an external secrets manager) rather than trusting redaction alone.

**The Docker socket.** The example compose file mounts the socket `:ro`, but know what that
does and doesn't do: it stops the container replacing the socket file itself, and nothing more --
it does not restrict which Docker Engine API calls are accepted over it. The real protection is
that this app's code only ever issues list/inspect calls. If you want an enforced boundary
rather than a code-review one, put a Docker socket proxy that allowlists specific API endpoints
in front of it.

## Backup and restore

Everything Service Sentinel knows lives in the `service-sentinel-data` volume: the SQLite
database (`/data/service_sentinel.db` in the container) plus, unless you set
`SECRETS_ENCRYPTION_KEY`, the auto-generated encryption key (`/data/secrets.key`) that the
database's stored secrets are unrecoverable without. To back both up, stop the container
first -- SQLite files copied mid-write can be inconsistent -- then copy them out:

```bash
docker compose stop service-sentinel
docker cp service-sentinel:/data/service_sentinel.db ./service_sentinel.db.backup
docker cp service-sentinel:/data/secrets.key ./secrets.key.backup
docker compose start service-sentinel
```

To restore, stop the container, copy the backups over the same paths, and start it again. If
you use `SECRETS_ENCRYPTION_KEY` instead, there's no key file -- back up the passphrase itself,
somewhere separate from the database file.

## Status

An ongoing homelab project, not a hardened production tool. Registry support currently covers
Docker Hub and GHCR (and lscr.io, which fronts GHCR) over the standard OCI distribution API.
Private registries, non-semver tag schemes, and multi-arch edge cases aren't fully handled yet;
see `app/registry.py` for current gaps.

## Development

Built by one person for their own homelab, with heavy use of AI-assisted coding throughout.
Issues and pull requests are welcome, but treat this as a personal project rather than a
supported product.

## License

[MIT](LICENSE)
