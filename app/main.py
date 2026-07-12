import hashlib
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import markdown
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app import ai_provider, check_state, compose_lookup, compose_reviewer, db, log_watcher, persist, release_notes, stacks
from app.check_state import format_summary, get_progress, get_state, set_running
from app.config import settings
from app.notifications import send_test_notification
from app.summarizer import summarize_findings_overview
from app.schedule_spec import DAY_NAMES, describe as describe_schedule
from app.scheduler import (
    apply_schedules,
    start_scheduler,
    trigger_compose_check_now,
    trigger_log_check_now,
)
from app.uptime import get_uptime_str

APP_VERSION = "0.7.0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("service_sentinel")


class NoStoreMiddleware(BaseHTTPMiddleware):
    """Prevents the browser from serving a stale cached snapshot on back/forward navigation."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response


def _static_asset_version() -> str:
    """A content hash of style.css, appended as a cache-busting query string on its <link> tag
    (see base.html) -- StaticFiles is deliberately excluded from NoStoreMiddleware below so
    browsers can cache CSS/JS long-term, but that means a plain unversioned /static/style.css
    URL keeps serving an old cached copy after a deploy changes it. Hashing the file instead of
    just using APP_VERSION means this bumps automatically on every CSS change, not only on
    releases that remembered to bump the version string."""
    css_path = Path(__file__).parent / "static" / "style.css"
    return hashlib.sha256(css_path.read_bytes()).hexdigest()[:10]


app = FastAPI(title="Service Sentinel")
app.add_middleware(NoStoreMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["app_version"] = APP_VERSION
templates.env.globals["static_asset_version"] = _static_asset_version()
templates.env.globals["github_url"] = "https://github.com/D4rkm3ch/service-sentinel"
templates.env.globals["app_timezone"] = db.get_timezone  # a callable, not a value -- Stage 5c
templates.env.globals["get_uptime_str"] = get_uptime_str

# "Suggestion" reads oddly for an update ("this release is a suggestion"?) — "Safe" matches
# the actual meaning (nothing risky here, safe to update) without changing the underlying
# severity value used for storage, sorting, and notification thresholds everywhere else.
SEVERITY_LABELS = {
    "updates": {
        # Covers routine fixes and dependency bumps as well as genuine security patches (e.g.
        # an XSS/path-traversal fix) -- both land on the same underlying "bugfix" severity (see
        # summarizer.py's severity rules), so the label needs to read honestly for either one
        # rather than undersell a real security fix as just "Bug Fixes".
        "bugfix": "Fixes & Security",
        "feature": "New Features",
        "action_needed": "Action Needed",
        "breaking": "Breaking Change",
    },
}
UPDATE_SEVERITIES = ("bugfix", "feature", "action_needed", "breaking")
FINDING_SEVERITIES = ("suggestion", "warning", "critical")


def severity_label(context: str, value: str) -> str:
    return SEVERITY_LABELS.get(context, {}).get(value, value.capitalize())


templates.env.globals["severity_label"] = severity_label


def local_dt(iso_utc: str | None) -> str:
    """Converts a stored UTC ISO timestamp (every timestamp in the database is UTC -- see
    db.now_iso()) into the configured display TZ (db.get_timezone(), Settings page) as
    "YYYY-MM-DD HH:MM" -- the one Jinja filter every template with a timestamp column/line must
    route through, rather than slicing the raw UTC string directly (x[:16].replace('T', ' ')),
    which is what every such table did before this existed and is exactly why none of them ever
    reflected the configured TZ -- only check_state.format_summary's own separately-hand-rolled
    "Last checked: ..." status line did. Same conversion logic as that function's local
    _local_timestamp helper, just a different display format (this one matches what the tables
    already looked like before, so this change is a TZ fix, not a format change)."""
    if not iso_utc:
        return "—"
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        local = dt.astimezone(ZoneInfo(db.get_timezone()))
    except ZoneInfoNotFoundError:
        local = dt.astimezone(timezone.utc)
    return local.strftime("%Y-%m-%d %H:%M")


templates.env.filters["local_dt"] = local_dt

# Every markdown-rendered block in the app (release notes, AI summaries/overviews, finding
# descriptions and suggested fixes) can contain links the user didn't put there themselves --
# a GitHub release body linking to Watchtower, a CHANGELOG.md, an upstream issue. Those should
# open in a new tab rather than navigating the user away from Service Sentinel. The regex only
# matches http(s) URLs, so it never touches non-link markup this app generates itself.
_EXTERNAL_LINK_RE = re.compile(r'<a href="(https?://[^"]*)"')


def render_markdown(text: str) -> str:
    return _EXTERNAL_LINK_RE.sub(r'<a target="_blank" rel="noopener" href="\1"', markdown.markdown(text))


TRIGGER_FUNCS = {
    "logs": trigger_log_check_now,
    "compose": trigger_compose_check_now,
}


@app.on_event("startup")
def on_startup():
    for problem in settings.validate():
        logger.warning(problem)
    db.init_db()
    start_scheduler()
    logger.info("Service Sentinel started")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

_CARD_TITLES = {"updates": "Updates", "logs": "Log Health", "compose": "Compose Health"}
_CARD_TAB_URLS = {"updates": "/updates", "logs": "/logs", "compose": "/compose"}


def _build_card(feature: str, title: str, tab_url: str) -> dict:
    enabled = db.get_feature_enabled(feature)
    if feature == "updates":
        summary = db.latest_update_summary()
        count = summary["unread"]
        headline = f"{count} pending update{'s' if count != 1 else ''}" if count else "All up to date"
        last_at = summary["last_at"]
    else:
        summary = db.findings_health_summary(feature)
        count = summary["active"]
        headline = f"{count} active finding{'s' if count != 1 else ''}" if count else "All clean"
        last_at = summary["last_at"]
    detail = f"Last checked {local_dt(last_at)}" if last_at else "Never checked"
    running = get_state(feature)["running"]
    return {
        "feature": feature, "title": title, "enabled": enabled,
        "headline": headline, "detail": detail, "tab_url": tab_url,
        "running": running,
        # Reuses the exact same live progress text the feature's own status badge shows (e.g.
        # "Checking for updates (3/59)…" for Updates, "Checking container logs (3/59)…" for
        # Logs, a plain "Checking…" for Compose, which doesn't report granular progress) -- the
        # Overview card's indicator is meant to read identically to the real thing, not a
        # simplified stand-in.
        "progress_text": _progress_text(get_progress(feature)) if running else "",
    }


@app.get("/")
def overview(request: Request):
    cards = [
        _build_card("updates", "Updates", "/updates"),
        _build_card("logs", "Log Health", "/logs"),
        _build_card("compose", "Compose Health", "/compose"),
    ]
    return templates.TemplateResponse(
        "overview.html", {"request": request, "cards": cards, "active_tab": "overview"}
    )


@app.post("/settings/toggle/{feature}")
def toggle_feature(feature: str, request: Request):
    if feature not in ("updates", "logs", "compose"):
        raise HTTPException(status_code=404)
    db.set_feature_enabled(feature, not db.get_feature_enabled(feature))
    # Takes effect immediately (adds/removes the periodic job) rather than waiting for a
    # restart -- see apply_schedules()'s own docstring for why this is the only place the
    # toggle is actually enforced.
    apply_schedules()
    card = _build_card(feature, _CARD_TITLES[feature], _CARD_TAB_URLS[feature])
    return templates.TemplateResponse("_feature_card.html", {"request": request, "card": card})


@app.get("/status/card/{feature}")
def feature_card_status(request: Request, feature: str, prev_running: bool = False):
    """Backs each Overview card's own tiny live status indicator, next to its title -- the
    same perpetual self-poll pattern as _status.html/_status_poll.html (see
    _render_status_poll's docstring), so a scheduled check starting makes "Checking…" appear on
    the card with no click needed. On a genuine running -> idle transition, also re-renders and
    swaps in the whole card (oob) so its headline/detail count doesn't sit stale once the check
    that was running when the page loaded finishes."""
    if feature not in _CARD_TITLES:
        raise HTTPException(status_code=404)
    running = get_state(feature)["running"]
    progress_text = _progress_text(get_progress(feature)) if running else ""
    card = _build_card(feature, _CARD_TITLES[feature], _CARD_TAB_URLS[feature]) if prev_running and not running else None
    return templates.TemplateResponse(
        "_card_status_poll.html",
        {"request": request, "feature": feature, "running": running, "progress_text": progress_text, "card": card},
    )


# ---------------------------------------------------------------------------
# Shared check-now / status handlers
# ---------------------------------------------------------------------------

# Live progress text (e.g. "Checking for updates (23/59)") only makes sense for a feature that
# reports progress — currently just "updates" (Stage 2, staged Stage 6). Polling faster while
# it's running gives meaningfully live-feeling updates now that a full check finishes in
# seconds rather than up to a minute; logs/compose keep their original 2s cadence untouched.
_FAST_POLL_FEATURES = {"updates", "logs", "compose"}

# Human label per pipeline stage (check_state.py's progress "stage" field) — every stage that
# reports progress needs an entry here, or it silently falls back to the generic "Checking…"
# below. Add the new stage's name here whenever a stage is added to the pipeline (see
# persist.py's docstrings for why a stage that reports progress but isn't named here, or
# doesn't report progress at all, both look exactly like a hang to the user).
_STAGE_LABELS = {
    "checking": "Checking for updates",
    "release_notes": "Grabbing release notes",
    "summarizing": "Summarizing with AI",
    "regenerating": "Regenerating AI response",
    "stack_analysis": "Analyzing cross-service impact",
    "checking_logs": "Checking container logs",
    "log_stack_analysis": "Analyzing cross-service impact",
    "triage_logs": "Analyzing logs with AI",
    "checking_compose_files": "Checking compose files",
}


def _progress_text(progress: dict) -> str:
    total = progress.get("total") or 0
    if not total:
        return "Checking…"
    label = _STAGE_LABELS.get(progress.get("stage"), "Checking")
    return f"{label} ({progress['done']}/{total})…"


# How often the status badge polls itself while idle, purely to notice a check that started
# some other way -- the scheduler firing, or a click on a different open tab/device -- since
# nothing else pushes that event to an already-open page. Deliberately slower than the
# feature's own running-cadence (poll_delay_ms below): there's no live progress to show yet,
# just a boolean to notice.
_IDLE_POLL_DELAY_MS = 3000


def _status_context(request: Request, feature: str) -> dict:
    state = get_state(feature)
    progress = get_progress(feature)
    # A different feature's check running elsewhere (this page's own action buttons are also
    # disabled sitewide for this same reason -- see base.html) gets its own short "a check is
    # running" variant of this same status badge, in place of the static "Last checked" text,
    # rather than a separate banner elsewhere on the page -- it's temporary state exactly like
    # this page's own running badge, so it belongs in the same spot as that badge, not a second
    # place to look. Own-feature running always takes priority if both are somehow true at once.
    other_running_feature = next(
        (f for f in check_state.FEATURES if f != feature and get_state(f)["running"]), None,
    )
    poll_delay_ms = 500 if feature in _FAST_POLL_FEATURES else 2000
    return {
        "request": request,
        "feature": feature,
        "state": state,
        "progress": progress,
        "progress_text": _progress_text(progress),
        "other_running_feature": other_running_feature,
        "poll_delay_ms": poll_delay_ms,
        "idle_poll_delay_ms": _IDLE_POLL_DELAY_MS,
        "status_summary_text": format_summary(feature, state),
    }


def _render_status(request: Request, feature: str):
    context = _status_context(request, feature)
    resp = templates.TemplateResponse("_status.html", context)
    if not context["state"]["running"]:
        resp.headers["HX-Trigger"] = "checkComplete"
    return resp


def _render_status_poll(
    request: Request, feature: str, prev_running: bool = False, prev_badge_running: bool = False,
):
    """Backs the status badge's own perpetual self-poll (see _status.html/_status_poll.html):
    every response re-embeds a fresh poller span so the chain keeps running indefinitely at
    whatever cadence matches the current state, which is what lets a scheduled check (or a
    manual one kicked off from a different tab) make the badge start showing progress on its
    own, with no click needed on this page. checkComplete only fires on a genuine running ->
    idle transition (prev_running says what the last poll considered current, compared against
    this poll's fresh state) -- firing it on every idle tick would re-trigger every table's
    "every 20s, checkComplete from:body" listener every _IDLE_POLL_DELAY_MS for nothing.

    prev_badge_running is a separate signal from prev_running: whether the badge was already
    showing the running/spinner visual last tick, for ANY reason (this feature's own check, or
    another feature's -- see other_running_feature in _status_context). _status_poll.html uses
    it to pick a cheap text-only OOB swap while that stays true (steady-state ticking without
    ever destroying/recreating the spinner's DOM node, which would restart its CSS animation
    before a single rotation finishes -- see test_status_poll_does_not_re_render_the_spinner_node),
    and a full-div swap only on the one tick that actually transitions into showing the badge --
    covering both this feature's own start and noticing another feature's check via the idle
    poll, neither of which prev_running (this feature's own running flag only) can tell apart
    from a same-feature steady state on its own."""
    context = _status_context(request, feature)
    context["prev_badge_running"] = prev_badge_running
    resp = templates.TemplateResponse("_status_poll.html", context)
    if prev_running and not context["state"]["running"]:
        resp.headers["HX-Trigger"] = "checkComplete"
    return resp


@app.post("/updates/check-now")
def updates_check_now(request: Request):
    _launch_check_if_not_running()
    return _render_status(request, "updates")


@app.get("/updates/status-poll")
def updates_status_poll(request: Request, prev_running: bool = False, prev_badge_running: bool = False):
    return _render_status_poll(request, "updates", prev_running, prev_badge_running)


@app.get("/updates/running-state")
def updates_running_state():
    """Tiny polled-everywhere signal (see base.html) for disabling every Updates-related
    Check now / Reset & re-check button across the Updates, Stack, and Service pages while
    any check -- full or a single scoped per-item recheck, both claim the same mutex -- is in
    flight. Deliberately its own minimal endpoint rather than reusing status-poll's full HTML
    fragment: this needs to be pollable from pages that don't render the status badge at all
    (Stack, Service), and every caller only ever needs the one boolean."""
    return {"running": get_state("updates")["running"]}


@app.get("/logs/running-state")
def logs_running_state():
    """Logs' equivalent of /updates/running-state -- see that route's docstring. Backs the
    Logs page's Check now button, which now dims the same way Updates' does (base.html)."""
    return {"running": get_state("logs")["running"]}


@app.get("/compose/running-state")
def compose_running_state():
    """Compose's equivalent of /updates/running-state -- see that route's docstring."""
    return {"running": get_state("compose")["running"]}


@app.get("/updates/partial")
def updates_partial(request: Request, sort: str = "importance", dir: str = "asc",
                     csort: str = "container", cdir: str = "asc", show_silenced: bool = False):
    rows = db.list_tracked_containers_with_status()
    updates = _sort_and_filter_rows(rows, sort, dir, updates_only=True, show_silenced=show_silenced)
    return templates.TemplateResponse(
        "_updates_table.html",
        {
            "request": request, "updates": updates, "sort": sort, "dir": dir, "csort": csort, "cdir": cdir,
            "show_silenced": show_silenced, "is_partial": True,
        },
    )


@app.get("/updates/partial/containers")
def updates_partial_containers(request: Request, sort: str = "importance", dir: str = "asc",
                                csort: str = "container", cdir: str = "asc"):
    rows = db.list_tracked_containers_with_status()
    containers = _sort_and_filter_rows(rows, csort, cdir, updates_only=False)
    return templates.TemplateResponse(
        "_containers_table.html",
        {"request": request, "containers": containers, "sort": sort, "dir": dir, "csort": csort, "cdir": cdir, "is_partial": True},
    )


@app.post("/logs/check-now")
def logs_check_now(request: Request):
    set_running("logs")
    TRIGGER_FUNCS["logs"]()
    return _render_status(request, "logs")


@app.get("/logs/status-poll")
def logs_status_poll(request: Request, prev_running: bool = False, prev_badge_running: bool = False):
    return _render_status_poll(request, "logs", prev_running, prev_badge_running)


@app.get("/logs/partial/issues")
def logs_partial_issues(request: Request, show_silenced: bool = False, sort: str = "severity", dir: str = "asc"):
    issues = _attach_stack_info(db.list_subjects_with_findings("logs", include_silenced=show_silenced), "subject")
    issues = _sort_issue_rows(issues, sort, dir)
    return templates.TemplateResponse(
        "_issues_grouped_table.html",
        {
            "request": request, "issues": issues, "source": "logs", "show_silenced": show_silenced,
            "sort": sort, "dir": dir, "is_partial": True, "show_stack_column": True,
        },
    )


@app.get("/logs/partial/containers")
def logs_partial_containers(request: Request, csort: str = "status", cdir: str = "asc"):
    items = _attach_stack_info(db.all_log_watch_states_with_status(), "name")
    items = _sort_status_list_rows(items, csort, cdir)
    return templates.TemplateResponse(
        "_status_list_table.html",
        {
            "request": request, "items": items, "detail_base": "/logs/container",
            "csort": csort, "cdir": cdir, "partial_url": "/logs/partial/containers",
            "target_id": "logs-containers-table", "base_url": "/logs", "is_partial": True,
            "show_stack_column": True,
        },
    )


@app.post("/compose/check-now")
def compose_check_now(request: Request):
    set_running("compose")
    TRIGGER_FUNCS["compose"]()
    return _render_status(request, "compose")


@app.get("/compose/status-poll")
def compose_status_poll(request: Request, prev_running: bool = False, prev_badge_running: bool = False):
    return _render_status_poll(request, "compose", prev_running, prev_badge_running)


@app.get("/compose/partial/issues")
def compose_partial_issues(request: Request, show_silenced: bool = False, sort: str = "severity", dir: str = "asc"):
    issues = db.list_subjects_with_findings("compose", include_silenced=show_silenced)
    for issue in issues:
        issue["display_name"] = compose_lookup.subject_display_name("compose", issue["subject"])
    issues = _sort_issue_rows(issues, sort, dir)
    return templates.TemplateResponse(
        "_issues_grouped_table.html",
        {
            "request": request, "issues": issues, "source": "compose", "show_silenced": show_silenced,
            "sort": sort, "dir": dir, "is_partial": True,
        },
    )


@app.get("/compose/partial/files")
def compose_partial_files(request: Request, csort: str = "status", cdir: str = "asc"):
    items = db.all_compose_file_states_with_status()
    for item in items:
        item["display_name"] = compose_lookup.subject_display_name("compose", item["name"])
    items = _sort_status_list_rows(items, csort, cdir)
    return templates.TemplateResponse(
        "_status_list_table.html",
        {
            "request": request, "items": items, "detail_base": "/compose/file", "use_query_param": True,
            "csort": csort, "cdir": cdir, "partial_url": "/compose/partial/files",
            "target_id": "compose-files-table", "base_url": "/compose", "is_partial": True,
        },
    )


def _launch_check_if_not_running() -> None:
    """Claims the "running" slot synchronously (in this request-handling thread) so the
    click's own HTTP response deterministically reflects "running" — right now the response
    only ever came back once the whole check had already finished (set_finished had already
    been called before any render happened), so the spinner never had a chance to appear from
    the click itself; the only way to see it was to load a fresh page while an earlier click's
    check happened to still be going. Only the actual check work (registry lookups, DB writes)
    runs on a background thread, so the response doesn't wait for that part.

    Guarded against double-starts: if a check is already running (e.g. a double-click, or
    Reset & re-check fired right after Check now, or the automatic schedule firing at the same
    moment — Stage 5), try_start_updates_check() is a no-op and nothing new gets launched."""
    if not persist.try_start_updates_check():
        return
    threading.Thread(target=persist.run_claimed_updates_check, daemon=True).start()


# Lower number = more severe = sorts first under ascending Importance order (the page's
# default). Rows with no severity at all (errors, or anything AI summarization hasn't reached
# yet) are handled entirely separately in _sort_and_filter_rows below -- they're not "low
# severity," they're unclassified, and always stay pinned to the top regardless of direction.
_IMPORTANCE_RANK = {"breaking": 0, "action_needed": 1, "feature": 2, "bugfix": 3}

# Synthetic rank for "release notes could not be found" rows -- lower priority than even a real
# bugfix classification, so it sorts last among ranked rows, but (unlike a genuine unclassified
# row) it's not pinned to the top: there's nothing to investigate here, the check already ran
# and came up empty. Not part of _IMPORTANCE_RANK itself since it's not a real AI severity
# value -- see _sort_and_filter_rows for how a row earns this tier.
_NOTES_NOT_FOUND_RANK = 4


def _attach_stack_info(rows: list[dict], name_key: str) -> list[dict]:
    """Attaches stack_id/stack_name to each row (container/update record) so the table can
    show and sort by which compose stack it belongs to. Ungrouped containers (no resolvable
    compose file) get stack_name=None.

    Builds the compose index once for the whole batch of rows rather than once per row — each
    row calling its own full compose-tree scan was the actual cause of slow page loads on
    setups with many tracked containers."""
    index = compose_lookup.build_stack_index()
    name_cache: dict[str, str] = {}
    annotated = []
    for row in rows:
        d = dict(row)
        info = compose_lookup.match_container_to_stack(d[name_key], index)
        if info and len(info["service_names"]) >= 2:
            d["stack_id"] = info["stack_id"]
            if info["stack_id"] not in name_cache:
                name_cache[info["stack_id"]] = stacks.get_or_generate_stack_name(
                    info["stack_id"], info["service_names"]
                )
            d["stack_name"] = name_cache[info["stack_id"]]
        else:
            d["stack_id"] = None
            d["stack_name"] = None
        annotated.append(d)
    return annotated


def _sort_and_filter_rows(rows: list[dict], sort: str, direction: str, updates_only: bool,
                           show_silenced: bool = False) -> list[dict]:
    """Filters the persisted per-container rows (db.list_tracked_containers_with_status()) down
    to just the ones needing attention when updates_only is set, attaches stack info to every
    row (see _attach_stack_info), and sorts by whichever column was clicked.

    show_silenced only affects the Updates list (updates_only=True) -- it's a swap, not an
    additive reveal: by default only non-silenced (actionable) containers show, and toggling on
    shows ONLY silenced ones, exactly like Logs/Compose's Issues table (see
    db.list_subjects_with_findings's include_silenced). The Tracked containers list
    (updates_only=False) always shows every container regardless, silenced or not -- same as
    Logs/Compose's "All containers" table always shows everything; only the actionable list
    ever filters anything.

    Importance is the odd one out: unclassified rows (errors, or anything AI summarization
    hasn't reached) are pinned to the very top regardless of direction -- they might be
    critical issues the operator isn't aware of yet and can't be allowed to just scroll off
    the bottom on a reverse sort the way a genuinely low-severity bugfix can. Rows where a
    check ran and genuinely found no release notes are a separate, lower tier: nothing to
    investigate there, so they sort alongside real severities (below even bugfix) instead of
    being pinned to the top."""
    filtered = [r for r in rows if not updates_only or r["status"] in ("update_available", "error")]
    if updates_only:
        filtered = [r for r in filtered if bool(r.get("silenced")) == show_silenced]
    annotated = _attach_stack_info(filtered, "container_name")
    reverse = direction == "desc"

    if sort == "image":
        annotated.sort(key=lambda r: r["image_repo"].lower(), reverse=reverse)
    elif sort == "detected":
        annotated.sort(key=lambda r: r.get("created_at") or "", reverse=reverse)
    elif sort == "lastchecked":
        annotated.sort(key=lambda r: r.get("last_checked_at") or "", reverse=reverse)
    elif sort == "stack":
        annotated.sort(
            key=lambda r: (r["stack_name"] is None, (r["stack_name"] or "").lower(), r["container_name"].lower()),
            reverse=reverse,
        )
        if reverse:
            # Reversing for desc also flips "ungrouped last" to "ungrouped first" -- undo that
            # specifically, ungrouped always sorts last regardless of direction.
            grouped = [r for r in annotated if r["stack_name"] is not None]
            ungrouped = [r for r in annotated if r["stack_name"] is None]
            annotated = grouped + ungrouped
    elif sort == "importance":
        classified = [r for r in annotated if r.get("severity") in _IMPORTANCE_RANK]
        notes_not_found = [
            r for r in annotated
            if r.get("severity") not in _IMPORTANCE_RANK and not r.get("error") and not r.get("release_notes_raw")
        ]
        unclassified = [
            r for r in annotated
            if r.get("severity") not in _IMPORTANCE_RANK and (r.get("error") or r.get("release_notes_raw"))
        ]
        ranked = classified + notes_not_found
        ranked.sort(key=lambda r: _IMPORTANCE_RANK.get(r["severity"], _NOTES_NOT_FOUND_RANK), reverse=reverse)
        unclassified.sort(key=lambda r: r["container_name"].lower())
        annotated = unclassified + ranked
    elif sort == "status":
        # Needs-manual-check (error) ranks above Unread, which ranks above Read -- same tiering
        # the (currently unused by this route) SQL-level UPDATE_SORT_COLUMNS["status"] already
        # encodes. Sorted alphabetically first, then stably by rank second, so the alphabetical
        # order within each read-status group always stays A-Z regardless of which direction
        # was clicked -- only the rank grouping itself flips, matching every other column here.
        annotated.sort(key=lambda r: r["container_name"].lower())
        annotated.sort(key=lambda r: 0 if r.get("error") else (1 if r.get("read_status") == "unread" else 2), reverse=reverse)
    elif sort == "silenced":
        annotated.sort(key=lambda r: r["container_name"].lower())
        annotated.sort(key=lambda r: 0 if r.get("silenced") else 1, reverse=reverse)
    else:
        annotated.sort(key=lambda r: r["container_name"].lower(), reverse=reverse)

    return annotated


_ISSUE_SEVERITY_RANK = {"critical": 0, "warning": 1, "suggestion": 2}


def _sort_issue_rows(rows: list[dict], sort: str, direction: str) -> list[dict]:
    """Sorts the Logs/Compose "Issues" table (db.list_subjects_with_findings's rows) --
    Python-level sort over an already-small result set, same approach _sort_and_filter_rows
    uses for Updates, rather than pushing sorting into SQL for what's realistically a few dozen
    rows at most."""
    reverse = direction == "desc"
    name_key = lambda r: (r.get("display_name") or r["subject"]).lower()  # noqa: E731
    if sort == "findings":
        return sorted(rows, key=lambda r: r["active_count"], reverse=reverse)
    if sort == "severity":
        return sorted(rows, key=lambda r: _ISSUE_SEVERITY_RANK.get(r.get("top_severity"), 99), reverse=reverse)
    if sort == "lastseen":
        return sorted(rows, key=lambda r: r.get("last_seen_at") or "", reverse=reverse)
    if sort == "unread":
        return sorted(rows, key=lambda r: r.get("unread_count") or 0, reverse=reverse)
    if sort == "stack":
        # Same "ungrouped always sorts last regardless of direction" tie-breaking as
        # _sort_and_filter_rows/_sort_status_list_rows' own stack sorts.
        annotated = sorted(
            rows, key=lambda r: (r.get("stack_name") is None, (r.get("stack_name") or "").lower(), name_key(r)),
            reverse=reverse,
        )
        if reverse:
            grouped = [r for r in annotated if r.get("stack_name") is not None]
            ungrouped = [r for r in annotated if r.get("stack_name") is None]
            return grouped + ungrouped
        return annotated
    return sorted(rows, key=name_key, reverse=reverse)


def _sort_status_list_rows(rows: list[dict], sort: str, direction: str) -> list[dict]:
    """Sorts the Logs/Compose "All containers"/"All files" table (db.all_log_watch_states_
    with_status / db.all_compose_file_states_with_status's rows)."""
    reverse = direction == "desc"
    name_key = lambda r: (r.get("display_name") or r["name"]).lower()  # noqa: E731
    if sort == "lastchecked":
        return sorted(rows, key=lambda r: r.get("last_at") or "", reverse=reverse)
    if sort == "status":
        _status_rank = {"error": 0, "issue": 1, "healthy": 2}
        return sorted(rows, key=lambda r: _status_rank.get(r["status"], 3), reverse=reverse)
    if sort == "stack":
        # Same "ungrouped always sorts last regardless of direction" tie-breaking as Updates'
        # own stack sort (_sort_and_filter_rows) -- rows must already carry stack_name/stack_id
        # (see _attach_stack_info), called by the route before this, same as Updates does.
        annotated = sorted(
            rows,
            key=lambda r: (r.get("stack_name") is None, (r.get("stack_name") or "").lower(), name_key(r)),
            reverse=reverse,
        )
        if reverse:
            grouped = [r for r in annotated if r.get("stack_name") is not None]
            ungrouped = [r for r in annotated if r.get("stack_name") is None]
            return grouped + ungrouped
        return annotated
    if sort == "silenced":
        # "silenced" ranks above "partially_silenced" ranks above unsilenced/None -- same
        # severity-style tiering _ISSUE_SEVERITY_RANK uses, just for the 3-state silence model.
        _rank = {"silenced": 0, "partially_silenced": 1}
        annotated = sorted(rows, key=name_key)
        return sorted(annotated, key=lambda r: _rank.get(r.get("silence_state"), 2), reverse=reverse)
    return sorted(rows, key=name_key, reverse=reverse)


def _findings_summary(findings: list[dict]) -> dict:
    """Aggregate stats for a subject's finding list -- active/silenced counts and the most
    severe finding's severity -- used to render subject_findings.html's title-row badges the
    same way detail.html's severity + Read/Unread badges summarize a single update.
    top_severity prefers active findings, but falls back to ALL findings (silenced included)
    when nothing's active -- a container that was critical and then got silenced should still
    read as critical, not lose its classification just because it's quiet now (matching
    _issues_grouped_table.html's top_severity, which is computed the same way at the SQL
    level -- see list_subjects_with_findings)."""
    active = [f for f in findings if f["status"] == "active"]
    severity_pool = active or findings
    top_severity = min(
        (f["severity"] for f in severity_pool), key=lambda s: _ISSUE_SEVERITY_RANK.get(s, 99), default=None
    )
    return {
        "active_count": len(active),
        "silenced_count": len(findings) - len(active),
        "unread_count": sum(1 for f in active if f["read_status"] == "unread"),
        "top_severity": top_severity,
    }


def _sort_subject_findings(findings: list[dict], sort: str, direction: str) -> list[dict]:
    """Sorts subject_findings.html's per-finding table (one subject's own findings, Logs or
    Compose) -- a small, single-subject result set, so plain full-page sort links are enough
    here (see _sort_header.html's macro used without partial_url/target_id), unlike the
    self-refreshing htmx tables elsewhere. Default ("severity", asc) matches the Issues table
    and every other findings table in the app -- most-severe-first, not most-recently-seen."""
    reverse = direction == "desc"
    if sort == "title":
        return sorted(findings, key=lambda f: f["title"].lower(), reverse=reverse)
    if sort == "category":
        return sorted(findings, key=lambda f: f["category"].lower(), reverse=reverse)
    if sort == "severity":
        return sorted(findings, key=lambda f: _ISSUE_SEVERITY_RANK.get(f["severity"], 99), reverse=reverse)
    if sort == "silenced":
        return sorted(findings, key=lambda f: 0 if f["status"] == "silenced" else 1, reverse=reverse)
    if sort == "read":
        return sorted(findings, key=lambda f: 0 if f["read_status"] == "unread" else 1, reverse=reverse)
    return sorted(findings, key=lambda f: f["last_seen_at"], reverse=reverse)


def _sort_stack_members(members: list[dict], sort: str, direction: str) -> list[dict]:
    """Sorts logs_stack_detail.html's per-stack members table -- a small, single-stack result
    set, so plain full-page sort links are enough here (see _sort_header.html's macro used
    without partial_url/target_id), same as _sort_subject_findings. Default ("severity", asc)
    matches the Issues table and every other findings table in the app."""
    reverse = direction == "desc"
    if sort == "findings":
        return sorted(members, key=lambda m: m["active_count"], reverse=reverse)
    if sort == "detected":
        return sorted(members, key=lambda m: m.get("last_checked_at") or "", reverse=reverse)
    if sort == "severity":
        return sorted(members, key=lambda m: _ISSUE_SEVERITY_RANK.get(m.get("top_severity"), 99), reverse=reverse)
    if sort == "read":
        return sorted(members, key=lambda m: m.get("unread_count") or 0, reverse=reverse)
    return sorted(members, key=lambda m: m["container_name"].lower(), reverse=reverse)


def _get_or_build_overview(source: str, subject: str, display_name: str, findings, force: bool = False) -> str | None:
    """Combined AI overview shown above a subject's findings list. Cached by a hash of the
    current finding set so it's only regenerated (costing an API call) when something about
    the findings actually changes, not on every page view. Never called for 0 or 1 findings —
    those cases either show nothing or get redirected straight to the single finding.

    force=True (the service-level and bulk Regenerate AI Response buttons) bypasses the
    content-hash cache and always calls the AI fresh, same "an explicit click always gets a
    fresh take" semantics as every other Regenerate button in the app."""
    if len(findings) < 2:
        return None

    fingerprint_input = "|".join(sorted(f"{f['id']}:{f['title']}:{f['status']}" for f in findings))
    findings_hash = hashlib.sha256(fingerprint_input.encode()).hexdigest()[:16]

    cached = db.get_subject_summary(source, subject)
    if not force and cached and cached["findings_hash"] == findings_hash:
        return cached["summary_markdown"]

    try:
        summary = summarize_findings_overview(display_name, [dict(f) for f in findings])
    except Exception:
        logger.exception("Findings overview generation failed for %s:%s", source, subject)
        return cached["summary_markdown"] if cached else None

    db.set_subject_summary(source, subject, findings_hash, summary)
    return summary


# ---------------------------------------------------------------------------
# Settings (schedules + notifications)
# ---------------------------------------------------------------------------

VALID_SCOPES = ("master", "updates", "logs", "compose")
VALID_FEATURES = ("updates", "logs", "compose")

# Computed once at import time rather than per-request — available_timezones() scans the
# system's IANA zone database, which doesn't change while the process is running.
AVAILABLE_TIMEZONES = sorted(available_timezones())

# Curated rather than exhaustive -- a short, known-good list per provider beats a free-text
# field nobody can be expected to get exactly right, and beats an exhaustive list that goes
# stale the moment either vendor ships a new model.
ANTHROPIC_MODELS = [
    ("claude-sonnet-5", "Claude Sonnet 5 (recommended)"),
    ("claude-opus-4-8", "Claude Opus 4.8 (highest quality, slower/costlier)"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5 (fastest, cheapest)"),
]
GEMINI_MODELS = [
    ("gemini-2.5-flash", "Gemini 2.5 Flash (recommended)"),
    ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite (fastest, cheapest)"),
    ("gemini-2.5-pro", "Gemini 2.5 Pro (highest quality, slower/costlier)"),
]


def _int_field(form, name: str, default: int) -> int:
    try:
        return int(form.get(name, default) or default)
    except (TypeError, ValueError):
        return default


def _spec_from_form(form, scope: str) -> dict:
    """Builds a schedule_spec.py dict from the Hourly/Daily/Weekly/Monthly picker's POSTed
    fields. Only the currently-selected mode's fields are ever enabled client-side (see
    updateScheduleVisibility() in settings.html), so disabled fields never make it into the
    form data — every mode can safely share one {scope}_time field name rather than needing
    per-mode suffixes, since at most one is ever actually submitted."""
    mode = form.get(f"{scope}_mode", "daily")

    if mode == "hourly":
        interval = max(1, min(23, _int_field(form, f"{scope}_interval_hours", 4)))
        start_hour = max(0, min(23, _int_field(form, f"{scope}_start_hour", 0)))
        return {"mode": "hourly", "interval_hours": interval, "start_hour": start_hour}

    time_str = form.get(f"{scope}_time", "06:00") or "06:00"
    try:
        hour, minute = (int(x) for x in time_str.split(":"))
    except (ValueError, TypeError):
        hour, minute = 6, 0

    if mode == "weekly":
        posted_days = form.getlist(f"{scope}_days_of_week")
        days = [d for d in DAY_NAMES if d in posted_days] or ["mon"]
        return {"mode": "weekly", "days_of_week": days, "hour": hour, "minute": minute}

    if mode == "monthly":
        day = max(1, min(31, _int_field(form, f"{scope}_day_of_month", 1)))
        return {"mode": "monthly", "day_of_month": day, "hour": hour, "minute": minute}

    return {"mode": "daily", "hour": hour, "minute": minute}


def _build_notify_context() -> dict:
    return {
        "enabled": db.get_notifications_enabled(),
        "apprise_urls": ", ".join(db.get_apprise_urls()),
        "updates_include_errors": db.get_notify_updates_include_errors(),
        "logs_include_errors": db.get_notify_logs_include_errors(),
        "compose_include_errors": db.get_notify_compose_include_errors(),
        "features": {
            feature: {
                "enabled": db.get_feature_notify_enabled(feature),
                "severity": db.get_feature_severity(feature),
            }
            for feature in ("updates", "logs", "compose")
        },
    }


@app.get("/settings")
def settings_page(request: Request):
    master = db.get_master_schedule()
    features = {
        feature: {
            "use_master": db.get_feature_uses_master_schedule(feature),
            "spec": db.get_feature_schedule(feature),
        }
        for feature in ("updates", "logs", "compose")
    }
    deep_analysis = {
        feature: db.get_deep_analysis_enabled(feature) for feature in ("logs", "compose", "updates")
    }
    cross_service_analysis = {
        feature: db.get_cross_service_analysis_enabled(feature) for feature in ("updates", "logs")
    }
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request, "master": master, "features": features,
            "describe": describe_schedule, "notify": _build_notify_context(),
            "deep_analysis": deep_analysis, "cross_service_analysis": cross_service_analysis,
            "update_severities": list(UPDATE_SEVERITIES),
            "release_notes_lookback": db.get_release_notes_lookback(),
            "timezone": db.get_timezone(), "available_timezones": AVAILABLE_TIMEZONES,
            "ai_provider": db.get_ai_provider(),
            "anthropic_key_configured": bool(db.get_anthropic_api_key()),
            "anthropic_model": db.get_anthropic_model(),
            "anthropic_models": ANTHROPIC_MODELS,
            "gemini_key_configured": bool(db.get_gemini_api_key()),
            "gemini_model": db.get_gemini_model(),
            "gemini_models": GEMINI_MODELS,
            "github_token_configured": bool(db.get_github_token()),
            "active_tab": "settings",
        },
    )


def _saved(request: Request):
    return templates.TemplateResponse("_saved_indicator.html", {"request": request})


@app.post("/settings/timezone")
async def save_timezone(request: Request):
    form = await request.form()
    tz = (form.get("timezone") or "").strip()
    if tz not in AVAILABLE_TIMEZONES:
        raise HTTPException(status_code=400, detail="Unknown timezone")
    db.set_timezone(tz)
    # Re-applies immediately so already-scheduled jobs reinterpret their times in the new
    # zone right away, rather than only taking effect after the next restart.
    apply_schedules()
    return _saved(request)


@app.post("/settings/deep-analysis/{feature}")
async def save_deep_analysis(feature: str, request: Request):
    if feature not in ("logs", "compose", "updates"):
        raise HTTPException(status_code=404)
    form = await request.form()
    db.set_deep_analysis_enabled(feature, form.get("enabled") == "on")
    return {"status": "ok"}


@app.post("/settings/cross-service-analysis/{feature}")
async def save_cross_service_analysis(feature: str, request: Request):
    if feature not in ("updates", "logs"):
        raise HTTPException(status_code=404)
    form = await request.form()
    db.set_cross_service_analysis_enabled(feature, form.get("enabled") == "on")
    return {"status": "ok"}


@app.post("/settings/release-notes-lookback")
async def save_release_notes_lookback(request: Request):
    form = await request.form()
    value = form.get("release_notes_lookback", "since_check")
    if value not in db.RELEASE_NOTES_LOOKBACK_DAYS:
        raise HTTPException(status_code=400, detail="Unknown lookback value")
    db.set_release_notes_lookback(value)
    return _saved(request)


@app.post("/settings/ai/provider")
async def save_ai_provider(request: Request):
    form = await request.form()
    provider = form.get("ai_provider", "")
    if provider not in ("anthropic", "gemini"):
        raise HTTPException(status_code=400, detail="Unknown provider")
    db.set_ai_provider(provider)
    return _saved(request)


@app.post("/settings/ai/anthropic-key")
async def save_anthropic_key(request: Request):
    form = await request.form()
    key = (form.get("api_key") or "").strip()
    if not key:
        return {"ok": False, "message": "Enter a key first."}
    ok, message = ai_provider.test_anthropic_key(key)
    if ok:
        db.set_anthropic_api_key(key)
    return {"ok": ok, "message": message}


@app.post("/settings/ai/anthropic-model")
async def save_anthropic_model(request: Request):
    form = await request.form()
    model = form.get("anthropic_model", "")
    if model not in dict(ANTHROPIC_MODELS):
        raise HTTPException(status_code=400, detail="Unknown model")
    db.set_anthropic_model(model)
    return _saved(request)


@app.post("/settings/ai/gemini-key")
async def save_gemini_key(request: Request):
    form = await request.form()
    key = (form.get("api_key") or "").strip()
    if not key:
        return {"ok": False, "message": "Enter a key first."}
    ok, message = ai_provider.test_gemini_key(key)
    if ok:
        db.set_gemini_api_key(key)
    return {"ok": ok, "message": message}


@app.post("/settings/ai/github-token")
async def save_github_token(request: Request):
    form = await request.form()
    token = (form.get("api_key") or "").strip()
    if not token:
        return {"ok": False, "message": "Enter a token first."}
    ok, message = release_notes.test_github_token(token)
    if ok:
        db.set_github_token(token)
    return {"ok": ok, "message": message}


@app.post("/settings/ai/gemini-model")
async def save_gemini_model(request: Request):
    form = await request.form()
    model = form.get("gemini_model", "")
    if model not in dict(GEMINI_MODELS):
        raise HTTPException(status_code=400, detail="Unknown model")
    db.set_gemini_model(model)
    return _saved(request)


@app.post("/settings/schedule/{scope}")
async def save_schedule(scope: str, request: Request):
    if scope not in VALID_SCOPES:
        raise HTTPException(status_code=404)
    form = await request.form()
    spec = _spec_from_form(form, scope)
    if scope == "master":
        db.set_master_schedule(spec)
    else:
        db.set_feature_schedule(scope, spec)
    apply_schedules()
    return _saved(request)


@app.post("/settings/schedule/use-master/{feature}")
async def save_schedule_use_master(feature: str, request: Request):
    """The checkbox this saves means "use my own schedule" (checked = own) -- the opposite of
    the stored use_master flag (True = defers to the general schedule), so what's submitted is
    inverted before it's written. See settings.html's toggleScheduleOverride for the matching
    client-side inversion."""
    if feature not in VALID_FEATURES:
        raise HTTPException(status_code=404)
    form = await request.form()
    db.set_feature_uses_master_schedule(feature, form.get("enabled") != "on")
    apply_schedules()
    return _saved(request)


@app.post("/settings/notify/enabled/{scope}")
async def save_notify_enabled(scope: str, request: Request):
    if scope not in VALID_SCOPES:
        raise HTTPException(status_code=404)
    form = await request.form()
    enabled = form.get("enabled") == "on"
    if scope == "master":
        db.set_notifications_enabled(enabled)
    else:
        db.set_feature_notify_enabled(scope, enabled)
    return _saved(request)


@app.post("/settings/notify/severity/{feature}")
async def save_notify_severity(feature: str, request: Request):
    """The posted field is named "{feature}_severity", not a shared "severity" -- when Updates,
    Logs, and Compose's radio groups all shared the literal name "severity" (a real-world report
    traced back to this), the browser enforced radio exclusivity across ALL of them together,
    silently unchecking every group but whichever rendered last. Scoping the name per feature
    (see _severity_buttons.html) fixes that; this reads the matching scoped field."""
    if feature not in VALID_FEATURES:
        raise HTTPException(status_code=404)
    form = await request.form()
    valid_values = UPDATE_SEVERITIES if feature == "updates" else FINDING_SEVERITIES
    default_value = "bugfix" if feature == "updates" else "suggestion"
    severity = form.get(f"{feature}_severity", default_value)
    if severity not in valid_values:
        severity = default_value
    db.set_feature_severity(feature, severity)
    return _saved(request)


@app.post("/settings/notify/updates-include-errors")
async def save_notify_updates_include_errors(request: Request):
    form = await request.form()
    db.set_notify_updates_include_errors(form.get("enabled") == "on")
    return _saved(request)


@app.post("/settings/notify/logs-include-errors")
async def save_notify_logs_include_errors(request: Request):
    form = await request.form()
    db.set_notify_logs_include_errors(form.get("enabled") == "on")
    return _saved(request)


@app.post("/settings/notify/compose-include-errors")
async def save_notify_compose_include_errors(request: Request):
    form = await request.form()
    db.set_notify_compose_include_errors(form.get("enabled") == "on")
    return _saved(request)


def _normalize_apprise_url(url: str) -> str:
    """Discord webhook URLs need ?format=markdown for Apprise to build a colored embed at all
    (see notifications.py's own module docstring) -- a bare discord:// URL otherwise falls back
    to a flat plain-text message with no severity color. Appended automatically here so the
    user never has to remember to type it themselves, the same way an email signup form fills
    in "@example.com" after whatever you type. Every other Apprise-supported service's URL
    passes through untouched -- this only ever touches a discord:// URL with no query string
    of its own yet."""
    if url.startswith("discord://") and "?" not in url:
        return url + "?format=markdown"
    return url


@app.post("/settings/notify/apprise-test")
async def test_apprise(request: Request):
    form = await request.form()
    raw = form.get("apprise_urls", "") or ""
    urls = [_normalize_apprise_url(u.strip()) for u in raw.replace("\n", ",").split(",") if u.strip()]
    success, message = send_test_notification(urls=urls)
    if success:
        # Only persist the URL once it's actually proven to work — an unsaved textarea
        # that hasn't been tested (or failed its test) never gets written to the database.
        # Saves the normalized form (with ?format=markdown already appended) so what's tested
        # is exactly what's saved and reused on every real notification from then on.
        db.set_apprise_urls(", ".join(urls))
    css_class = "test-result-ok" if success else "test-result-error"
    return templates.TemplateResponse(
        "_test_notification_result.html", {"request": request, "message": message, "css_class": css_class}
    )


# ---------------------------------------------------------------------------
# Global Reset & re-check — wipes all persisted Updates history/tracking state, then runs a
# fresh check. Real persistence exists as of Stage 3 (see app/persist.py, db.reset_updates_data),
# so this is now a genuine, permanent action rather than the Stage 1 placeholder it used to be.
# ---------------------------------------------------------------------------

@app.post("/updates/reset-and-recheck")
def reset_and_recheck_updates():
    db.reset_updates_data()
    _launch_check_if_not_running()
    return RedirectResponse(url="/updates", status_code=303)


@app.post("/updates/regenerate-all")
def regenerate_all_updates():
    """The main Updates page's bulk "Regenerate AI Response" -- reuses the same claimed-mutex
    pattern as reset-and-recheck above so it can't overlap with a real check or another
    regenerate run; the actual fan-out lives in persist.run_claimed_bulk_regenerate."""
    if persist.try_start_updates_check():
        threading.Thread(target=persist.run_claimed_bulk_regenerate, daemon=True).start()
    return RedirectResponse(url="/updates", status_code=303)


# ---------------------------------------------------------------------------
# Logs' own global Reset & re-check / bulk Regenerate AI Response -- same shape as Updates'
# pair above, adapted for Logs' checkpoint-based (not digest-based) architecture: "reset" means
# wiping findings + the per-container checkpoint so the next check re-scans a fresh lookback
# window, and "regenerate" means force-refreshing every AI-written blurb already on the Logs
# pages (each subject's overview + every qualifying stack's Cross-Service Analysis blurb)
# rather than re-triaging live logs again, since there's no separately-stored "raw" text to
# resummarize a finding from the way Updates has release_notes_raw.
# ---------------------------------------------------------------------------

@app.post("/logs/reset-and-recheck")
def reset_and_recheck_logs():
    db.reset_logs_data()
    set_running("logs")
    TRIGGER_FUNCS["logs"]()
    return RedirectResponse(url="/logs", status_code=303)


def _run_claimed_logs_bulk_regenerate() -> None:
    """The main Logs page's bulk "Regenerate AI Response" -- force-regenerates every subject's
    cached overview blurb (for subjects with 2+ findings) and every qualifying stack's
    Cross-Service Analysis blurb, bypassing their content-hash caches, same "an explicit click
    always gets a fresh take" semantics as every other Regenerate button in the app. Claims the
    Logs mutex so it can't overlap with a real check or another scoped action."""
    try:
        container_names = [row["name"] for row in db.all_log_watch_states_with_status()]
        for name in container_names:
            findings = db.list_findings_for_subject("logs", name, include_silenced=True)
            if len(findings) >= 2:
                _get_or_build_overview("logs", name, name, findings, force=True)
        stacks.run_log_stack_analysis_pass(container_names, force=True)
    except Exception:
        logger.exception("Bulk Regenerate AI Response failed unexpectedly for Logs")
    finally:
        check_state.release_running("logs")


@app.post("/logs/regenerate-all")
def regenerate_all_logs():
    if check_state.try_start("logs"):
        threading.Thread(target=_run_claimed_logs_bulk_regenerate, daemon=True).start()
    return RedirectResponse(url="/logs", status_code=303)


# ---------------------------------------------------------------------------
# Compose's own global Reset & re-check / bulk Regenerate AI Response -- same shape as Logs'
# pair above, minus the Cross-Service Analysis pass (Compose has no stack concept, see
# db.reset_compose_data's docstring): "reset" means wiping findings + the per-file content-hash
# checkpoint so the next check reviews every file fresh regardless of whether it actually
# changed, and "regenerate" means force-refreshing every file's already-cached AI overview
# blurb rather than re-reviewing the file's config again.
# ---------------------------------------------------------------------------

@app.post("/compose/reset-and-recheck")
def reset_and_recheck_compose():
    db.reset_compose_data()
    set_running("compose")
    TRIGGER_FUNCS["compose"]()
    return RedirectResponse(url="/compose", status_code=303)


def _run_claimed_compose_bulk_regenerate() -> None:
    """The main Compose page's bulk "Regenerate AI Response" -- force-regenerates every file's
    cached overview blurb (for files with 2+ findings), bypassing its content-hash cache, same
    "an explicit click always gets a fresh take" semantics as every other Regenerate button in
    the app. Claims the Compose mutex so it can't overlap with a real check or another scoped
    action."""
    try:
        file_paths = [row["name"] for row in db.all_compose_file_states_with_status()]
        for path in file_paths:
            findings = db.list_findings_for_subject("compose", path, include_silenced=True)
            if len(findings) >= 2:
                display_name = compose_lookup.subject_display_name("compose", path)
                _get_or_build_overview("compose", path, display_name, findings, force=True)
    except Exception:
        logger.exception("Bulk Regenerate AI Response failed unexpectedly for Compose")
    finally:
        check_state.release_running("compose")


@app.post("/compose/regenerate-all")
def regenerate_all_compose():
    if check_state.try_start("compose"):
        threading.Thread(target=_run_claimed_compose_bulk_regenerate, daemon=True).start()
    return RedirectResponse(url="/compose", status_code=303)


def _emphasize_stack_mentions(text: str, service_names: list[str]) -> str:
    """Bolds exact mentions of a stack's own service names within the analysis text, purely to
    make them easier to pick out while reading -- these used to be jump-links to that service's
    row further down the same page, which was pointless since the table with all of them is
    already right there, visible, on the same page load. Runs on the raw markdown before
    rendering, since inline HTML passes through markdown.markdown() unescaped. Longest names
    are matched first in one combined pass so a shorter name that happens to be a substring
    of a longer one (rare, but possible) can't steal part of the match."""
    if not text or not service_names:
        return text
    names_sorted = sorted(set(service_names), key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in names_sorted) + r")\b")
    return pattern.sub(lambda m: f'<strong>{m.group(1)}</strong>', text)


@app.get("/updates/stack")
def stack_detail(request: Request, id: str):
    stack_row = db.get_stack(id)
    member_names = stacks.stack_member_names(id)
    display_name = stack_row["display_name"] if stack_row else (member_names[0] if member_names else "Unknown stack")

    # The blurb (and the button that regenerates it) only ever make sense with the toggle on --
    # showing a stale blurb (or a working button) while it's off would misrepresent a feature
    # the operator has explicitly opted out of as still active.
    deep_analysis_enabled = db.get_cross_service_analysis_enabled("updates")
    analysis_row = db.get_stack_analysis(id, source="updates") if deep_analysis_enabled else None
    analysis_html = None
    if analysis_row:
        emphasized_text = _emphasize_stack_mentions(analysis_row["analysis_markdown"], member_names)
        analysis_html = render_markdown(emphasized_text)

    members = []
    for name in member_names:
        container_row = db.get_container_state(name)
        latest_update = db.get_latest_update_for_container(name)
        members.append({
            "container_name": name,
            "image_repo": container_row["image_repo"] if container_row else "",
            "tag": container_row["tag"] if container_row else "",
            "latest_update": dict(latest_update) if latest_update else None,
        })

    return templates.TemplateResponse(
        "stack_detail.html",
        {
            "request": request, "stack_id": id, "display_name": display_name,
            "members": members, "deep_analysis_enabled": deep_analysis_enabled,
            "analysis_html": analysis_html, "active_tab": "updates",
        },
    )


def _stack_return_url(form, stack_id: str) -> str:
    """A stack's identity (and its name) is shared across whichever feature is looking at it
    (see stacks.py -- stack_id is just the compose file's own path), so the same rename/reset-
    name routes serve the Updates stack page and the Logs stack page alike. return_to says
    which one to bounce back to -- checked against an allowlist of the two real prefixes rather
    than trusted outright, since it's attacker-influenceable form data."""
    return_to = form.get("return_to", "")
    if return_to.startswith("/logs/stack?id="):
        return f"/logs/stack?id={quote(stack_id)}"
    return f"/updates/stack?id={quote(stack_id)}"


@app.post("/updates/stack/rename")
async def rename_stack_route(request: Request):
    form = await request.form()
    stack_id = form.get("stack_id", "")
    name = (form.get("name") or "").strip()
    if stack_id and name:
        stacks.rename_stack(stack_id, name)
    return RedirectResponse(url=_stack_return_url(form, stack_id), status_code=303)


@app.post("/updates/stack/reset-name")
async def reset_stack_name_route(request: Request):
    form = await request.form()
    stack_id = form.get("stack_id", "")
    if stack_id:
        stacks.reset_stack_name(stack_id)
        # Regenerate immediately rather than leaving it nameless until the next check.
        index = compose_lookup.build_stack_index()
        for entry in index:
            if entry["stack_id"] == stack_id:
                stacks.get_or_generate_stack_name(stack_id, entry["service_names"])
                break
    return RedirectResponse(url=_stack_return_url(form, stack_id), status_code=303)


@app.post("/compose/file/rename")
async def rename_compose_file_route(request: Request):
    """Compose's counterpart to /updates/stack/rename above -- simpler than that route pair
    since Compose has only the one detail page (no return_to dance needed) and no AI-generated
    name to preserve a services_hash for (see compose_files' own schema comment)."""
    form = await request.form()
    path = form.get("path", "")
    name = (form.get("name") or "").strip()
    if path and name:
        db.set_compose_file_name(path, name, "manual")
    return RedirectResponse(url=f"/compose/file?path={quote(path)}", status_code=303)


@app.post("/compose/file/reset-name")
async def reset_compose_file_name_route(request: Request):
    form = await request.form()
    path = form.get("path", "")
    if path:
        db.reset_compose_file_name(path)
    return RedirectResponse(url=f"/compose/file?path={quote(path)}", status_code=303)


@app.post("/updates/stack/check-now")
def check_now_stack_route(request: Request, stack_id: str = ""):
    """Non-destructive scoped re-check for every member of this stack: re-checks each one
    (digest + release notes if something changed), only touching a member's row if its digest
    actually moved -- exactly like every other "Check now" in the app, hence no confirmation
    dialog on the button. Mirrors retry_stack_route/reset_and_recheck_stack_route below."""
    if not stack_id:
        raise HTTPException(status_code=400, detail="stack_id is required")
    return _launch_scoped_stack_check(
        request, stack_id,
        lambda item_key: persist.run_claimed_stack_check_now(item_key, stack_id),
    )


@app.post("/updates/stack/retry")
def retry_stack_route(request: Request, stack_id: str = ""):
    """Force-regenerates this stack's cross-service analysis blurb, bypassing the content-hash
    cache regardless of whether anything's actually changed since the last one -- same
    "an explicit click always regenerates" semantics as the per-update Regenerate AI Response
    button. Runs on a background thread with a live spinner via _launch_scoped_stack_check,
    same shape as every per-item action -- a real AI call here can take several seconds, and
    routing through the shared mutex means this can't race a full check's own automatic
    regeneration of the same stack's analysis row."""
    if not stack_id:
        raise HTTPException(status_code=400, detail="stack_id is required")
    return _launch_scoped_stack_check(
        request, stack_id,
        lambda item_key: persist.run_claimed_stack_retry(item_key, stack_id),
    )


@app.post("/updates/stack/reset-and-recheck")
def reset_and_recheck_stack_route(request: Request, stack_id: str = ""):
    """Stack-scoped equivalent of the per-item Reset & re-check: wipes and re-checks every
    service belonging to this stack, and no others, then force-regenerates the stack's
    cross-service analysis on top if Deep Analysis is on (see
    persist.run_claimed_stack_reset_and_recheck). Runs on a background thread with a live
    spinner via _launch_scoped_stack_check, same shape as every per-item action."""
    if not stack_id:
        raise HTTPException(status_code=400, detail="stack_id is required")
    return _launch_scoped_stack_check(
        request, stack_id,
        lambda item_key: persist.run_claimed_stack_reset_and_recheck(item_key, stack_id),
    )


@app.get("/updates/stack/status-poll")
def stack_status_poll(request: Request, stack_id: str):
    """Polling counterpart to update_recheck_status_poll below, for a stack-scoped action --
    same "still running -> re-arm the poller, finished -> redirect" shape, just always
    redirecting back to this same stack (a stack's own URL never moves the way a per-update
    action's id can)."""
    item_key = _stack_item_key(stack_id)
    item = check_state.get_item_state(item_key)

    if item is not None and item["running"]:
        return templates.TemplateResponse(
            "_stack_item_status_poll.html",
            {"request": request, "stack_id": stack_id, "item": item, "progress_text": _progress_text(item)},
        )

    check_state.clear_item(item_key)
    resp = templates.TemplateResponse(
        "_stack_item_status_poll.html",
        {"request": request, "stack_id": stack_id, "item": None, "progress_text": ""},
    )
    resp.headers["HX-Redirect"] = f"/updates/stack?id={quote(stack_id)}"
    return resp


# ---------------------------------------------------------------------------
# Tab pages
# ---------------------------------------------------------------------------

@app.get("/updates")
def updates_page(request: Request, sort: str = "importance", dir: str = "asc",
                  csort: str = "container", cdir: str = "asc", show_silenced: bool = False):
    rows = db.list_tracked_containers_with_status()
    updates = _sort_and_filter_rows(rows, sort, dir, updates_only=True, show_silenced=show_silenced)
    containers = _sort_and_filter_rows(rows, csort, cdir, updates_only=False)
    return templates.TemplateResponse(
        "updates.html",
        {
            **_status_context(request, "updates"),
            "updates": updates, "containers": containers,
            "updates_count": len(updates), "containers_count": len(containers),
            "sort": sort, "dir": dir, "csort": csort, "cdir": cdir, "show_silenced": show_silenced,
            "active_tab": "updates",
        },
    )


@app.get("/logs")
def logs_page(request: Request, show_silenced: bool = False,
              sort: str = "severity", dir: str = "asc", csort: str = "status", cdir: str = "asc"):
    issues = _attach_stack_info(db.list_subjects_with_findings("logs", include_silenced=show_silenced), "subject")
    issues = _sort_issue_rows(issues, sort, dir)
    containers = _attach_stack_info(db.all_log_watch_states_with_status(), "name")
    containers = _sort_status_list_rows(containers, csort, cdir)
    return templates.TemplateResponse(
        "logs.html",
        {
            **_status_context(request, "logs"),
            "issues": issues, "containers": containers, "show_silenced": show_silenced,
            "sort": sort, "dir": dir, "csort": csort, "cdir": cdir,
            "active_tab": "logs", "show_stack_column": True,
        },
    )


@app.get("/logs/container/{container_name}")
def logs_container_detail(request: Request, container_name: str, sort: str = "severity", dir: str = "asc"):
    # Always shows every finding for this container, active and silenced alike -- unlike the
    # Issues table (where hiding silenced rows keeps the list focused on what's actionable),
    # once you've drilled into one specific container there's no reason to hide part of its
    # own history, so the show/hide silenced toggle that used to live on this page is gone.
    findings = db.list_findings_for_subject("logs", container_name, include_silenced=True)

    if len(findings) == 1:
        return RedirectResponse(url=f"/findings/{findings[0]['id']}", status_code=303)

    overview = _get_or_build_overview("logs", container_name, container_name, findings)
    overview_html = render_markdown(overview) if overview else None
    summary = _findings_summary(findings)
    stack_info = compose_lookup.get_stack_info(container_name)
    stack_id = stack_info["stack_id"] if stack_info and len(stack_info["service_names"]) >= 2 else None
    return templates.TemplateResponse(
        "subject_findings.html",
        {
            "request": request, "findings": _sort_subject_findings(findings, sort, dir),
            "display_name": container_name, "subject": container_name,
            "back_url": "/logs", "overview_html": overview_html, "source": "logs",
            **summary,
            "silence_state": _silence_state(summary["active_count"], summary["silenced_count"]),
            "last_checked_at": db.get_log_watch_checkpoint(container_name),
            "stack_id": stack_id, "sort": sort, "dir": dir,
            "sort_base_url": f"/logs/container/{container_name}", "sort_extra_qs": "",
            "active_tab": "logs",
        },
    )


@app.get("/logs/stack")
def logs_stack_detail(request: Request, id: str, sort: str = "severity", dir: str = "asc"):
    """Logs' equivalent of /updates/stack -- groups every log-watched container belonging to
    the same compose stack onto one page, each row summarized the same way a row in the Issues
    table is (top severity, active/silenced counts, an aggregate unread indicator). Stack
    identity/naming is shared with Updates (see stacks.py), so a name set from either page
    shows on both."""
    stack_row = db.get_stack(id)
    member_names = stacks.stack_member_names_for_logs(id)
    display_name = stack_row["display_name"] if stack_row else (member_names[0] if member_names else "Unknown stack")

    # Same "the blurb and its button only ever make sense with the toggle on" reasoning as
    # Updates' stack page -- see stack_detail() above.
    cross_service_enabled = db.get_cross_service_analysis_enabled("logs")
    analysis_row = db.get_stack_analysis(id, source="logs") if cross_service_enabled else None
    analysis_html = None
    if analysis_row:
        emphasized_text = _emphasize_stack_mentions(analysis_row["analysis_markdown"], member_names)
        analysis_html = render_markdown(emphasized_text)

    members = []
    active_total = 0
    silenced_total = 0
    for name in member_names:
        findings = db.list_findings_for_subject("logs", name, include_silenced=True)
        summary = _findings_summary(findings)
        active_total += summary["active_count"]
        silenced_total += summary["silenced_count"]
        members.append({
            "container_name": name,
            "last_checked_at": db.get_log_watch_checkpoint(name),
            **summary,
        })

    return templates.TemplateResponse(
        "logs_stack_detail.html",
        {
            "request": request, "stack_id": id, "display_name": display_name,
            "members": _sort_stack_members(members, sort, dir), "active_tab": "logs",
            "cross_service_enabled": cross_service_enabled, "analysis_html": analysis_html,
            "silence_state": _silence_state(active_total, silenced_total),
            "sort": sort, "dir": dir,
            "sort_base_url": "/logs/stack", "sort_extra_qs": "&id=" + quote(id),
        },
    )


@app.post("/logs/stack/retry")
def retry_log_stack_route(request: Request, stack_id: str = ""):
    """Force-regenerates this Logs stack's cross-service analysis blurb, bypassing the
    content-hash cache -- same "an explicit click always regenerates" semantics as Updates'
    stack Retry button. Runs on a background thread with a live spinner via
    _launch_scoped_log_stack_check."""
    if not stack_id:
        raise HTTPException(status_code=400, detail="stack_id is required")
    return _launch_scoped_log_stack_check(
        request, stack_id,
        lambda item_key: stacks.run_claimed_log_stack_retry(item_key, stack_id),
    )


@app.post("/logs/stack/check-now")
def check_now_log_stack_route(request: Request, stack_id: str = ""):
    """Non-destructive scoped re-check for every member of this Logs stack -- mirrors Updates'
    stack Check now, hence no confirmation dialog on the button."""
    if not stack_id:
        raise HTTPException(status_code=400, detail="stack_id is required")
    return _launch_scoped_log_stack_check(
        request, stack_id,
        lambda item_key: log_watcher.run_claimed_log_stack_check_now(item_key, stack_id),
    )


@app.post("/logs/stack/reset-and-recheck")
def reset_and_recheck_log_stack_route(request: Request, stack_id: str = ""):
    """Stack-scoped equivalent of the per-service Reset & re-check: wipes and re-checks every
    service belonging to this stack, and no others, then force-regenerates the stack's
    Cross-Service Analysis on top."""
    if not stack_id:
        raise HTTPException(status_code=400, detail="stack_id is required")
    return _launch_scoped_log_stack_check(
        request, stack_id,
        lambda item_key: log_watcher.run_claimed_log_stack_reset_and_recheck(item_key, stack_id),
    )


def _silence_state(active_count: int, silenced_count: int) -> str | None:
    """The shared 3-state silence model for any scope that aggregates a set of findings' active/
    silenced counts (a service's own findings, or every finding across a stack's services):
    None (nothing silenced, or nothing to silence at all), "partially_silenced" (some but not
    all silenced), or "silenced" (at least one finding, and every one of them silenced).
    A brand new finding appearing later naturally starts active, which is what correctly demotes
    a fully "silenced" service/stack back to "partially_silenced" -- silence here is an action
    applied to today's active rows, not a persistent mute flag layered on top (see
    db.silence_all_findings_for_subjects)."""
    total = active_count + silenced_count
    if total == 0 or silenced_count == 0:
        return None
    return "silenced" if active_count == 0 else "partially_silenced"


def _stack_silence_toggle_response(request: Request, stack_id: str):
    member_names = stacks.stack_member_names_for_logs(stack_id)
    active_total = 0
    silenced_total = 0
    for name in member_names:
        summary = _findings_summary(db.list_findings_for_subject("logs", name, include_silenced=True))
        active_total += summary["active_count"]
        silenced_total += summary["silenced_count"]
    return templates.TemplateResponse(
        "_stack_silence_toggle.html",
        {"request": request, "stack_id": stack_id, "silence_state": _silence_state(active_total, silenced_total)},
    )


@app.post("/logs/stack/silence")
def silence_log_stack_route(request: Request, stack_id: str = ""):
    if not stack_id:
        raise HTTPException(status_code=400, detail="stack_id is required")
    db.silence_all_findings_for_subjects("logs", stacks.stack_member_names_for_logs(stack_id))
    return _stack_silence_toggle_response(request, stack_id)


@app.post("/logs/stack/unsilence")
def unsilence_log_stack_route(request: Request, stack_id: str = ""):
    if not stack_id:
        raise HTTPException(status_code=400, detail="stack_id is required")
    db.unsilence_all_findings_for_subjects("logs", stacks.stack_member_names_for_logs(stack_id))
    return _stack_silence_toggle_response(request, stack_id)


@app.get("/logs/stack/status-poll")
def log_stack_status_poll(request: Request, stack_id: str):
    """Polling counterpart to stack_status_poll (Updates' version) below, for a Logs-scoped
    stack action -- same "still running -> re-arm the poller, finished -> redirect back to this
    stack" shape."""
    item_key = _log_stack_item_key(stack_id)
    item = check_state.get_item_state(item_key)

    if item is not None and item["running"]:
        return templates.TemplateResponse(
            "_log_stack_item_status_poll.html",
            {"request": request, "stack_id": stack_id, "item": item, "progress_text": _progress_text(item)},
        )

    check_state.clear_item(item_key)
    resp = templates.TemplateResponse(
        "_log_stack_item_status_poll.html",
        {"request": request, "stack_id": stack_id, "item": None, "progress_text": ""},
    )
    resp.headers["HX-Redirect"] = f"/logs/stack?id={quote(stack_id)}"
    return resp


@app.get("/compose")
def compose_page(request: Request, show_silenced: bool = False,
                  sort: str = "severity", dir: str = "asc", csort: str = "status", cdir: str = "asc"):
    issues = db.list_subjects_with_findings("compose", include_silenced=show_silenced)
    for issue in issues:
        issue["display_name"] = compose_lookup.subject_display_name("compose", issue["subject"])
    issues = _sort_issue_rows(issues, sort, dir)
    files = db.all_compose_file_states_with_status()
    for f in files:
        f["display_name"] = compose_lookup.subject_display_name("compose", f["name"])
    files = _sort_status_list_rows(files, csort, cdir)
    return templates.TemplateResponse(
        "compose.html",
        {
            **_status_context(request, "compose"),
            "issues": issues, "files": files, "show_silenced": show_silenced,
            "sort": sort, "dir": dir, "csort": csort, "cdir": cdir,
            "active_tab": "compose",
        },
    )


@app.get("/compose/file")
def compose_file_detail(request: Request, path: str, sort: str = "severity", dir: str = "asc"):
    # See logs_container_detail's comment -- always shows every finding for this file.
    findings = db.list_findings_for_subject("compose", path, include_silenced=True)

    if len(findings) == 1:
        return RedirectResponse(url=f"/findings/{findings[0]['id']}", status_code=303)

    display_name = compose_lookup.subject_display_name("compose", path)
    overview = _get_or_build_overview("compose", path, display_name, findings)
    overview_html = render_markdown(overview) if overview else None
    summary = _findings_summary(findings)
    return templates.TemplateResponse(
        "subject_findings.html",
        {
            "request": request, "findings": _sort_subject_findings(findings, sort, dir),
            "display_name": display_name, "subject": path,
            "back_url": "/compose", "overview_html": overview_html, "source": "compose",
            **summary,
            "silence_state": _silence_state(summary["active_count"], summary["silenced_count"]),
            "last_checked_at": db.get_compose_file_checkpoint(path),
            "sort": sort, "dir": dir,
            "sort_base_url": "/compose/file", "sort_extra_qs": "&path=" + quote(path),
            "active_tab": "compose",
        },
    )


# ---------------------------------------------------------------------------
# Update detail
# ---------------------------------------------------------------------------

@app.get("/updates/{update_id}")
def update_detail(request: Request, update_id: int):
    update = db.get_update(update_id)
    if update is None:
        raise HTTPException(status_code=404, detail="Update not found")

    # Auto-mark-as-read: viewing this page at all counts as "seen it," the instant it's
    # opened -- a JS "mark it on the way out" approach (pagehide/visibilitychange) was tried
    # first but even the more reliable visibilitychange signal wasn't reliable enough in
    # practice, so this is deliberately unconditional server-side state instead of a
    # best-effort client-side one. The Mark as read/unread toggle still works exactly as
    # before for whenever the user wants to flip it back either way.
    #
    # Deliberately NOT gated on summary_markdown/release_notes_raw existing (an earlier
    # version was, matching the old "Mark as read" button's own gate) -- when release notes
    # genuinely can't be found for an image, that gate meant this never fired and the toggle
    # button never rendered at all (see detail.html), permanently stranding that update as
    # Unread with no way to ever change it, client or server. Viewing the page counts as
    # "seen it" even when the content is "no notes found" -- only an error row is exempt,
    # since those aren't a read/unread concept at all (see the badge/toggle's own guard).
    if update["status"] == "unread" and not update["error"]:
        db.mark_update_status(update_id, "read")
        update = db.get_update(update_id)

    summary_html = render_markdown(update["summary_markdown"]) if update["summary_markdown"] else None
    # No AI summary yet without Stage 7 -- release_notes_raw (Stage 6) is the real content on
    # a fresh install, shown as-is (still markdown-rendered, since GitHub release bodies and
    # changelog files both are) whenever there's no AI summary to show instead.
    release_notes_html = (
        render_markdown(update["release_notes_raw"])
        if not update["summary_markdown"] and update["release_notes_raw"]
        else None
    )
    stack_info = compose_lookup.get_stack_info(update["container_name"])
    stack_id = stack_info["stack_id"] if stack_info and len(stack_info["service_names"]) >= 2 else None
    container_row = db.get_container_state(update["container_name"])
    upgrade_guidance_html = render_markdown(update["upgrade_guidance"]) if update["upgrade_guidance"] else None
    return templates.TemplateResponse(
        "detail.html",
        {
            "request": request, "update": update, "summary_html": summary_html,
            "release_notes_html": release_notes_html,
            "upgrade_guidance_html": upgrade_guidance_html,
            "stack_id": stack_id, "active_tab": "updates",
            "container_silenced": bool(container_row["silenced"]) if container_row else False,
        },
    )


def _read_toggle_response(request: Request, update_id: int):
    """Shared by mark_read/mark_unread: both just flip the status column then re-render the
    same fragment (the button and the title-row badge, the latter via an out-of-band swap) --
    the fragment itself decides which button to show from the update's current status."""
    update = db.get_update(update_id)
    if update is None:
        raise HTTPException(status_code=404, detail="Update not found")
    return templates.TemplateResponse("_read_toggle.html", {"request": request, "update": update})


def _container_silence_toggle_response(request: Request, container_name: str):
    """Shared by silence_container/unsilence_container: same in-place-toggle pattern as
    _read_toggle_response, but keyed by container_name (not update_id) -- an EOL container's
    silenced flag lives on container_state, independent of whatever pending update row exists
    right now, so it must survive that row being deleted and recreated as digests keep
    changing (see db.set_container_silenced)."""
    container_row = db.get_container_state(container_name)
    if container_row is None:
        raise HTTPException(status_code=404, detail="Container not found")
    return templates.TemplateResponse(
        "_container_silence_toggle.html",
        {"request": request, "container_name": container_name, "silenced": bool(container_row["silenced"])},
    )


@app.post("/updates/container/{container_name}/silence")
def silence_container(request: Request, container_name: str):
    if db.get_container_state(container_name) is None:
        raise HTTPException(status_code=404, detail="Container not found")
    db.set_container_silenced(container_name, True)
    return _container_silence_toggle_response(request, container_name)


@app.post("/updates/container/{container_name}/unsilence")
def unsilence_container(request: Request, container_name: str):
    if db.get_container_state(container_name) is None:
        raise HTTPException(status_code=404, detail="Container not found")
    db.set_container_silenced(container_name, False)
    return _container_silence_toggle_response(request, container_name)


@app.post("/updates/{update_id}/read")
def mark_read(request: Request, update_id: int):
    # Neither direction navigates away anymore -- both are in-place htmx toggles (see
    # detail.html's action row and _read_toggle.html). Also the target of the auto-mark-as-
    # read beacon detail.html fires via navigator.sendBeacon() on leaving the page, so this
    # has to tolerate being called with no meaningful response ever being read.
    db.mark_update_status(update_id, "read")
    return _read_toggle_response(request, update_id)


@app.post("/updates/{update_id}/unread")
def mark_unread(request: Request, update_id: int):
    db.mark_update_status(update_id, "unread")
    return _read_toggle_response(request, update_id)


def _item_key(update_id: int) -> str:
    return f"update:{update_id}"


def _render_item_status(request: Request, update_id: int, item_key: str, busy_message: str | None = None):
    item = check_state.get_item_state(item_key)
    return templates.TemplateResponse(
        "_item_status.html",
        {
            "request": request, "update_id": update_id, "item": item,
            "progress_text": _progress_text(item) if item else "",
            "busy_message": busy_message,
        },
    )


def _launch_scoped_check(request: Request, update_id: int, target) -> object:
    """Shared by the per-item Check now and Reset & re-check routes below — identical claim/
    launch/render shape, differing only in which persist.py function actually does the work
    (non-destructive vs delete-the-row-first) once the background thread starts.

    try_start_updates_check() failing here (the busy_message branch) should be rare in
    practice: every button that can reach either route is disabled client-side the moment
    /updates/running-state reports a check in flight (see base.html), so this is just a
    defensive fallback for the brief window before that poll catches up."""
    update = db.get_update(update_id)
    if update is None:
        raise HTTPException(status_code=404, detail="Update not found")

    item_key = _item_key(update_id)
    if not persist.try_start_updates_check():
        return _render_item_status(
            request, update_id, item_key,
            busy_message="A check just started elsewhere — try again shortly.",
        )

    check_state.start_item(item_key, update["container_name"])
    threading.Thread(target=target, args=(item_key, update["container_name"]), daemon=True).start()
    return _render_item_status(request, update_id, item_key)


def _stack_item_key(stack_id: str) -> str:
    return f"stack:{stack_id}"


def _render_stack_item_status(request: Request, stack_id: str, item_key: str, busy_message: str | None = None):
    item = check_state.get_item_state(item_key)
    return templates.TemplateResponse(
        "_stack_item_status.html",
        {
            "request": request, "stack_id": stack_id, "item": item,
            "progress_text": _progress_text(item) if item else "",
            "busy_message": busy_message,
        },
    )


def _launch_scoped_stack_check(request: Request, stack_id: str, target) -> object:
    """Stack-level counterpart to _launch_scoped_check above — identical claim/launch/render
    shape, keyed by stack_id rather than an update id since a stack action's own URL never
    changes underneath it the way a per-update action's id can (a digest transition can get
    superseded mid-recheck; a stack's compose file path can't)."""
    item_key = _stack_item_key(stack_id)
    if not persist.try_start_updates_check():
        return _render_stack_item_status(
            request, stack_id, item_key,
            busy_message="A check just started elsewhere — try again shortly.",
        )

    check_state.start_item(item_key, stack_id)
    threading.Thread(target=target, args=(item_key,), daemon=True).start()
    return _render_stack_item_status(request, stack_id, item_key)


def _log_stack_item_key(stack_id: str) -> str:
    return f"logstack:{stack_id}"


def _render_log_stack_item_status(request: Request, stack_id: str, item_key: str, busy_message: str | None = None):
    item = check_state.get_item_state(item_key)
    return templates.TemplateResponse(
        "_log_stack_item_status.html",
        {
            "request": request, "stack_id": stack_id, "item": item,
            "progress_text": _progress_text(item) if item else "",
            "busy_message": busy_message,
        },
    )


def _launch_scoped_log_stack_check(request: Request, stack_id: str, target) -> object:
    """Logs' counterpart to _launch_scoped_stack_check above -- claims the Logs mutex (check_
    state's "logs" channel), not Updates', since this is a Logs-scoped action and must not
    block on (or be blocked by) an unrelated Updates check."""
    item_key = _log_stack_item_key(stack_id)
    if not check_state.try_start("logs"):
        return _render_log_stack_item_status(
            request, stack_id, item_key,
            busy_message="A check just started elsewhere — try again shortly.",
        )

    check_state.start_item(item_key, stack_id)
    threading.Thread(target=target, args=(item_key,), daemon=True).start()
    return _render_log_stack_item_status(request, stack_id, item_key)


def _log_item_key(container_name: str) -> str:
    return f"logitem:{container_name}"


def _render_log_item_status(request: Request, container_name: str, item_key: str, busy_message: str | None = None):
    item = check_state.get_item_state(item_key)
    return templates.TemplateResponse(
        "_log_item_status.html",
        {
            "request": request, "container_name": container_name, "item": item,
            "progress_text": _progress_text(item) if item else "",
            "busy_message": busy_message,
        },
    )


def _launch_scoped_log_item_check(request: Request, container_name: str, target) -> object:
    """Service-scoped counterpart to _launch_scoped_log_stack_check above -- a container's own
    identity never changes underneath a running action (unlike an Updates row's id), so this
    always lands back on the exact same /logs/container/{container_name} page it started from."""
    item_key = _log_item_key(container_name)
    if not check_state.try_start("logs"):
        return _render_log_item_status(
            request, container_name, item_key,
            busy_message="A check just started elsewhere — try again shortly.",
        )

    check_state.start_item(item_key, container_name)
    threading.Thread(target=target, args=(item_key,), daemon=True).start()
    return _render_log_item_status(request, container_name, item_key)


def _run_claimed_log_item_regenerate(item_key: str, container_name: str) -> None:
    """Service-scoped Regenerate AI Response -- force-recomputes this one container's cached
    overview blurb (see _get_or_build_overview), bypassing the content-hash cache. Same
    "one AI call, reported as a single (0,1) -> (1,1) step" shape as Updates' per-item
    regenerate (persist.run_claimed_regenerate_summary)."""
    check_state.set_item_progress(item_key, "regenerating", 0, 1)
    try:
        findings = db.list_findings_for_subject("logs", container_name, include_silenced=True)
        if len(findings) >= 2:
            _get_or_build_overview("logs", container_name, container_name, findings, force=True)
    except Exception:
        logger.exception("Regenerate AI Response failed unexpectedly for %s", container_name)
    finally:
        check_state.set_item_progress(item_key, "regenerating", 1, 1)
        check_state.finish_item(item_key)
        check_state.release_running("logs")


@app.post("/logs/container/{container_name}/check-now")
def check_now_log_item_route(request: Request, container_name: str):
    return _launch_scoped_log_item_check(
        request, container_name,
        lambda item_key: log_watcher.run_claimed_log_item_check_now(item_key, container_name),
    )


@app.post("/logs/container/{container_name}/reset-and-recheck")
def reset_and_recheck_log_item_route(request: Request, container_name: str):
    return _launch_scoped_log_item_check(
        request, container_name,
        lambda item_key: log_watcher.run_claimed_log_item_reset_and_recheck(item_key, container_name),
    )


@app.post("/logs/container/{container_name}/regenerate")
def regenerate_log_item_route(request: Request, container_name: str):
    return _launch_scoped_log_item_check(
        request, container_name,
        lambda item_key: _run_claimed_log_item_regenerate(item_key, container_name),
    )


@app.get("/logs/container/{container_name}/status-poll")
def log_item_status_poll(request: Request, container_name: str):
    item_key = _log_item_key(container_name)
    item = check_state.get_item_state(item_key)

    if item is not None and item["running"]:
        return templates.TemplateResponse(
            "_log_item_status_poll.html",
            {"request": request, "container_name": container_name, "item": item, "progress_text": _progress_text(item)},
        )

    check_state.clear_item(item_key)
    resp = templates.TemplateResponse(
        "_log_item_status_poll.html",
        {"request": request, "container_name": container_name, "item": None, "progress_text": ""},
    )
    resp.headers["HX-Redirect"] = f"/logs/container/{container_name}"
    return resp


def _subject_read_toggle_response(request: Request, source: str, subject: str):
    summary = _findings_summary(db.list_findings_for_subject(source, subject, include_silenced=True))
    return templates.TemplateResponse(
        "_subject_read_toggle.html", {"request": request, "source": source, "subject": subject, **summary},
    )


@app.post("/logs/container/{container_name}/read")
def mark_log_subject_read(request: Request, container_name: str):
    db.set_findings_read_status_for_subject("logs", container_name, "read")
    return _subject_read_toggle_response(request, "logs", container_name)


@app.post("/logs/container/{container_name}/unread")
def mark_log_subject_unread(request: Request, container_name: str):
    db.set_findings_read_status_for_subject("logs", container_name, "unread")
    return _subject_read_toggle_response(request, "logs", container_name)


def _subject_silence_toggle_response(request: Request, source: str, subject: str):
    summary = _findings_summary(db.list_findings_for_subject(source, subject, include_silenced=True))
    return templates.TemplateResponse(
        "_subject_silence_toggle.html",
        {
            "request": request, "source": source, "subject": subject,
            "silence_state": _silence_state(summary["active_count"], summary["silenced_count"]),
        },
    )


@app.post("/logs/container/{container_name}/silence")
def silence_log_subject(request: Request, container_name: str):
    db.silence_all_findings_for_subjects("logs", [container_name])
    return _subject_silence_toggle_response(request, "logs", container_name)


@app.post("/logs/container/{container_name}/unsilence")
def unsilence_log_subject(request: Request, container_name: str):
    db.unsilence_all_findings_for_subjects("logs", [container_name])
    return _subject_silence_toggle_response(request, "logs", container_name)


# ---------------------------------------------------------------------------
# Compose's own service-scoped (per-file) Check now / Reset & re-check / Regenerate AI Response
# and bulk Read/Unread / Silence/Unsilence -- same shape as Logs' equivalents above, keyed by
# check_state's "compose" channel. Compose file paths are passed as a query string (?path=...),
# not a URL path segment, since they can contain slashes -- same reasoning as the existing
# GET /compose/file?path=... route.
# ---------------------------------------------------------------------------

def _compose_item_key(path: str) -> str:
    return f"composeitem:{path}"


def _render_compose_item_status(request: Request, path: str, item_key: str, busy_message: str | None = None):
    item = check_state.get_item_state(item_key)
    return templates.TemplateResponse(
        "_compose_item_status.html",
        {
            "request": request, "path": path, "item": item,
            "progress_text": _progress_text(item) if item else "",
            "busy_message": busy_message,
        },
    )


def _launch_scoped_compose_item_check(request: Request, path: str, target) -> object:
    """Service-scoped counterpart to _launch_scoped_log_item_check -- a compose file's own path
    never changes underneath a running action, so this always lands back on the exact same
    /compose/file?path=... page it started from."""
    item_key = _compose_item_key(path)
    if not check_state.try_start("compose"):
        return _render_compose_item_status(
            request, path, item_key,
            busy_message="A check just started elsewhere — try again shortly.",
        )

    check_state.start_item(item_key, path)
    threading.Thread(target=target, args=(item_key,), daemon=True).start()
    return _render_compose_item_status(request, path, item_key)


def _run_claimed_compose_item_regenerate(item_key: str, path: str) -> None:
    """Service-scoped Regenerate AI Response -- force-recomputes this one file's cached
    overview blurb (see _get_or_build_overview), bypassing the content-hash cache. Same
    "one AI call, reported as a single (0,1) -> (1,1) step" shape as Logs' own per-item
    regenerate (_run_claimed_log_item_regenerate)."""
    check_state.set_item_progress(item_key, "regenerating", 0, 1)
    try:
        findings = db.list_findings_for_subject("compose", path, include_silenced=True)
        if len(findings) >= 2:
            display_name = compose_lookup.subject_display_name("compose", path)
            _get_or_build_overview("compose", path, display_name, findings, force=True)
    except Exception:
        logger.exception("Regenerate AI Response failed unexpectedly for %s", path)
    finally:
        check_state.set_item_progress(item_key, "regenerating", 1, 1)
        check_state.finish_item(item_key)
        check_state.release_running("compose")


@app.post("/compose/file/check-now")
def check_now_compose_item_route(request: Request, path: str):
    return _launch_scoped_compose_item_check(
        request, path,
        lambda item_key: compose_reviewer.run_claimed_compose_item_check_now(item_key, path),
    )


@app.post("/compose/file/reset-and-recheck")
def reset_and_recheck_compose_item_route(request: Request, path: str):
    return _launch_scoped_compose_item_check(
        request, path,
        lambda item_key: compose_reviewer.run_claimed_compose_item_reset_and_recheck(item_key, path),
    )


@app.post("/compose/file/regenerate")
def regenerate_compose_item_route(request: Request, path: str):
    return _launch_scoped_compose_item_check(
        request, path,
        lambda item_key: _run_claimed_compose_item_regenerate(item_key, path),
    )


@app.get("/compose/file/status-poll")
def compose_item_status_poll(request: Request, path: str):
    item_key = _compose_item_key(path)
    item = check_state.get_item_state(item_key)

    if item is not None and item["running"]:
        return templates.TemplateResponse(
            "_compose_item_status_poll.html",
            {"request": request, "path": path, "item": item, "progress_text": _progress_text(item)},
        )

    check_state.clear_item(item_key)
    resp = templates.TemplateResponse(
        "_compose_item_status_poll.html",
        {"request": request, "path": path, "item": None, "progress_text": ""},
    )
    resp.headers["HX-Redirect"] = f"/compose/file?path={quote(path)}"
    return resp


@app.post("/compose/file/read")
def mark_compose_subject_read(request: Request, path: str):
    db.set_findings_read_status_for_subject("compose", path, "read")
    return _subject_read_toggle_response(request, "compose", path)


@app.post("/compose/file/unread")
def mark_compose_subject_unread(request: Request, path: str):
    db.set_findings_read_status_for_subject("compose", path, "unread")
    return _subject_read_toggle_response(request, "compose", path)


@app.post("/compose/file/silence")
def silence_compose_subject(request: Request, path: str):
    db.silence_all_findings_for_subjects("compose", [path])
    return _subject_silence_toggle_response(request, "compose", path)


@app.post("/compose/file/unsilence")
def unsilence_compose_subject(request: Request, path: str):
    db.unsilence_all_findings_for_subjects("compose", [path])
    return _subject_silence_toggle_response(request, "compose", path)


@app.post("/updates/{update_id}/check-now")
def check_now_update_route(request: Request, update_id: int):
    """Non-destructive scoped re-check: re-checks just this container (digest + release notes
    if something changed), only touching the row if the digest actually moved -- exactly like
    every other "Check now" in the app, hence no confirmation dialog on the button.

    Shares the same "only one check at a time" mutex a full check uses (see
    persist.run_claimed_single_check) without disturbing the full check's own status display.
    Renders the live spinner/progress fragment next to the button; the poller it kicks off
    (see the recheck-status-poll route below) follows up with an HX-Redirect once the
    container's update row has possibly moved to a new id, changed, or disappeared."""
    return _launch_scoped_check(request, update_id, persist.run_claimed_single_check)


@app.post("/updates/{update_id}/reset-and-recheck")
def reset_and_recheck_update_route(request: Request, update_id: int):
    """Destructive scoped equivalent of the global Reset & re-check: wipes just this update's
    history first (see persist.run_and_persist_single_reset_and_check), forcing a fresh
    release notes fetch even if the digest hasn't actually changed -- useful for retrying a
    notes fetch that failed without waiting for a real update. Confirmed client-side since,
    unlike Check now above, this really does throw away state."""
    return _launch_scoped_check(request, update_id, persist.run_claimed_single_reset_and_check)


@app.post("/updates/{update_id}/regenerate")
def regenerate_update_route(request: Request, update_id: int):
    """Stage 7: Regenerate AI Response is real now -- re-runs summarization for this update's
    already-stored release notes in place (no registry check, no fresh notes fetch). The
    button itself is disabled server-side (see detail.html) whenever there's no
    release_notes_raw to regenerate from at all, so this route mainly exists to be reached by
    a real click. Reuses the exact same launch/spinner/poll machinery as Check Now and
    Reset & Re-check above -- the update's id never changes for this action, so the poller's
    "look the container back up by name, redirect to its current id" logic just lands back on
    the same page."""
    return _launch_scoped_check(request, update_id, persist.run_claimed_regenerate_summary)


@app.get("/updates/{update_id}/recheck-status-poll")
def update_recheck_status_poll(request: Request, update_id: int):
    item_key = _item_key(update_id)
    item = check_state.get_item_state(item_key)

    if item is not None and item["running"]:
        return templates.TemplateResponse(
            "_item_status_poll.html",
            {"request": request, "update_id": update_id, "item": item, "progress_text": _progress_text(item)},
        )

    # Finished (or the item vanished, e.g. after a restart) -- figure out where this
    # container's update actually landed: the digest transition it was tracking may have been
    # superseded (a new row, different id), resolved (no row at all), or unchanged (same id).
    container_name = item["container_name"] if item else None
    check_state.clear_item(item_key)
    redirect_url = "/updates"
    if container_name:
        latest = db.get_latest_update_for_container(container_name)
        if latest is not None:
            redirect_url = f"/updates/{latest['id']}"

    resp = templates.TemplateResponse(
        "_item_status_poll.html",
        {"request": request, "update_id": update_id, "item": None, "progress_text": ""},
    )
    resp.headers["HX-Redirect"] = redirect_url
    return resp


# ---------------------------------------------------------------------------
# Findings detail (shared by logs and compose)
# ---------------------------------------------------------------------------

@app.get("/findings/{finding_id}")
def finding_detail(request: Request, finding_id: int):
    finding = db.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")

    # Auto-mark-as-read: viewing this page counts as "seen it" -- same unconditional
    # server-side behavior as update_detail's own auto-mark (see that route's docstring for
    # why this beats a client-side pagehide/visibilitychange signal).
    if finding["read_status"] == "unread":
        db.set_finding_read_status(finding_id, "read")
        finding = db.get_finding(finding_id)

    description_html = render_markdown(finding["description_markdown"] or "")
    suggested_fix_html = render_markdown(finding["suggested_fix"]) if finding["suggested_fix"] else None
    display_name = compose_lookup.subject_display_name(finding["source"], finding["subject"])

    # A finding is the bottom of a 4-level hierarchy for Logs (main -> stack -> service ->
    # finding) and a 3-level one for Compose (main -> file -> finding, no stack concept) --
    # subject_findings_count backs both the "Back to {service/file}" link below (only useful
    # when 2+ findings share this subject; with exactly one, that page would just redirect
    # straight back here) and the Regenerate button's own gate, same reasoning as
    # subject_findings.html's.
    subject_findings_count = len(db.list_findings_for_subject(finding["source"], finding["subject"], include_silenced=True))
    if finding["source"] == "logs":
        subject_url = f"/logs/container/{finding['subject']}"
    else:
        subject_url = f"/compose/file?path={quote(finding['subject'])}"

    # A finding's Check Now/Regenerate/Reset & re-check operate on its own subject (container or
    # compose file), same routes subject_findings.html uses for either source -- there's no
    # per-finding equivalent since a finding's own AI content can only ever come from a fresh
    # log fetch/file review of its whole subject, not from something stored per-finding. The
    # Stack concept itself (below) really is Logs/Updates-only -- see stacks.py.
    stack_id = None
    if finding["source"] == "logs":
        stack_info = compose_lookup.get_stack_info(finding["subject"])
        stack_id = stack_info["stack_id"] if stack_info and len(stack_info["service_names"]) >= 2 else None

    return templates.TemplateResponse(
        "finding_detail.html",
        {
            "request": request, "finding": finding, "description_html": description_html,
            "suggested_fix_html": suggested_fix_html,
            "display_name": display_name, "active_tab": finding["source"],
            "stack_id": stack_id, "subject_findings_count": subject_findings_count,
            "subject_url": subject_url,
        },
    )


def _finding_read_toggle_response(request: Request, finding_id: int):
    """Shared by mark_finding_read/mark_finding_unread -- same in-place-toggle pattern as
    _read_toggle_response for Updates' Mark as Read/Unread."""
    finding = db.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return templates.TemplateResponse("_finding_read_toggle.html", {"request": request, "finding": finding})


@app.post("/findings/{finding_id}/read")
def mark_finding_read(request: Request, finding_id: int):
    finding = db.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    db.set_finding_read_status(finding_id, "read")
    return _finding_read_toggle_response(request, finding_id)


@app.post("/findings/{finding_id}/unread")
def mark_finding_unread(request: Request, finding_id: int):
    finding = db.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    db.set_finding_read_status(finding_id, "unread")
    return _finding_read_toggle_response(request, finding_id)


def _silence_toggle_response(request: Request, finding_id: int):
    """Shared by silence_finding/unsilence_finding: both just flip the status column then
    re-render the same fragment (the button and the title-row badge, the latter via an
    out-of-band swap) -- same in-place-toggle pattern as _read_toggle_response for Updates'
    Mark as Read/Unread, rather than the old redirect-back-to-the-list behavior."""
    finding = db.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return templates.TemplateResponse("_silence_toggle.html", {"request": request, "finding": finding})


@app.post("/findings/{finding_id}/silence")
def silence_finding(request: Request, finding_id: int):
    finding = db.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    db.set_finding_status(finding_id, "silenced")
    return _silence_toggle_response(request, finding_id)


@app.post("/findings/{finding_id}/unsilence")
def unsilence_finding(request: Request, finding_id: int):
    finding = db.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    db.set_finding_status(finding_id, "active")
    return _silence_toggle_response(request, finding_id)
