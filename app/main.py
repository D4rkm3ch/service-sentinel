import hashlib
import logging
import re
import threading
from urllib.parse import quote, urlencode
from zoneinfo import available_timezones

import markdown
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app import check_state, compose_lookup, db, persist, stacks
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

RELEASE_RADAR_VERSION = "0.6.0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("release_radar")


class NoStoreMiddleware(BaseHTTPMiddleware):
    """Prevents the browser from serving a stale cached snapshot on back/forward navigation."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response


app = FastAPI(title="release-radar")
app.add_middleware(NoStoreMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["app_version"] = RELEASE_RADAR_VERSION
templates.env.globals["github_url"] = "https://github.com/D4rkm3ch/release-radar"
templates.env.globals["app_timezone"] = db.get_timezone  # a callable, not a value -- Stage 5c
templates.env.globals["get_uptime_str"] = get_uptime_str

# "Suggestion" reads oddly for an update ("this release is a suggestion"?) — "Safe" matches
# the actual meaning (nothing risky here, safe to update) without changing the underlying
# severity value used for storage, sorting, and notification thresholds everywhere else.
SEVERITY_LABELS = {
    "updates": {
        "bugfix": "Bug Fixes",
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

# Every markdown-rendered block in the app (release notes, AI summaries/overviews, finding
# descriptions and suggested fixes) can contain links the user didn't put there themselves --
# a GitHub release body linking to Watchtower, a CHANGELOG.md, an upstream issue. Those should
# open in a new tab rather than navigating the user away from release-radar; internal links
# this app generates itself (e.g. _linkify_stack_mentions's "#row-<service>" jump links) are
# same-page anchors, not http(s) URLs, so this regex never touches them.
_EXTERNAL_LINK_RE = re.compile(r'<a href="(https?://[^"]*)"')


def render_markdown(text: str) -> str:
    return _EXTERNAL_LINK_RE.sub(r'<a target="_blank" rel="noopener" href="\1"', markdown.markdown(text))


TRIGGER_FUNCS = {
    "logs": trigger_log_check_now,
    "compose": trigger_compose_check_now,
}
FINDING_SOURCES = {"logs": "/logs", "compose": "/compose"}


@app.on_event("startup")
def on_startup():
    for problem in settings.validate():
        logger.warning(problem)
    db.init_db()
    start_scheduler()
    logger.info("release-radar started")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

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
    detail = f"Last checked {last_at[:16].replace('T', ' ')}" if last_at else "Never checked"
    return {
        "feature": feature, "title": title, "enabled": enabled,
        "headline": headline, "detail": detail, "tab_url": tab_url,
    }


@app.get("/")
def overview(request: Request):
    cards = [
        _build_card("updates", "Updates", "/updates"),
        _build_card("logs", "Log health", "/logs"),
        _build_card("compose", "Compose health", "/compose"),
    ]
    return templates.TemplateResponse(
        "overview.html", {"request": request, "cards": cards, "active_tab": "overview"}
    )


@app.post("/settings/toggle/{feature}")
def toggle_feature(feature: str, request: Request):
    if feature not in ("updates", "logs", "compose"):
        raise HTTPException(status_code=404)
    db.set_feature_enabled(feature, not db.get_feature_enabled(feature))
    titles = {"updates": "Updates", "logs": "Log health", "compose": "Compose health"}
    tab_urls = {"updates": "/updates", "logs": "/logs", "compose": "/compose"}
    card = _build_card(feature, titles[feature], tab_urls[feature])
    return templates.TemplateResponse("_feature_card.html", {"request": request, "card": card})


# ---------------------------------------------------------------------------
# Shared check-now / status handlers
# ---------------------------------------------------------------------------

# Live progress text (e.g. "Checking for updates (23/59)") only makes sense for a feature that
# reports progress — currently just "updates" (Stage 2, staged Stage 6). Polling faster while
# it's running gives meaningfully live-feeling updates now that a full check finishes in
# seconds rather than up to a minute; logs/compose keep their original 2s cadence untouched.
_FAST_POLL_FEATURES = {"updates"}

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
}


def _progress_text(progress: dict) -> str:
    total = progress.get("total") or 0
    if not total:
        return "Checking…"
    label = _STAGE_LABELS.get(progress.get("stage"), "Checking")
    return f"{label} ({progress['done']}/{total})…"


def _status_context(request: Request, feature: str) -> dict:
    state = get_state(feature)
    progress = get_progress(feature)
    poll_delay_ms = 500 if feature in _FAST_POLL_FEATURES else 2000
    return {
        "request": request,
        "feature": feature,
        "state": state,
        "progress": progress,
        "progress_text": _progress_text(progress),
        "poll_delay_ms": poll_delay_ms,
        "status_summary_text": format_summary(feature, state),
    }


def _render_status(request: Request, feature: str):
    context = _status_context(request, feature)
    resp = templates.TemplateResponse("_status.html", context)
    if not context["state"]["running"]:
        resp.headers["HX-Trigger"] = "checkComplete"
    return resp


def _render_status_poll(request: Request, feature: str):
    context = _status_context(request, feature)
    resp = templates.TemplateResponse("_status_poll.html", context)
    if not context["state"]["running"]:
        resp.headers["HX-Trigger"] = "checkComplete"
    return resp


@app.post("/updates/check-now")
def updates_check_now(request: Request):
    _launch_check_if_not_running()
    return _render_status(request, "updates")


@app.get("/updates/status-poll")
def updates_status_poll(request: Request):
    return _render_status_poll(request, "updates")


@app.get("/updates/running-state")
def updates_running_state():
    """Tiny polled-everywhere signal (see base.html) for disabling every Updates-related
    Check now / Reset & re-check button across the Updates, Stack, and Service pages while
    any check -- full or a single scoped per-item recheck, both claim the same mutex -- is in
    flight. Deliberately its own minimal endpoint rather than reusing status-poll's full HTML
    fragment: this needs to be pollable from pages that don't render the status badge at all
    (Stack, Service), and every caller only ever needs the one boolean."""
    return {"running": get_state("updates")["running"]}


@app.get("/updates/partial")
def updates_partial(request: Request, sort: str = "importance", dir: str = "asc",
                     csort: str = "container", cdir: str = "asc"):
    rows = db.list_tracked_containers_with_status()
    updates = _sort_and_filter_rows(rows, sort, dir, updates_only=True)
    return templates.TemplateResponse(
        "_updates_table.html",
        {"request": request, "updates": updates, "sort": sort, "dir": dir, "csort": csort, "cdir": cdir, "is_partial": True},
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
def logs_status_poll(request: Request):
    return _render_status_poll(request, "logs")


@app.get("/logs/partial/issues")
def logs_partial_issues(request: Request, show_silenced: bool = False):
    issues = db.list_subjects_with_findings("logs", include_silenced=show_silenced)
    return templates.TemplateResponse(
        "_issues_grouped_table.html",
        {"request": request, "issues": issues, "source": "logs", "show_silenced": show_silenced},
    )


@app.get("/logs/partial/containers")
def logs_partial_containers(request: Request):
    items = db.all_log_watch_states_with_status()
    return templates.TemplateResponse("_status_list_table.html", {"request": request, "items": items, "detail_base": "/logs/container"})


@app.post("/compose/check-now")
def compose_check_now(request: Request):
    set_running("compose")
    TRIGGER_FUNCS["compose"]()
    return _render_status(request, "compose")


@app.get("/compose/status-poll")
def compose_status_poll(request: Request):
    return _render_status_poll(request, "compose")


@app.get("/compose/partial/issues")
def compose_partial_issues(request: Request, show_silenced: bool = False):
    issues = db.list_subjects_with_findings("compose", include_silenced=show_silenced)
    for issue in issues:
        issue["display_name"] = compose_lookup.subject_display_name("compose", issue["subject"])
    return templates.TemplateResponse(
        "_issues_grouped_table.html",
        {"request": request, "issues": issues, "source": "compose", "show_silenced": show_silenced},
    )


@app.get("/compose/partial/files")
def compose_partial_files(request: Request):
    items = db.all_compose_file_states_with_status()
    for item in items:
        item["display_name"] = compose_lookup.subject_display_name("compose", item["name"])
    return templates.TemplateResponse("_status_list_table.html", {"request": request, "items": items, "detail_base": "/compose/file", "use_query_param": True})


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


def _sort_and_filter_rows(rows: list[dict], sort: str, direction: str, updates_only: bool) -> list[dict]:
    """Filters the persisted per-container rows (db.list_tracked_containers_with_status()) down
    to just the ones needing attention when updates_only is set, attaches stack info to every
    row (see _attach_stack_info), and sorts by whichever column was clicked.

    Importance is the odd one out: unclassified rows (errors, or anything AI summarization
    hasn't reached) are pinned to the very top regardless of direction -- they might be
    critical issues the operator isn't aware of yet and can't be allowed to just scroll off
    the bottom on a reverse sort the way a genuinely low-severity bugfix can. Rows where a
    check ran and genuinely found no release notes are a separate, lower tier: nothing to
    investigate there, so they sort alongside real severities (below even bugfix) instead of
    being pinned to the top."""
    filtered = [r for r in rows if not updates_only or r["status"] in ("update_available", "error")]
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
    else:
        annotated.sort(key=lambda r: r["container_name"].lower(), reverse=reverse)

    return annotated


def _get_or_build_overview(source: str, subject: str, display_name: str, findings) -> str | None:
    """Combined AI overview shown above a subject's findings list. Cached by a hash of the
    current finding set so it's only regenerated (costing an API call) when something about
    the findings actually changes, not on every page view. Never called for 0 or 1 findings —
    those cases either show nothing or get redirected straight to the single finding."""
    if len(findings) < 2:
        return None

    fingerprint_input = "|".join(sorted(f"{f['id']}:{f['title']}:{f['status']}" for f in findings))
    findings_hash = hashlib.sha256(fingerprint_input.encode()).hexdigest()[:16]

    cached = db.get_subject_summary(source, subject)
    if cached and cached["findings_hash"] == findings_hash:
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
        "severity_master": db.get_severity_master(),
        "features": {
            feature: {
                "enabled": db.get_feature_notify_enabled(feature),
                "use_master_severity": db.get_feature_uses_master_severity(feature),
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
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request, "master": master, "features": features,
            "describe": describe_schedule, "notify": _build_notify_context(),
            "deep_analysis": deep_analysis, "update_severities": list(UPDATE_SEVERITIES),
            "release_notes_web_search_enabled": db.get_release_notes_web_search_enabled(),
            "timezone": db.get_timezone(), "available_timezones": AVAILABLE_TIMEZONES,
            "ai_provider": db.get_ai_provider(),
            "anthropic_key_configured": bool(db.get_anthropic_api_key()),
            "anthropic_model": db.get_anthropic_model(),
            "anthropic_models": ANTHROPIC_MODELS,
            "gemini_key_configured": bool(db.get_gemini_api_key()),
            "gemini_model": db.get_gemini_model(),
            "gemini_models": GEMINI_MODELS,
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


@app.post("/settings/release-notes/web-search")
async def save_release_notes_web_search(request: Request):
    form = await request.form()
    db.set_release_notes_web_search_enabled(form.get("enabled") == "on")
    return {"status": "ok"}


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
    # Blank submission means "leave it as-is" (the field's placeholder already says so once a
    # key is on file) -- never overwrite a working key with an accidental empty save.
    if key:
        db.set_anthropic_api_key(key)
    return {"status": "ok"}


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
    if key:
        db.set_gemini_api_key(key)
    return {"status": "ok"}


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
    if feature not in VALID_FEATURES:
        raise HTTPException(status_code=404)
    form = await request.form()
    db.set_feature_uses_master_schedule(feature, form.get("enabled") == "on")
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


@app.post("/settings/notify/severity/{scope}")
async def save_notify_severity(scope: str, request: Request):
    if scope not in VALID_SCOPES:
        raise HTTPException(status_code=404)
    form = await request.form()
    valid_values = UPDATE_SEVERITIES if scope == "updates" else FINDING_SEVERITIES
    default_value = "bugfix" if scope == "updates" else "suggestion"
    severity = form.get("severity", default_value)
    if severity not in valid_values:
        severity = default_value
    if scope == "master":
        db.set_severity_master(severity)
    else:
        db.set_feature_severity(scope, severity)
    return _saved(request)


@app.post("/settings/notify/use-master-severity/{feature}")
async def save_notify_use_master_severity(feature: str, request: Request):
    if feature not in VALID_FEATURES:
        raise HTTPException(status_code=404)
    form = await request.form()
    db.set_feature_uses_master_severity(feature, form.get("enabled") == "on")
    return _saved(request)


@app.post("/settings/notify/apprise-test")
async def test_apprise(request: Request):
    form = await request.form()
    raw = form.get("apprise_urls", "") or ""
    urls = [u.strip() for u in raw.replace("\n", ",").split(",") if u.strip()]
    success, message = send_test_notification(urls=urls)
    if success:
        # Only persist the URL once it's actually proven to work — an unsaved textarea
        # that hasn't been tested (or failed its test) never gets written to the database.
        db.set_apprise_urls(raw)
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


def _linkify_stack_mentions(text: str, service_names: list[str]) -> str:
    """Turns exact mentions of a stack's own service names within the analysis text into
    jump-links to that service's row on the same page. Runs on the raw markdown before
    rendering, since inline HTML passes through markdown.markdown() unescaped. Longest names
    are matched first in one combined pass so a shorter name that happens to be a substring
    of a longer one (rare, but possible) can't steal part of the match."""
    if not text or not service_names:
        return text
    names_sorted = sorted(set(service_names), key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in names_sorted) + r")\b")
    return pattern.sub(lambda m: f'<a href="#row-{m.group(1)}">{m.group(1)}</a>', text)


def _stack_member_names(stack_id: str) -> list[str]:
    index = compose_lookup.build_stack_index()
    return sorted(
        c["container_name"] for c in db.all_container_states()
        if (match := compose_lookup.match_container_to_stack(c["container_name"], index)) and match["stack_id"] == stack_id
    )


@app.get("/updates/stack")
def stack_detail(request: Request, id: str):
    stack_row = db.get_stack(id)
    member_names = _stack_member_names(id)
    display_name = stack_row["display_name"] if stack_row else (member_names[0] if member_names else "Unknown stack")
    analysis_row = db.get_stack_analysis(id)
    analysis_html = None
    if analysis_row:
        linked_text = _linkify_stack_mentions(analysis_row["analysis_markdown"], member_names)
        analysis_html = render_markdown(linked_text)

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
            "members": members,
            "analysis_html": analysis_html, "active_tab": "updates",
        },
    )


@app.post("/updates/stack/rename")
async def rename_stack_route(request: Request):
    form = await request.form()
    stack_id = form.get("stack_id", "")
    name = (form.get("name") or "").strip()
    if stack_id and name:
        stacks.rename_stack(stack_id, name)
    return RedirectResponse(url=f"/updates/stack?id={quote(stack_id)}", status_code=303)


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
    return RedirectResponse(url=f"/updates/stack?id={quote(stack_id)}", status_code=303)


@app.post("/updates/stack/retry")
async def retry_stack_route(request: Request):
    # Not reachable from the UI yet — stack detection returns in Stage 12. Kept as a safe
    # no-op (not removed) rather than calling functions that no longer exist in Stage 1.
    form = await request.form()
    stack_id = form.get("stack_id", "")
    return RedirectResponse(url=f"/updates/stack?id={quote(stack_id)}", status_code=303)


@app.post("/updates/stack/reset-and-recheck")
async def reset_and_recheck_stack_route(request: Request):
    """Stack-scoped equivalent of the per-item Reset & re-check: wipes and re-checks every
    service belonging to this stack, and no others. Runs synchronously in the request (the
    button is a plain form post, not htmx, matching how it's always been wired) since a stack
    is a handful of services at most -- nowhere near the size where a background thread +
    spinner/poll setup like the per-item buttons use would earn its keep.

    Shares the same "only one check at a time" mutex as every other check. If it's already
    held (a full check or another scoped action mid-flight), this is a silent no-op and just
    redirects back -- the button is already disabled client-side the moment
    /updates/running-state reports anything running, so this is only a defensive fallback for
    the brief window before that poll catches up, same as _launch_scoped_check's busy_message
    branch for the per-item buttons."""
    form = await request.form()
    stack_id = form.get("stack_id", "")
    member_names = _stack_member_names(stack_id) if stack_id else []
    if member_names and persist.try_start_updates_check():
        try:
            persist.run_and_persist_many_reset_and_check(member_names)
        finally:
            check_state.release_running("updates")
    return RedirectResponse(url=f"/updates/stack?id={quote(stack_id)}", status_code=303)


# ---------------------------------------------------------------------------
# Tab pages
# ---------------------------------------------------------------------------

@app.get("/updates")
def updates_page(request: Request, sort: str = "importance", dir: str = "asc",
                  csort: str = "container", cdir: str = "asc"):
    rows = db.list_tracked_containers_with_status()
    updates = _sort_and_filter_rows(rows, sort, dir, updates_only=True)
    containers = _sort_and_filter_rows(rows, csort, cdir, updates_only=False)
    return templates.TemplateResponse(
        "updates.html",
        {
            **_status_context(request, "updates"),
            "updates": updates, "containers": containers,
            "updates_count": len(updates), "containers_count": len(containers),
            "sort": sort, "dir": dir, "csort": csort, "cdir": cdir,
            "active_tab": "updates",
        },
    )


@app.get("/logs")
def logs_page(request: Request, show_silenced: bool = False):
    issues = db.list_subjects_with_findings("logs", include_silenced=show_silenced)
    containers = db.all_log_watch_states_with_status()
    return templates.TemplateResponse(
        "logs.html",
        {
            **_status_context(request, "logs"),
            "issues": issues, "containers": containers, "show_silenced": show_silenced,
            "active_tab": "logs",
        },
    )


@app.get("/logs/container/{container_name}")
def logs_container_detail(request: Request, container_name: str, show_silenced: bool = False):
    findings = db.list_findings_for_subject("logs", container_name, include_silenced=show_silenced)

    if not show_silenced and len(findings) == 1:
        return RedirectResponse(url=f"/findings/{findings[0]['id']}", status_code=303)

    overview = _get_or_build_overview("logs", container_name, container_name, findings)
    overview_html = render_markdown(overview) if overview else None
    toggle_url = f"/logs/container/{quote(container_name)}?{urlencode({'show_silenced': 0 if show_silenced else 1})}"
    return templates.TemplateResponse(
        "subject_findings.html",
        {
            "request": request, "findings": findings, "display_name": container_name,
            "back_url": "/logs", "show_silenced": show_silenced, "overview_html": overview_html,
            "toggle_url": toggle_url,
            "active_tab": "logs",
        },
    )


@app.get("/compose")
def compose_page(request: Request, show_silenced: bool = False):
    issues = db.list_subjects_with_findings("compose", include_silenced=show_silenced)
    for issue in issues:
        issue["display_name"] = compose_lookup.subject_display_name("compose", issue["subject"])
    files = db.all_compose_file_states_with_status()
    for f in files:
        f["display_name"] = compose_lookup.subject_display_name("compose", f["name"])
    return templates.TemplateResponse(
        "compose.html",
        {
            **_status_context(request, "compose"),
            "issues": issues, "files": files, "show_silenced": show_silenced,
            "active_tab": "compose",
        },
    )


@app.get("/compose/file")
def compose_file_detail(request: Request, path: str, show_silenced: bool = False):
    findings = db.list_findings_for_subject("compose", path, include_silenced=show_silenced)

    if not show_silenced and len(findings) == 1:
        return RedirectResponse(url=f"/findings/{findings[0]['id']}", status_code=303)

    display_name = compose_lookup.subject_display_name("compose", path)
    overview = _get_or_build_overview("compose", path, display_name, findings)
    overview_html = render_markdown(overview) if overview else None
    toggle_url = f"/compose/file?{urlencode({'path': path, 'show_silenced': 0 if show_silenced else 1})}"
    return templates.TemplateResponse(
        "subject_findings.html",
        {
            "request": request, "findings": findings, "display_name": display_name,
            "back_url": "/compose", "show_silenced": show_silenced, "overview_html": overview_html,
            "toggle_url": toggle_url,
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
    return templates.TemplateResponse(
        "detail.html",
        {
            "request": request, "update": update, "summary_html": summary_html,
            "release_notes_html": release_notes_html,
            "stack_id": stack_id, "active_tab": "updates",
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
    description_html = render_markdown(finding["description_markdown"] or "")
    suggested_fix_html = render_markdown(finding["suggested_fix"]) if finding["suggested_fix"] else None
    display_name = compose_lookup.subject_display_name(finding["source"], finding["subject"])
    return templates.TemplateResponse(
        "finding_detail.html",
        {
            "request": request, "finding": finding, "description_html": description_html,
            "suggested_fix_html": suggested_fix_html,
            "display_name": display_name, "active_tab": finding["source"],
        },
    )


@app.post("/findings/{finding_id}/silence")
def silence_finding(finding_id: int):
    finding = db.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    db.set_finding_status(finding_id, "silenced")
    return RedirectResponse(url=FINDING_SOURCES.get(finding["source"], "/"), status_code=303)


@app.post("/findings/{finding_id}/unsilence")
def unsilence_finding(finding_id: int):
    finding = db.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    db.set_finding_status(finding_id, "active")
    return RedirectResponse(url=FINDING_SOURCES.get(finding["source"], "/"), status_code=303)
