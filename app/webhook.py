import logging

from fastapi import APIRouter, Request, HTTPException

from app import persist
from app.check_state import get_state, set_finished, set_running
from app.config import settings

logger = logging.getLogger("release_radar.webhook")

router = APIRouter()


@router.post("/webhook/dockhand")
async def dockhand_webhook(request: Request, token: str | None = None):
    """Point Dockhand's generic webhook notifier at this endpoint (with ?token=... matching
    WEBHOOK_TOKEN) to trigger an immediate check instead of waiting for the schedule.

    The payload content isn't parsed or trusted — Dockhand's generic webhook format is too
    thin to reliably extract structured data from, so this just treats any POST as a signal
    to go check for ourselves.

    Deliberately still synchronous (unlike the UI's Check now, which backgrounds itself as of
    Stage 1's real-world fixes) — Dockhand expects a response describing what the check found,
    so its own request waits for the full check to finish. Registry lookups run concurrently
    (Stage 2), so the wait is much shorter than a one-at-a-time check would be, but it's still
    a blocking wait; if Dockhand has a short request timeout this may need revisiting.

    Shares the same check_state "running" flag the UI uses (Stage 4), so a webhook-triggered
    check and a UI-triggered one can never run concurrently and duplicate registry/DB work —
    whichever started first wins, and the other is skipped rather than piling on top of it.
    Also mirrors the UI worker's safety net: if the check fails partway through, "running"
    still gets cleared rather than getting stuck, so the next trigger (webhook or UI) isn't
    permanently blocked by one bad run.
    """
    if not settings.webhook_token:
        raise HTTPException(status_code=404, detail="Webhook endpoint is disabled (WEBHOOK_TOKEN not set)")
    if token != settings.webhook_token:
        raise HTTPException(status_code=403, detail="Invalid token")

    if get_state("updates").get("running"):
        logger.info("Webhook received while a check is already running — skipping this trigger")
        return {"status": "skipped, a check is already in progress"}

    set_running("updates")
    body = await request.body()
    logger.info("Webhook received (%d bytes), running an immediate check", len(body))
    try:
        outcome = persist.run_and_persist_check()
    except Exception:
        logger.exception("Webhook-triggered check failed unexpectedly")
        set_finished("updates", {"checked": 0, "updates_found": 0, "errors": 1})
        raise

    result = {
        "checked": len(outcome["containers"]),
        "updates_found": sum(1 for c in outcome["containers"] if c["status"] == "update_available"),
        "errors": outcome["errors"],
    }
    set_finished("updates", result)
    return {"status": "check complete", "checked": result["checked"], "errors": result["errors"]}
