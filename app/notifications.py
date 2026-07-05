"""Notification dispatch — Apprise only. All settings (enabled, URLs, per-feature toggles,
severity thresholds) live in the database and are configured from the Settings tab, not env
vars, matching the pattern the rest of the app's runtime configuration follows.
"""

import logging

from app import db
from app.config import settings

logger = logging.getLogger("release_radar.notifications")

SEVERITY_ORDER = {"suggestion": 0, "warning": 1, "critical": 2}


def _dashboard_url(path: str) -> str:
    return f"{settings.public_url}{path}" if settings.public_url else path


def _send(title: str, body: str) -> None:
    urls = db.get_apprise_urls()
    if not urls:
        return
    import apprise
    try:
        a = apprise.Apprise()
        for url in urls:
            a.add(url)
        a.notify(title=title, body=body)
    except Exception:
        logger.exception("Apprise notification failed")


def notify_update(container_name: str, image_repo: str, tag: str, update_id: int, error: str | None = None) -> None:
    if not db.get_notifications_enabled() or not db.get_feature_notify_enabled("updates"):
        return
    url = _dashboard_url(f"/updates/{update_id}")
    title = f"Update available: {container_name}"
    if error:
        body = f"{image_repo}:{tag}\nCouldn't generate a summary automatically: {error}"
    else:
        body = f"{image_repo}:{tag}\nAI summary ready.\n{url}"
    _send(title, body)


def notify_finding(source: str, subject: str, title: str, severity: str, category: str, finding_id: int) -> None:
    """Notifies about a newly created finding, if notifications are on, this feature's
    notifications are on, and the severity meets the effective threshold (master or this
    feature's own override). Recurrences of an already-known finding never reach this —
    callers only call it for genuinely new findings."""
    if not db.get_notifications_enabled() or not db.get_feature_notify_enabled(source):
        return

    threshold = db.get_effective_severity(source)
    if SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER.get(threshold, 0):
        return

    url = _dashboard_url(f"/findings/{finding_id}")
    label = "Log issue" if source == "logs" else "Compose issue"
    full_title = f"{label} ({severity}): {subject}"
    body = f"{title}\n\nCategory: {category}\n{url}"
    _send(full_title, body)


def send_test_notification() -> tuple[bool, str]:
    """Used by the 'Send test notification' button on the Settings page — reports back
    whether it actually worked rather than silently succeeding either way, since a
    misconfigured Apprise URL is otherwise invisible until a real notification fails."""
    urls = db.get_apprise_urls()
    if not urls:
        return False, "No Apprise URL configured yet."

    import apprise
    try:
        a = apprise.Apprise()
        all_valid = True
        for url in urls:
            if not a.add(url):
                all_valid = False
        if not all_valid:
            return False, "One or more Apprise URLs look malformed — check the format against https://github.com/caronc/apprise."
        success = a.notify(
            title="Release Radar test notification",
            body="If you're seeing this, your Apprise URL is configured correctly.",
        )
        if success:
            return True, "Test notification sent successfully."
        return False, "Apprise reported the send failed — double check the URL and the target service."
    except Exception as exc:
        logger.exception("Test notification failed")
        return False, f"Error: {exc}"
