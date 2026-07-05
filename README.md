# Release Radar

A companion to [Dockhand](https://github.com/izm1chael/Dockhand) (or any updater) that answers
questions Dockhand doesn't: **what does this update actually change for *my* setup, is anything
in my logs actually broken, and is my compose setup sane?**

Three independent features, each **off by default** — nothing runs and no tokens are spent
until you turn each one on from the Overview page:

- **Updates** — checks running containers against their registries on a schedule, and when
  something's genuinely new, asks Claude to summarize what's new and what's breaking, checked
  against your own compose configuration.
- **Log health** — daily, pulls each container's recent logs, filters them locally down to
  lines that look suspicious (this happens before anything touches the API — a clean container
  never costs a token), and only sends the flagged excerpts to Claude to separate real problems
  from routine noise.
- **Compose health** — hashes every compose file it can see; a file that's new or has changed
  gets reviewed by Claude for security, reliability, and optimization issues, secrets redacted
  before anything leaves your network. Unchanged files cost nothing on repeat checks.

Findings from Log health and Compose health are deduplicated by fingerprint — a recurring issue
updates its occurrence count rather than spamming a new notification every day — and can be
manually silenced from the dashboard if you've seen it and don't need to be told again.

## What it needs

- Read-only access to the Docker socket (to list running containers, their images, and logs)
- Read-only access to the folder where your compose files live (e.g. Dockge's stacks directory)
- Your own Anthropic API key (your tokens, your usage — see `.env.example`)
- Optionally, a GitHub token to raise the GitHub API rate limit (unauthenticated is 60 req/hr)

## Per-container labels

Add these to a service in your compose file to override default behaviour:

```yaml
labels:
  releaseradar.ignore: "true"                     # skip this container for update-checking
  releaseradar.logs.ignore: "true"                # skip this container for log watching
  releaseradar.source: "owner/repo"                # force the GitHub repo to check for release notes
  releaseradar.changelog_url: "https://example.com/changelog"  # skip auto-detection, use this URL directly
```

## Running it

See `docker-compose.example.yml`. Copy it into your Dockge stacks folder, fill in `.env`, deploy.
After it's up, visit the Overview page and turn on whichever features you want — everything
starts off.

## Status

Ongoing homelab project, not a production tool. Registry support covers Docker Hub and GHCR
(and lscr.io, which fronts GHCR) over the standard OCI distribution API. Private registries,
non-semver tag schemes, and multi-arch edge cases are not fully hardened — check `app/registry.py`
for current TODOs.
