"""Resolves 'this image updated' into 'here's the human-readable changelog text'.

Priority order:
1. A per-container 'releaseradar.changelog_url' label override — fetched as plain text/markdown.
2. A per-container 'releaseradar.source' label override (owner/repo) — used against GitHub Releases.
3. Best-effort guess: ghcr.io/owner/repo images map directly to a GitHub repo.
4. Web search fallback: asks Claude (with the Anthropic API's web search tool enabled) to find
   the actual official release notes/changelog when the guesses above come up empty. This costs
   a small amount per search on top of normal token usage, so it only runs when the free/direct
   sources fail.
5. Docker Hub's repository overview page as an absolute last resort (rarely has real changelog
   content, but better than nothing to click on).

Returns (notes_text, source_url) or (None, None) if nothing could be found — callers should
treat that as "flag for manual review" rather than failing the whole check.
"""

import json
import logging

import anthropic
import httpx

from app.config import settings

logger = logging.getLogger("release_radar.release_notes")


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


def _web_search_release_notes(image_repo: str, tag: str) -> tuple[str | None, str | None]:
    """Last-resort fallback: asks Claude to search the web for the real release notes when
    guessing the source repo directly didn't work. Only called when the free options above
    have already failed, since this costs a small amount per search."""
    if not settings.anthropic_api_key:
        return None, None

    prompt = f"""Find the official release notes or changelog for the Docker image "{image_repo}", \
tag/version "{tag}".

Search for the project's actual GitHub releases page, changelog file, or official announcement \
for this specific version — prefer the project's own repository or documentation over \
third-party mirrors, package indexes, or unofficial blog posts.

Respond with ONLY a JSON object and nothing else — no markdown fences, no preamble. Use exactly \
this shape:
{{"found": true or false, "source_url": "the URL you found, or null", "notes": "a paraphrased, \
faithful description of what changed in this release in your own words, or null if nothing found"}}"""

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.claude_model,
            max_tokens=1200,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        logger.exception("Web search fallback failed for %s:%s", image_repo, tag)
        return None, None

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Web search fallback returned non-JSON for %s:%s", image_repo, tag)
        return None, None

    if not data.get("found"):
        return None, None
    return data.get("notes"), data.get("source_url")


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

    notes, url = _web_search_release_notes(image_repo, tag)
    if notes:
        return notes, url

    # Absolute last resort: point at the Docker Hub tags page so there's at least something to
    # click, if even the web search came up empty.
    if "/" in image_repo and not image_repo.startswith(("ghcr.io/", "quay.io/")):
        repo_path = image_repo if "/" in image_repo else f"library/{image_repo}"
        return None, f"https://hub.docker.com/r/{repo_path}/tags"

    return None, None
