# Release Radar

A companion to [Dockhand](https://github.com/izm1chael/Dockhand) (or any updater) that answers
the question Dockhand doesn't: **"what does this update actually change for *my* setup?"**

Release Radar runs alongside your existing update tool. It doesn't pull images, it doesn't touch
running containers, and it doesn't replace your update workflow. On a schedule you set, it:

1. Looks at your running containers and checks their registries (Docker Hub / GHCR) for a newer
   image than what's currently running.
2. When it finds one, fetches the project's release notes (GitHub Releases, with a Docker Hub
   fallback, or a manual override URL you set per container).
3. Sends those notes to Claude, along with the *relevant slice* of your own compose file for that
   service (image, env var names, volumes, ports, labels — secret values are redacted before they
   leave your network), and asks for a short note: what's new, what's breaking, and whether either
   of those touches something you've actually configured.
4. Shows the result on a small dashboard so you can decide whether to update, and when.

It also exposes `POST /webhook/dockhand` — point Dockhand's generic webhook notifier at it and
it'll run an out-of-cycle check immediately instead of waiting for the schedule. This is a
convenience, not a requirement; the scheduled check is the source of truth.

## What it needs

- Read-only access to the Docker socket (to list running containers and their images)
- Read-only access to the folder where your compose files live (e.g. Dockge's stacks directory)
- Your own Anthropic API key (your tokens, your usage — see `.env.example`)
- Optionally, a GitHub token to raise the GitHub API rate limit (unauthenticated is 60 req/hr,
  which is fine for a handful of containers but tight if you're watching 30+)

## Per-container labels

Add these to a service in your compose file to override default behaviour:

```yaml
labels:
  releaseradar.ignore: "true"                     # skip this container entirely
  releaseradar.source: "owner/repo"                # force the GitHub repo to check for release notes
  releaseradar.changelog_url: "https://example.com/changelog"  # skip auto-detection, use this URL directly
```

## Running it

See `docker-compose.example.yml`. Copy it into your Dockge stacks folder, fill in `.env`, deploy.

## Status

First pass / MVP. Registry support covers Docker Hub and GHCR public images over the standard OCI
distribution API. Private registries, non-semver tag schemes, and multi-arch edge cases are not
fully hardened yet — check `app/registry.py` for the current TODOs before relying on this for
anything business-critical. This is a homelab tool, not a production update pipeline.
