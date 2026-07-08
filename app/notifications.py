"""Notification dispatch — Apprise only. All settings (enabled, URLs, per-feature toggles,
severity thresholds) live in the database and are configured from the Settings tab, not env
vars, matching the pattern the rest of the app's runtime configuration follows.

No emojis anywhere in this product, ever — severity is communicated through text labels and
each message's own accent color (the vertical bar Discord shows down the left edge of an
embed), never symbols.

Stage 10 originally fired one Apprise call per genuinely new/changed update. Real-world use on
a ~60-container fleet showed that as a wall of a dozen-plus separate Discord messages landing
within seconds of each other for one check run. notify_updates_digest() replaced that with one
combined call for the whole batch -- but a single message can only carry one accent color, so a
batch spanning several severities either picked one color for everything or lost the color
signal entirely. This version instead sends one call PER SEVERITY LEVEL present in the batch
(plus one more for check errors, if opted in) -- every "Breaking Change" message is red, every
"Bug Fixes" message is the info color, etc., so the color itself is always trustworthy at a
glance rather than "whatever was worst across an unrelated mix of updates" -- still nowhere
near Stage 10's original one-call-per-container flood, since everything of the same severity in
one check still batches into a single message.

Apprise's Discord plugin only builds a colored embed at all when the target webhook URL itself
includes `?format=markdown` (e.g. `discord://id/token/?format=markdown`); a bare discord://
URL falls back to a flat plain-text message with no color. See settings.html's Apprise URL hint.
"""

import logging

import apprise
from apprise import NotifyFormat, NotifyType

from app import db
from app.config import settings

logger = logging.getLogger("release_radar.notifications")

FINDING_SEVERITY_ORDER = {"suggestion": 0, "warning": 1, "critical": 2}
UPDATE_SEVERITY_ORDER = {"bugfix": 0, "feature": 1, "action_needed": 2, "breaking": 3}
UPDATE_SEVERITY_LABELS = {
    "bugfix": "Bug Fixes", "feature": "New Features",
    "action_needed": "Action Needed", "breaking": "Breaking Change",
}
_UPDATE_NOTIFY_TYPE = {
    "bugfix": NotifyType.INFO, "feature": NotifyType.SUCCESS,
    "action_needed": NotifyType.WARNING, "breaking": NotifyType.FAILURE,
}
# Sent lowest-severity-first so, in a Discord channel (newest message at the bottom), the most
# severe group of updates ends up as the most recent, most visible message.
_SEVERITY_SEND_ORDER = ("bugfix", "feature", "action_needed", "breaking")


def _dashboard_url(path: str) -> str:
    return f"{settings.public_url}{path}" if settings.public_url else path


def _send(title: str, body: str, notify_type: str = NotifyType.INFO) -> None:
    urls = db.get_apprise_urls()
    if not urls:
        return
    try:
        a = apprise.Apprise()
        for url in urls:
            a.add(url)
        a.notify(title=title, body=body, notify_type=notify_type, body_format=NotifyFormat.MARKDOWN)
    except Exception:
        logger.exception("Apprise notification failed")


def _send_severity_group(severity: str, group: list[dict]) -> None:
    label = UPDATE_SEVERITY_LABELS.get(severity, severity.capitalize())
    count = len(group)
    title = f"{label} — {count} update{'s' if count != 1 else ''}"
    sections = []
    for item in sorted(group, key=lambda i: i["container_name"].lower()):
        url = _dashboard_url(f"/updates/{item['update_id']}")
        sections.append(f"**{item['container_name']}** — `{item['image_repo']}:{item['tag']}` — [View]({url})")
    body = "\n\n---\n\n".join(sections)
    body += f"\n\n[View all updates]({_dashboard_url('/updates')})"
    _send(title, body, _UPDATE_NOTIFY_TYPE.get(severity, NotifyType.INFO))


def _send_error_group(group: list[dict]) -> None:
    count = len(group)
    title = f"Check errors — {count} container{'s' if count != 1 else ''}"
    lines = [
        f"- **{err['container_name']}** — {err['error']}"
        for err in sorted(group, key=lambda e: e["container_name"].lower())
    ]
    body = "\n".join(lines)
    body += f"\n\n[View all updates]({_dashboard_url('/updates')})"
    _send(title, body, NotifyType.FAILURE)


def notify_updates_digest(items: list[dict], errors: list[dict]) -> None:
    """One Apprise call per severity level present in a whole check run's worth of
    notify-worthy updates, plus one more for check errors if opted into via the Settings
    toggle -- never one call per update. items/errors are the full, unfiltered candidate lists
    from persist.py; every filtering decision (master/feature toggle, severity threshold, the
    registry-error opt-in) happens exactly once here rather than once per item, so a big batch
    costs the same handful of Settings reads as a small one.

    items: [{"container_name", "image_repo", "tag", "update_id", "severity"}, ...] -- severity
    expected non-blank (persist.py only includes rows that actually have one).
    errors: [{"container_name", "image_repo", "tag", "update_id", "error"}, ...]

    A still-unknown severity was already filtered out by persist.py before this is even called
    (nothing meaningful to compare against a threshold) -- see persist_check_outcome()."""
    if not items and not errors:
        return
    if not db.get_notifications_enabled() or not db.get_feature_notify_enabled("updates"):
        return

    threshold = db.get_effective_severity("updates")
    qualifying = [i for i in items if UPDATE_SEVERITY_ORDER.get(i["severity"], 0) >= UPDATE_SEVERITY_ORDER.get(threshold, 0)]
    qualifying_errors = errors if (errors and db.get_notify_updates_include_errors()) else []

    if not qualifying and not qualifying_errors:
        return

    if qualifying_errors:
        _send_error_group(qualifying_errors)

    by_severity: dict[str, list[dict]] = {}
    for item in qualifying:
        by_severity.setdefault(item["severity"], []).append(item)
    for severity in _SEVERITY_SEND_ORDER:
        group = by_severity.get(severity)
        if group:
            _send_severity_group(severity, group)


def notify_finding(source: str, subject: str, title: str, severity: str, category: str, finding_id: int) -> None:
    """Notifies about a newly created finding, if notifications are on, this feature's
    notifications are on, and the severity meets the effective threshold (master or this
    feature's own override). Recurrences of an already-known finding never reach this —
    callers only call it for genuinely new findings."""
    if not db.get_notifications_enabled() or not db.get_feature_notify_enabled(source):
        return

    threshold = db.get_effective_severity(source)
    if FINDING_SEVERITY_ORDER.get(severity, 0) < FINDING_SEVERITY_ORDER.get(threshold, 0):
        return

    url = _dashboard_url(f"/findings/{finding_id}")
    label = "Log issue" if source == "logs" else "Compose issue"
    full_title = f"{label} ({severity}): {subject}"
    body = f"{title}\n\nCategory: {category}\n{url}"
    _send(full_title, body)


def send_test_notification(urls: list[str] | None = None) -> tuple[bool, str]:
    """Used by the 'Send test notification' button on the Settings page — reports back
    whether it actually worked rather than silently succeeding either way, since a
    misconfigured Apprise URL is otherwise invisible until a real notification fails.

    Accepts an explicit URL list so the caller can test whatever's currently typed in the
    box, whether or not it's been saved yet — the route only persists it if this succeeds.
    """
    if urls is None:
        urls = db.get_apprise_urls()
    if not urls:
        return False, "No Apprise URL configured yet."

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
