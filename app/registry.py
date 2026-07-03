"""Checks whether a newer image exists for a given repo:tag.

Covers the standard two-legged OCI distribution auth flow (anonymous or token-based),
which works for Docker Hub, GHCR, and most other public registries. This is intentionally
scoped narrowly: it only needs to answer "is there a newer digest for this tag", not do
anything Dockhand-style with the result.

TODO / known gaps (fine for a homelab MVP, flag before relying on this more broadly):
- No private registry credential support yet (DOCKHAND_REGISTRY_USER-style env vars would
  be the natural place to add this, mirroring Dockhand's own approach).
- Manifest list (multi-arch) handling picks the list digest, which is correct for detecting
  "did anything change" but doesn't try to resolve a specific platform.
- No semver-aware "is there a newer tag" check yet — only "did the digest behind this exact
  tag change", which covers rolling tags (:latest, :4) but not "new major version tag exists
  and you're pinned to an old one". Worth adding if you pin exact version tags.
"""

import httpx

DOCKER_HUB_REGISTRY = "registry-1.docker.io"
DOCKER_HUB_AUTH = "https://auth.docker.io/token"


def _normalize_repo(repo: str) -> tuple[str, str]:
    """Returns (registry_host, repo_path). Handles Docker Hub's implicit host and
    official-image shorthand (e.g. 'nginx' -> library/nginx)."""
    if "/" not in repo:
        return DOCKER_HUB_REGISTRY, f"library/{repo}"

    first_segment = repo.split("/", 1)[0]
    if "." in first_segment or ":" in first_segment or first_segment == "localhost":
        host, path = repo.split("/", 1)
        return host, path

    return DOCKER_HUB_REGISTRY, repo


def _auth_headers(registry_host: str, repo_path: str, client: httpx.Client) -> dict:
    if registry_host == DOCKER_HUB_REGISTRY:
        resp = client.get(
            DOCKER_HUB_AUTH,
            params={"service": "registry.docker.io", "scope": f"repository:{repo_path}:pull"},
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        return {"Authorization": f"Bearer {token}"}

    if registry_host == "ghcr.io":
        resp = client.get(
            "https://ghcr.io/token",
            params={"scope": f"repository:{repo_path}:pull"},
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        return {"Authorization": f"Bearer {token}"}

    # Best-effort fallback for other OCI-compliant registries that allow anonymous pulls.
    return {}


MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)


def get_latest_digest(image_repo: str, tag: str) -> str | None:
    """Returns the current digest the registry serves for repo:tag, or None on failure.

    A digest change between what a container is running and what this returns means
    the tag has moved (typical for :latest-style rolling tags) or that a pinned tag was
    force-updated upstream.
    """
    registry_host, repo_path = _normalize_repo(image_repo)

    with httpx.Client(timeout=10.0) as client:
        try:
            headers = _auth_headers(registry_host, repo_path, client)
            headers["Accept"] = MANIFEST_ACCEPT
            resp = client.head(
                f"https://{registry_host}/v2/{repo_path}/manifests/{tag}",
                headers=headers,
            )
            if resp.status_code == 405:
                # Some registries don't support HEAD for manifests; fall back to GET.
                resp = client.get(
                    f"https://{registry_host}/v2/{repo_path}/manifests/{tag}",
                    headers=headers,
                )
            resp.raise_for_status()
            return resp.headers.get("Docker-Content-Digest")
        except httpx.HTTPError:
            return None
