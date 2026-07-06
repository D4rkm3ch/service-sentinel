"""Resolves 'this image updated' into 'here's the human-readable changelog text'.

Priority order:
1. A per-container 'releaseradar.changelog_url' label override — fetched as plain text/markdown.
2. The cached location that worked last time for this exact image (see release_notes_cache
   in db.py) — skips straight past guessing and web search if it still works, and falls
   through to full discovery below if it doesn't (e.g. the repo was renamed or moved).
3. A per-container 'releaseradar.source' label override (owner/repo) — used against GitHub Releases.
4. Best-effort guesses based on naming convention: ghcr.io images map directly to a GitHub
   repo; LinuxServer images follow their docker-<name>/<name> convention; a plain Docker Hub
   image's namespace is often the same as the project's GitHub username too.
5. Web search fallback: asks Claude (with the Anthropic API's web search tool enabled, capped
   at 3 searches) to find the actual official release notes/changelog when the guesses above
   come up empty. This costs a small amount per search on top of normal token usage, so it
   only runs when the free/direct sources fail. On success, the discovered location is cached
   for next time — most images only ever pay this cost once.
6. Docker Hub's repository overview page as an absolute last resort (rarely has real changelog
   content, but better than nothing to click on).

Returns (notes_text, source_url) or (None, None) if nothing could be found — callers should
treat that as "flag for manual review" rather than failing the whole check.
"""

import logging
import re

import anthropic
import httpx

from app import db
from app.ai_json import extract_json
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


def _guess_github_repos(image_repo: str) -> list[str]:
    """Returns candidate GitHub repos to try, in priority order, based on naming
    conventions common enough in a typical homelab to be worth trying before ever paying
    for a web search. Not exhaustive by design — anything that doesn't match a known
    convention falls through to web search, same as before."""
    if image_repo.startswith("ghcr.io/"):
        parts = image_repo.removeprefix("ghcr.io/").split("/")
        if len(parts) >= 2:
            return ["/".join(parts[:2])]
        return []

    stripped = image_repo.removeprefix("lscr.io/")
    if stripped.startswith("linuxserver/"):
        name = stripped.split("/", 1)[1]
        # LinuxServer's actual GitHub convention is docker-<name>; a handful of newer
        # images just use <name> directly. Try both.
        return [f"linuxserver/docker-{name}", f"linuxserver/{name}"]

    # A plain two-part Docker Hub image (namespace/name, not a registry host, not the
    # unnamespaced "library" images) very often shares its namespace with the project's
    # GitHub username too.
    parts = image_repo.split("/")
    if len(parts) == 2 and "." not in parts[0] and parts[0] != "library":
        return [f"{parts[0]}/{parts[1]}"]

    return []


def _extract_github_repo_from_url(url: str) -> str | None:
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)", url)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
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
    have already failed, since this costs a small amount per search. Capped at 3 searches so
    even this worst case has a predictable ceiling rather than open-ended exploration."""
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

    text_blocks = [block.text for block in response.content if block.type == "text"]
    if not text_blocks:
        logger.warning("Web search fallback returned no text for %s:%s", image_repo, tag)
        return None, None

    # The model often narrates its search process in earlier text blocks and only puts the
    # final JSON answer in the last one — concatenating everything breaks JSON parsing, so
    # try the last block alone first, and only fall back to the full concatenation (in case
    # the model put the JSON somewhere else) if that doesn't parse.
    data = extract_json(text_blocks[-1].strip())
    if data is None:
        data = extract_json("".join(text_blocks).strip())
    if data is None:
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

    # Try wherever worked last time for this exact image first — this is the whole point:
    # once we've paid the cost of discovering where an image's release notes actually live
    # (however that happened, including the expensive web search), never pay it again
    # unless that location genuinely stops working.
    cached = db.get_release_notes_source(image_repo)
    if cached:
        if cached["method"] == "github":
            notes, url = _fetch_github_release_notes(cached["location"], tag)
            if notes:
                return notes, url
        elif cached["method"] == "url":
            notes, url = _fetch_manual_url(cached["location"])
            if notes:
                return notes, url
        # Cached location no longer works (renamed, moved, deleted) — fall through to full
        # discovery below, same as if nothing had ever been cached.

    candidates = [source_override] if source_override else []
    candidates += _guess_github_repos(image_repo)
    for owner_repo in candidates:
        notes, url = _fetch_github_release_notes(owner_repo, tag)
        if notes:
            db.set_release_notes_source(image_repo, "github", owner_repo)
            return notes, url

    notes, url = _web_search_release_notes(image_repo, tag)
    if notes:
        if url:
            github_repo = _extract_github_repo_from_url(url)
            if github_repo:
                db.set_release_notes_source(image_repo, "github", github_repo)
            else:
                db.set_release_notes_source(image_repo, "url", url)
        return notes, url

    # Absolute last resort: point at the Docker Hub tags page so there's at least something to
    # click, if even the web search came up empty.
    if "/" in image_repo and not image_repo.startswith(("ghcr.io/", "quay.io/")):
        repo_path = image_repo if "/" in image_repo else f"library/{image_repo}"
        return None, f"https://hub.docker.com/r/{repo_path}/tags"

    return None, None
