"""Resolves 'this image updated' into 'here's the human-readable changelog text'.

Priority order:
1. A per-container 'releaseradar.changelog_url' label override — fetched as plain text/markdown.
2. A per-container 'releaseradar.source' label override (owner/repo) — used against GitHub Releases.
3. Best-effort guess: ghcr.io/owner/repo images map directly to a GitHub repo.
4. Docker Hub's repository overview page as a last resort (rarely has real changelog content,
   but better than nothing).

Returns (notes_text, source_url) or (None, None) if nothing could be found — callers should
treat that as "flag for manual review" rather than failing the whole check.
"""

import httpx

from app.config import settings


def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


def _fetch_github_release_notes(owner_repo: str, tag: str) -> tuple[str | None, str | None]:
    with httpx.Client(timeout=10.0, headers=_github_headers()) as client:
        # Try an exact tag match first (common naming: 'v1.2.3', '1.2.3').
        for candidate_tag in (tag, f"v{tag}", tag.lstrip("v")):
            resp = client.get(
                f"https://api.github.com/repos/{owner_repo}/releases/tags/{candidate_tag}"
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("body") or "(release has no description)", data.get("html_url")

        # Fall back to the most recent release if we can't match the tag exactly —
        # still useful signal, just less precisely scoped.
        resp = client.get(f"https://api.github.com/repos/{owner_repo}/releases", params={"per_page": 1})
        if resp.status_code == 200 and resp.json():
            data = resp.json()[0]
            return data.get("body") or "(release has no description)", data.get("html_url")

    return None, None


def _guess_github_repo(image_repo: str) -> str | None:
    if image_repo.startswith("ghcr.io/"):
        parts = image_repo.removeprefix("ghcr.io/").split("/")
        if len(parts) >= 2:
            return "/".join(parts[:2])
    return None


def _fetch_manual_url(url: str) -> tuple[str | None, str | None]:
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text, url
    except httpx.HTTPError:
        return None, None


def get_release_notes(
    image_repo: str,
    tag: str,
    source_override: str | None = None,
    changelog_url_override: str | None = None,
) -> tuple[str | None, str | None]:
    if changelog_url_override:
        return _fetch_manual_url(changelog_url_override)

    owner_repo = source_override or _guess_github_repo(image_repo)
    if owner_repo:
        notes, url = _fetch_github_release_notes(owner_repo, tag)
        if notes:
            return notes, url

    # Last resort: point at the Docker Hub tags page so there's at least something to click.
    if "/" in image_repo and not image_repo.startswith(("ghcr.io/", "quay.io/")):
        repo_path = image_repo if "/" in image_repo else f"library/{image_repo}"
        return None, f"https://hub.docker.com/r/{repo_path}/tags"

    return None, None
