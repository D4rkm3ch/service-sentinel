import logging

from fastapi import APIRouter, Request, HTTPException

from app.config import settings
from app.scheduler import trigger_check_now

logger = logging.getLogger("release_radar.webhook")

router = APIRouter()


@router.post("/webhook/dockhand")
async def dockhand_webhook(request: Request, token: str | None = None):
    """Point Dockhand's generic webhook notifier at this endpoint (with ?token=... matching
    WEBHOOK_TOKEN) to trigger an immediate check instead of waiting for the schedule.

    The payload content isn't parsed or trusted — Dockhand's generic webhook format is too
    thin to reliably extract structured data from, so this just treats any POST as a signal
    to go check for ourselves.
    """
    if not settings.webhook_token:
        raise HTTPException(status_code=404, detail="Webhook endpoint is disabled (WEBHOOK_TOKEN not set)")
    if token != settings.webhook_token:
        raise HTTPException(status_code=403, detail="Invalid token")

    body = await request.body()
    logger.info("Webhook received (%d bytes), triggering immediate check", len(body))
    trigger_check_now()
    return {"status": "check triggered"}
