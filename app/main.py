import hashlib
import logging
from urllib.parse import quote, urlencode

import markdown
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app import compose_lookup, db
from app.check_state import format_summary, get_state, set_running
from app.config import settings
from app.notifications import send_test_notification
from app.summarizer import summarize_findings_overview
from app.schedule_spec import describe as describe_schedule
from app.scheduler import (
    apply_schedules,
    start_scheduler,
    trigger_check_now,
    trigger_compose_check_now,
    trigger_log_check_now,
)
from app.uptime import get_uptime_str
from app.webhook import router as webhook_router

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
templates.env.globals["app_timezone"] = settings.tz
templates.env.globals["get_uptime_str"] = get_uptime_str

# "Suggestion" reads oddly for an update ("this release is a suggestion"?) — "Safe" matches
# the actual meaning (nothing risky here, safe to update) without changing the underlying
# severity value used for storage, sorting, and notification thresholds everywhere else.
SEVERITY_LABELS = {"updates": {"suggestion": "Safe"}}


def severity_label(context: str, value: str) -> str:
    return SEVERITY_LABELS.get(context, {}).get(value, value.capitalize())


templates.env.globals["severity_label"] = severity_label
app.include_router(webhook_router)

TRIGGER_FUNCS = {
    "updates": trigger_check_now,
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

def _render_status(request: Request, feature: str):
    state = get_state(feature)
    resp = templates.TemplateResponse(
        "_status.html",
        {"request": request, "feature": feature, "state": state, "status_summary_text": format_summary(feature, state)},
    )
    if not state["running"]:
        resp.headers["HX-Trigger"] = "checkComplete"
    return resp


@app.post("/updates/check-now")
def updates_check_now(request: Request):
    set_running("updates")
    TRIGGER_FUNCS["updates"]()
    return _render_status(request, "updates")


@app.get("/updates/status")
def updates_status(request: Request):
    return _render_status(request, "updates")


@app.get("/updates/partial")
def updates_partial(request: Request, sort: str = "importance", dir: str = "asc",
                     csort: str = "container", cdir: str = "asc"):
    updates = db.list_recent_updates(limit=100, sort=sort, direction=dir)
    return templates.TemplateResponse(
        "_updates_table.html",
        {"request": request, "updates": updates, "sort": sort, "dir": dir, "csort": csort, "cdir": cdir},
    )


@app.get("/updates/partial/containers")
def updates_partial_containers(request: Request, sort: str = "importance", dir: str = "asc",
                                csort: str = "container", cdir: str = "asc"):
    containers = db.all_container_states(sort=csort, direction=cdir)
    return templates.TemplateResponse(
        "_containers_table.html",
        {"request": request, "containers": containers, "sort": sort, "dir": dir, "csort": csort, "cdir": cdir},
    )


@app.post("/logs/check-now")
def logs_check_now(request: Request):
    set_running("logs")
    TRIGGER_FUNCS["logs"]()
    return _render_status(request, "logs")


@app.get("/logs/status")
def logs_status(request: Request):
    return _render_status(request, "logs")


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


@app.get("/compose/status")
def compose_status(request: Request):
    return _render_status(request, "compose")


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


def _spec_from_form(form, scope: str) -> dict:
    input_type = form.get(f"{scope}_input_type", "datetime")
    if input_type == "cron":
        cron_str = (form.get(f"{scope}_cron", "") or "").strip()
        if cron_str:
            try:
                from apscheduler.triggers.cron import CronTrigger
                CronTrigger.from_crontab(cron_str)  # raises if malformed
                return {"mode": "custom", "cron": cron_str}
            except Exception:
                pass  # malformed despite passing client-side validation — fall back safely below

    mode = form.get(f"{scope}_mode", "daily")
    time_field = f"{scope}_time_weekly" if mode == "weekly" else f"{scope}_time"
    time_str = form.get(time_field, "06:00") or "06:00"
    try:
        hour, minute = (int(x) for x in time_str.split(":"))
    except (ValueError, TypeError):
        hour, minute = 6, 0

    if mode == "hourly":
        try:
            interval = int(form.get(f"{scope}_interval_hours", 4) or 4)
        except ValueError:
            interval = 4
        return {"mode": "hourly", "interval_hours": max(1, interval)}
    if mode == "weekly":
        return {"mode": "weekly", "day_of_week": form.get(f"{scope}_day_of_week", "mon"), "hour": hour, "minute": minute}
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
        feature: db.get_deep_analysis_enabled(feature) for feature in ("logs", "compose")
    }
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request, "master": master, "features": features,
            "describe": describe_schedule, "notify": _build_notify_context(),
            "deep_analysis": deep_analysis,
            "active_tab": "settings",
        },
    )


def _saved(request: Request):
    return templates.TemplateResponse("_saved_indicator.html", {"request": request})


@app.post("/settings/deep-analysis/{feature}")
async def save_deep_analysis(feature: str, request: Request):
    if feature not in ("logs", "compose"):
        raise HTTPException(status_code=404)
    form = await request.form()
    db.set_deep_analysis_enabled(feature, form.get("enabled") == "on")
    return {"status": "ok"}


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
    severity = form.get("severity", "suggestion")
    if severity not in ("suggestion", "warning", "critical"):
        severity = "suggestion"
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
# Tab pages
# ---------------------------------------------------------------------------

@app.get("/updates")
def updates_page(request: Request, sort: str = "importance", dir: str = "asc",
                  csort: str = "container", cdir: str = "asc"):
    updates = db.list_recent_updates(limit=100, sort=sort, direction=dir)
    containers = db.all_container_states(sort=csort, direction=cdir)
    state = get_state("updates")
    return templates.TemplateResponse(
        "updates.html",
        {
            "request": request, "updates": updates, "containers": containers,
            "updates_count": len(updates), "containers_count": len(containers),
            "feature": "updates", "state": state,
            "status_summary_text": format_summary("updates", state),
            "sort": sort, "dir": dir, "csort": csort, "cdir": cdir,
            "active_tab": "updates",
        },
    )


@app.get("/logs")
def logs_page(request: Request, show_silenced: bool = False):
    issues = db.list_subjects_with_findings("logs", include_silenced=show_silenced)
    containers = db.all_log_watch_states_with_status()
    state = get_state("logs")
    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request, "issues": issues, "containers": containers, "show_silenced": show_silenced,
            "feature": "logs", "state": state,
            "status_summary_text": format_summary("logs", state),
            "active_tab": "logs",
        },
    )


@app.get("/logs/container/{container_name}")
def logs_container_detail(request: Request, container_name: str, show_silenced: bool = False):
    findings = db.list_findings_for_subject("logs", container_name, include_silenced=show_silenced)

    if not show_silenced and len(findings) == 1:
        return RedirectResponse(url=f"/findings/{findings[0]['id']}", status_code=303)

    overview = _get_or_build_overview("logs", container_name, container_name, findings)
    toggle_url = f"/logs/container/{quote(container_name)}?{urlencode({'show_silenced': 0 if show_silenced else 1})}"
    return templates.TemplateResponse(
        "subject_findings.html",
        {
            "request": request, "findings": findings, "display_name": container_name,
            "back_url": "/logs", "show_silenced": show_silenced, "overview": overview,
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
    state = get_state("compose")
    return templates.TemplateResponse(
        "compose.html",
        {
            "request": request, "issues": issues, "files": files, "show_silenced": show_silenced,
            "feature": "compose", "state": state,
            "status_summary_text": format_summary("compose", state),
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
    toggle_url = f"/compose/file?{urlencode({'path': path, 'show_silenced': 0 if show_silenced else 1})}"
    return templates.TemplateResponse(
        "subject_findings.html",
        {
            "request": request, "findings": findings, "display_name": display_name,
            "back_url": "/compose", "show_silenced": show_silenced, "overview": overview,
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
    summary_html = markdown.markdown(update["summary_markdown"]) if update["summary_markdown"] else None
    return templates.TemplateResponse(
        "detail.html",
        {"request": request, "update": update, "summary_html": summary_html, "active_tab": "updates"},
    )


@app.post("/updates/{update_id}/read")
def mark_read(update_id: int):
    db.mark_update_status(update_id, "read")
    return RedirectResponse(url="/updates", status_code=303)


# ---------------------------------------------------------------------------
# Findings detail (shared by logs and compose)
# ---------------------------------------------------------------------------

@app.get("/findings/{finding_id}")
def finding_detail(request: Request, finding_id: int):
    finding = db.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    description_html = markdown.markdown(finding["description_markdown"] or "")
    suggested_fix_html = markdown.markdown(finding["suggested_fix"]) if finding["suggested_fix"] else None
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
