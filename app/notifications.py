"""Sends a notification whenever a new update note is generated. Both channels can be
active at once — this isn't a fallback chain, it's "notify everywhere configured."
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger("release_radar.notifications")

COLOR_OK = 0x5EC9A6
COLOR_ERROR = 0xD9705E


def _dashboard_url(update_id: int) -> str:
    path = f"/updates/{update_id}"
    return f"{settings.public_url}{path}" if settings.public_url else path


def _send_discord(container_name: str, image_repo: str, tag: str, update_id: int, error: str | None) -> None:
    if not settings.discord_webhook_url:
        return

    url = _dashboard_url(update_id)
    description = f"Couldn't generate a summary automatically: {error}" if error else \
        "AI summary is ready — new features and breaking changes, checked against your compose config."

    payload = {
        "embeds": [
            {
                "title": f"Update available: {container_name}",
                "description": description,
                "url": url if settings.public_url else None,
                "fields": [{"name": "Image", "value": f"{image_repo}:{tag}", "inline": True}],
                "color": COLOR_ERROR if error else COLOR_OK,
            }
        ]
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(settings.discord_webhook_url, json=payload)
            resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Discord notification failed for %s", container_name)


def _send_apprise(container_name: str, image_repo: str, tag: str, update_id: int, error: str | None) -> None:
    if not settings.apprise_urls:
        return

    # Imported lazily so the app still starts fine if Apprise isn't configured or installed.
    import apprise

    url = _dashboard_url(update_id)
    title = f"Update available: {container_name}"
    if error:
        body = f"{image_repo}:{tag}\nCouldn't generate a summary automatically: {error}"
    else:
        body = f"{image_repo}:{tag}\nAI summary ready.\n{url}"

    try:
        a = apprise.Apprise()
        for service_url in settings.apprise_urls:
            a.add(service_url)
        a.notify(title=title, body=body)
    except Exception:
        logger.exception("Apprise notification failed for %s", container_name)


def notify_update(container_name: str, image_repo: str, tag: str, update_id: int, error: str | None = None) -> None:
    _send_discord(container_name, image_repo, tag, update_id, error)
    _send_apprise(container_name, image_repo, tag, update_id, error)


SEVERITY_ORDER = {"suggestion": 0, "warning": 1, "critical": 2}


def _finding_dashboard_url(source: str, finding_id: int) -> str:
    path = f"/findings/{finding_id}"
    return f"{settings.public_url}{path}" if settings.public_url else path


def _meets_severity_threshold(severity: str) -> bool:
    threshold = SEVERITY_ORDER.get(settings.min_notify_severity, 0)
    return SEVERITY_ORDER.get(severity, 0) >= threshold


def notify_finding(source: str, subject: str, title: str, severity: str, category: str, finding_id: int) -> None:
    """Notifies about a newly created finding (log issue or compose issue), if its severity
    meets the configured threshold. Recurrences of an already-known finding never reach this —
    callers only call it for genuinely new findings (see db.upsert_finding's is_new return)."""
    if not _meets_severity_threshold(severity):
        return

    url = _finding_dashboard_url(source, finding_id)
    label = "Log issue" if source == "logs" else "Compose issue"
    discord_title = f"{label} ({severity}): {subject}"
    body = f"{title}\n\nCategory: {category}\n{url}"

    if settings.discord_webhook_url:
        payload = {
            "embeds": [
                {
                    "title": discord_title,
                    "description": title,
                    "url": url if settings.public_url else None,
                    "fields": [
                        {"name": "Category", "value": category, "inline": True},
                        {"name": "Severity", "value": severity, "inline": True},
                    ],
                    "color": COLOR_ERROR if severity == "critical" else COLOR_OK,
                }
            ]
        }
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(settings.discord_webhook_url, json=payload)
                resp.raise_for_status()
        except httpx.HTTPError:
            logger.exception("Discord notification failed for finding %s", finding_id)

    if settings.apprise_urls:
        import apprise
        try:
            a = apprise.Apprise()
            for service_url in settings.apprise_urls:
                a.add(service_url)
            a.notify(title=discord_title, body=body)
        except Exception:
            logger.exception("Apprise notification failed for finding %s", finding_id)
