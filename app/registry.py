"""Checks whether a newer image exists for a given repo:tag.

Uses the standard OCI distribution auth flow: try the request unauthenticated first, and
if the registry challenges with a 401 + WWW-Authenticate header, fetch a token from
whatever realm/service/scope it specifies and retry. This is what every compliant
registry (Docker Hub, GHCR, lscr.io, Quay, etc.) expects, and means we don't need to
special-case each registry by hostname.

For registries we already know always challenge (Docker Hub, GHCR, lscr.io — which is a
front for GHCR), we skip straight to requesting a token instead of wasting a round trip on
a request we know will get a 401. Unknown/self-hosted registries still go through the full
try-then-challenge flow, since we can't assume they require auth at all.

TODO / known gaps (fine for a homelab MVP, flag before relying on this more broadly):
- No private registry credential support yet (REGISTRY_USER-style env vars would be the
  natural place to add this).
- Manifest list (multi-arch) handling picks the list digest, which is correct for detecting
  "did anything change" but doesn't try to resolve a specific platform.
- No semver-aware "is there a newer tag" check yet — only "does the digest behind this exact
  tag differ from what's running", which covers rolling tags (:latest, :4) but not "new major
  version tag exists and you're pinned to an old one". Worth adding if you pin exact version tags.
"""

import re

import httpx

DOCKER_HUB_REGISTRY = "registry-1.docker.io"

# Hosts that mean "Docker Hub" but aren't the actual v2 API host (registry-1.docker.io is the
# only host that serves the v2 API; 'docker.io' itself just redirects to Docker's marketing
# site for arbitrary paths, which looks like a working response but isn't the registry).
_DOCKER_HUB_ALIASES = {"docker.io", "index.docker.io"}

# (realm, service) for registries known to always require a bearer token. lscr.io is a
# redirect front for GHCR (confirmed via its own challenge response), so it uses GHCR's realm.
KNOWN_REALMS: dict[str, tuple[str, str]] = {
    DOCKER_HUB_REGISTRY: ("https://auth.docker.io/token", "registry.docker.io"),
    "ghcr.io": ("https://ghcr.io/token", "ghcr.io"),
    "lscr.io": ("https://ghcr.io/token", "ghcr.io"),
}

MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)

_CHALLENGE_PARAM = re.compile(r'(\w+)="([^"]*)"')


def _normalize_repo(repo: str) -> tuple[str, str]:
    """Returns (registry_host, repo_path). Handles Docker Hub's implicit host and
    official-image shorthand (e.g. 'nginx' -> library/nginx)."""
    if "/" not in repo:
        return DOCKER_HUB_REGISTRY, f"library/{repo}"

    first_segment = repo.split("/", 1)[0]
    if first_segment in _DOCKER_HUB_ALIASES:
        _, path = repo.split("/", 1)
        return DOCKER_HUB_REGISTRY, path
    if "." in first_segment or ":" in first_segment or first_segment == "localhost":
        host, path = repo.split("/", 1)
        return host, path

    return DOCKER_HUB_REGISTRY, repo


def _token_from_realm(realm: str, service: str, repo_path: str, client: httpx.Client) -> str | None:
    resp = client.get(realm, params={"service": service, "scope": f"repository:{repo_path}:pull"})
    resp.raise_for_status()
    return resp.json().get("token")


def _bearer_token_for_challenge(challenge: str, client: httpx.Client) -> str | None:
    """Parses a 'Bearer realm="...",service="...",scope="..."' WWW-Authenticate header
    and fetches a token from the realm it specifies. Used for registries not in
    KNOWN_REALMS, where we have to discover the realm from the challenge itself."""
    if not challenge.lower().startswith("bearer"):
        return None
    params = dict(_CHALLENGE_PARAM.findall(challenge))
    realm = params.pop("realm", None)
    if not realm:
        return None
    resp = client.get(realm, params=params)
    resp.raise_for_status()
    return resp.json().get("token")


def get_latest_digest(image_repo: str, tag: str) -> str | None:
    """Returns the current digest the registry serves for repo:tag, or None on failure.

    A digest change between what a container is running and what this returns means
    the tag has moved (typical for :latest-style rolling tags) or that a pinned tag was
    force-updated upstream.
    """
    registry_host, repo_path = _normalize_repo(image_repo)
    manifest_url = f"https://{registry_host}/v2/{repo_path}/manifests/{tag}"

    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        try:
            headers = {"Accept": MANIFEST_ACCEPT}

            known = KNOWN_REALMS.get(registry_host)
            if known:
                # Skip the wasted "try unauthenticated first" round trip — we already know
                # this host always challenges.
                realm, service = known
                token = _token_from_realm(realm, service, repo_path, client)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                resp = client.head(manifest_url, headers=headers)
            else:
                resp = client.head(manifest_url, headers=headers)
                if resp.status_code == 401:
                    challenge = resp.headers.get("WWW-Authenticate", "")
                    token = _bearer_token_for_challenge(challenge, client)
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                        resp = client.head(manifest_url, headers=headers)

            if resp.status_code == 401 and known:
                # Our hardcoded realm assumption was wrong for this host after all —
                # fall back to discovering it from the actual challenge instead of giving up.
                challenge = resp.headers.get("WWW-Authenticate", "")
                token = _bearer_token_for_challenge(challenge, client)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    resp = client.head(manifest_url, headers=headers)

            if resp.status_code == 405:
                # Some registries don't support HEAD for manifests; fall back to GET.
                resp = client.get(manifest_url, headers=headers)

            resp.raise_for_status()
            return resp.headers.get("Docker-Content-Digest")
        except httpx.HTTPError:
            return None
