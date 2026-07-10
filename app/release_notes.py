"""Resolves 'this image updated' into 'here's the human-readable changelog text'.

Stage 6 of the ground-up rebuild: real release notes. Stage 8 (brought forward once Stage 7's
AI summarization landed) adds a web search fallback, always on — a real AI provider call with
web search/grounding tool use (see app/ai_provider.py), versus everything above being either
free or a plain HTTP request, but only ever reached after every cheaper option above has come up
empty, and cached per image after the first successful lookup (see step 5) so it's never a
repeat cost for the same image. This used to be an opt-in Settings toggle, off by default; it's
unconditional now because the whole point of the app is real release notes, and a container
that falls through every guess above without this step never gets any — silently defeating the
purpose for exactly the images that need it most (ones that don't follow a guessable naming
convention). Priority order get_release_notes() actually uses:
1. A per-container 'servicesentinel.changelog_url' label override — fetched as plain text/markdown.
2. The cached location that worked last time for this exact image (see release_notes_cache
   in db.py) — skips straight past guessing if it still works, and falls through to full
   discovery below if it doesn't (e.g. the repo was renamed or moved).
3. A per-container 'servicesentinel.source' label override (owner/repo) — used against GitHub Releases.
4. Best-effort guesses based on naming convention: ghcr.io images map directly to a GitHub
   repo; LinuxServer images follow their docker-<name>/<name> convention; a plain Docker Hub
   image's namespace is often the same as the project's GitHub username too.
5. Asks the configured AI provider to search the web — see _web_search_release_notes() below. A
   successful result is cached exactly like a successful guess (as "github" if the discovered
   URL is a GitHub repo, via _extract_github_repo_from_url, so future lookups reuse the cheap
   GitHub Releases API path instead of searching again; as a plain "url" otherwise), so this
   expensive call only ever happens once per image, not on every check.
6. Docker Hub's repository overview page as an absolute last resort (rarely has real changelog
   content, but better than nothing to click on).

Returns (notes_text, source_url) or (None, None) if nothing could be found — callers should
treat that as "flag for manual review" rather than failing the whole check."""

import logging
import re
from datetime import datetime

import httpx

from app import ai_provider, db
from app.ai_json import extract_json
from app.config import settings

logger = logging.getLogger("service_sentinel.release_notes")

# Hard ceiling on how many releases get compiled into one prompt regardless of the Settings
# lookback window -- a container that's gone unchecked for a very long time (or one with a
# very active release cadence) could otherwise pull in an unbounded number of releases.
_MAX_COMPILED_RELEASES = 20


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


def _fetch_github_releases_since(owner_repo: str, since: datetime) -> list[dict]:
    """Every GitHub release published after `since`, newest first, capped at
    _MAX_COMPILED_RELEASES as a hard ceiling. Stops paging as soon as it hits a release
    published at or before the cutoff, since the API already returns releases newest-first --
    everything after that point is guaranteed to be even older."""
    releases: list[dict] = []
    with httpx.Client(timeout=10.0, headers=_github_headers()) as client:
        page = 1
        while len(releases) < _MAX_COMPILED_RELEASES:
            resp = client.get(
                f"https://api.github.com/repos/{owner_repo}/releases",
                params={"per_page": 30, "page": page},
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            hit_cutoff = False
            for item in batch:
                published = item.get("published_at")
                if not published:
                    continue
                published_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if published_dt <= since:
                    hit_cutoff = True
                    break
                releases.append(item)
                if len(releases) >= _MAX_COMPILED_RELEASES:
                    break
            if hit_cutoff or len(batch) < 30:
                break
            page += 1
    return releases


def _compile_releases_text(releases: list[dict]) -> str:
    """releases is newest-first (see _fetch_github_releases_since) -- reversed here so the
    compiled text reads oldest-to-newest, matching the "catch the operator up in order" framing
    summarizer.py's prompt uses."""
    parts = []
    for item in reversed(releases):
        tag = item.get("tag_name") or item.get("name") or "unknown version"
        published = (item.get("published_at") or "")[:10]
        body = (item.get("body") or "(release has no description)").strip()
        parts.append(f"## {tag} ({published})\n{body}")
    return "\n\n".join(parts)


def _resolve_github_notes(owner_repo: str, tag: str, since: datetime | None) -> tuple[str | None, str | None]:
    """Tries the multi-release compilation first when `since` is given and there are genuinely
    2+ releases to compile (a single release found since the cutoff is the normal, common case
    -- falls straight through to the existing single-release path below so behavior there is
    unchanged from before this existed). since=None (e.g. this container's very first check
    ever, with no prior check to measure a window from) always uses the single-release path."""
    if since is not None:
        try:
            releases = _fetch_github_releases_since(owner_repo, since)
        except httpx.HTTPError:
            releases = []
        if len(releases) >= 2:
            return _compile_releases_text(releases), f"https://github.com/{owner_repo}/releases"

    return _fetch_github_release_notes(owner_repo, tag)


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
    """Last-resort fallback: asks the configured AI provider to search the web for the real
    release notes when guessing the source repo directly didn't work. Only called when the free
    options above have already failed, since this costs a small amount per search. Capped at 3
    searches (Anthropic) so even this worst case has a predictable ceiling rather than
    open-ended exploration -- Gemini's grounding tool decides its own query count."""
    if not ai_provider.is_configured():
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
        text = ai_provider.web_search(prompt, max_tokens=1200)
    except Exception:
        logger.exception("Web search fallback failed for %s:%s", image_repo, tag)
        return None, None

    if not text.strip():
        logger.warning("Web search fallback returned no text for %s:%s", image_repo, tag)
        return None, None

    data = extract_json(text.strip())
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
    since: datetime | None = None,
) -> tuple[str | None, str | None]:
    """Label overrides, the source-cache, and naming-convention guesses, then (if enabled in
    Settings) a web search as a last resort before giving up. See the module docstring above
    for the full priority order and why the web search step is opt-in and its result cached.

    since, when given, compiles EVERY GitHub release published after that point into one text
    blob instead of just the latest/exact-tag match -- for a container that's missed several
    releases since its last check (see persist._release_notes_since for how the cutoff is
    computed). Only applies to GitHub-backed methods; a manual changelog_url_override or a
    non-GitHub cached source has no structured release list to compile from, so those always
    behave exactly as before."""
    if changelog_url_override:
        return _fetch_manual_url(changelog_url_override)

    # Try wherever worked last time for this exact image first — this is the whole point:
    # once we've paid the cost of discovering where an image's release notes actually live,
    # never pay it again unless that location genuinely stops working.
    cached = db.get_release_notes_source(image_repo)
    if cached:
        if cached["method"] == "github":
            notes, url = _resolve_github_notes(cached["location"], tag, since)
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
        notes, url = _resolve_github_notes(owner_repo, tag, since)
        if notes:
            db.set_release_notes_source(image_repo, "github", owner_repo)
            return notes, url

    notes, url = _web_search_release_notes(image_repo, tag)
    if notes:
        # Cache as a GitHub repo (the cheap, high-quality path future lookups will use)
        # whenever the discovered URL is actually a GitHub one; otherwise fall back to
        # caching the plain URL, same as a changelog_url override would be re-fetched.
        github_repo = _extract_github_repo_from_url(url) if url else None
        if github_repo:
            db.set_release_notes_source(image_repo, "github", github_repo)
        elif url:
            db.set_release_notes_source(image_repo, "url", url)
        return notes, url

    # Absolute last resort: point at the Docker Hub tags page so there's at least something to
    # click, since nothing above found real notes.
    if "/" in image_repo and not image_repo.startswith(("ghcr.io/", "quay.io/")):
        return None, f"https://hub.docker.com/r/{image_repo}/tags"

    return None, None
