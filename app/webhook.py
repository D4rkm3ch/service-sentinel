import logging

from fastapi import APIRouter, Request, HTTPException

from app import reconcile
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

    This runs the check synchronously within the webhook request itself (no background job
    yet — that's Stage 4), so Dockhand's own request will wait for the full check to finish
    before getting a response. Registry lookups run concurrently as of Stage 2, so the wait
    is shorter than Stage 1's one-at-a-time version, but it's still a blocking wait. If
    Dockhand has a short request timeout, this may need revisiting once Stage 4 brings back
    proper backgrounding.
    """
    if not settings.webhook_token:
        raise HTTPException(status_code=404, detail="Webhook endpoint is disabled (WEBHOOK_TOKEN not set)")
    if token != settings.webhook_token:
        raise HTTPException(status_code=403, detail="Invalid token")

    body = await request.body()
    logger.info("Webhook received (%d bytes), running an immediate check", len(body))
    outcome = reconcile.run_check()
    return {"status": "check complete", "checked": len(outcome["containers"]), "errors": outcome["errors"]}
