import logging

import markdown
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app import db
from app.check_state import format_summary, get_state, set_running
from app.config import settings
from app.scheduler import (
    start_scheduler,
    trigger_check_now,
    trigger_compose_check_now,
    trigger_log_check_now,
)
from app.webhook import router as webhook_router

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
# Shared check-now / status / silence handlers
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
def updates_partial(request: Request):
    updates = db.list_recent_updates(limit=100)
    return templates.TemplateResponse("_updates_table.html", {"request": request, "updates": updates})


@app.get("/updates/partial/containers")
def updates_partial_containers(request: Request):
    containers = db.all_container_states()
    return templates.TemplateResponse("_containers_table.html", {"request": request, "containers": containers})


@app.post("/logs/check-now")
def logs_check_now(request: Request):
    set_running("logs")
    TRIGGER_FUNCS["logs"]()
    return _render_status(request, "logs")


@app.get("/logs/status")
def logs_status(request: Request):
    return _render_status(request, "logs")


@app.get("/logs/partial")
def logs_partial(request: Request):
    findings = db.list_findings("logs")
    return templates.TemplateResponse("_findings_table.html", {"request": request, "findings": findings})


@app.post("/compose/check-now")
def compose_check_now(request: Request):
    set_running("compose")
    TRIGGER_FUNCS["compose"]()
    return _render_status(request, "compose")


@app.get("/compose/status")
def compose_status(request: Request):
    return _render_status(request, "compose")


@app.get("/compose/partial")
def compose_partial(request: Request):
    findings = db.list_findings("compose")
    return templates.TemplateResponse("_findings_table.html", {"request": request, "findings": findings})


# ---------------------------------------------------------------------------
# Tab pages (declared with their literal sub-routes above already registered,
# so "/updates/status" etc. never falls through to the "/updates/{update_id}"
# detail route registered further below)
# ---------------------------------------------------------------------------

@app.get("/updates")
def updates_page(request: Request):
    updates = db.list_recent_updates(limit=100)
    containers = db.all_container_states()
    state = get_state("updates")
    return templates.TemplateResponse(
        "updates.html",
        {
            "request": request, "updates": updates, "containers": containers,
            "feature": "updates", "state": state,
            "status_summary_text": format_summary("updates", state),
            "active_tab": "updates",
        },
    )


@app.get("/logs")
def logs_page(request: Request):
    findings = db.list_findings("logs")
    state = get_state("logs")
    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request, "findings": findings,
            "feature": "logs", "state": state,
            "status_summary_text": format_summary("logs", state),
            "active_tab": "logs",
        },
    )


@app.get("/compose")
def compose_page(request: Request):
    findings = db.list_findings("compose")
    state = get_state("compose")
    return templates.TemplateResponse(
        "compose.html",
        {
            "request": request, "findings": findings,
            "feature": "compose", "state": state,
            "status_summary_text": format_summary("compose", state),
            "active_tab": "compose",
        },
    )


# ---------------------------------------------------------------------------
# Update detail (literal /updates/check-now, /updates/status, /updates/partial*
# routes above are matched first since they're declared earlier)
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
    return templates.TemplateResponse(
        "finding_detail.html",
        {
            "request": request, "finding": finding, "description_html": description_html,
            "active_tab": finding["source"],
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
