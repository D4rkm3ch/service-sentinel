"""Notification dispatch — Apprise only. All settings (enabled, URLs, per-feature toggles,
severity thresholds) live in the database and are configured from the Settings tab, not env
vars, matching the pattern the rest of the app's runtime configuration follows.

No emojis anywhere in this product, ever — severity is communicated through text (labels,
ANSI-colored text inside a Discord code block, and the message's own accent color) rather than
symbols.

Stage 10 originally fired one Apprise call per genuinely new/changed update. Real-world use on
a ~60-container fleet showed that as a wall of a dozen-plus separate Discord messages landing
within seconds of each other for one check run. notify_updates_digest() replaces that: persist.py
collects every notify-worthy item across a whole check run and this fires exactly one Apprise
call for the batch — see persist.py's to_notify list and this function's own docstring.

Apprise's Discord plugin only builds a colored embed — and only renders the ANSI color codes
notify_updates_digest() uses — when the target webhook URL itself includes `?format=markdown`
(e.g. `discord://id/token/?format=markdown`); a bare discord:// URL falls back to one flat
plain-text message with no color at all. See settings.html's Apprise URL hint.
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
# Discord's supported ANSI foreground codes for code-block text coloring, picked to roughly
# match this severity's badge color everywhere else in the app (see style.css's badge-sev-*).
_UPDATE_ANSI_CODE = {"bugfix": "30", "feature": "32", "action_needed": "33", "breaking": "31"}


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


def _ansi_severity_block(severity: str) -> str:
    """A severity label rendered in its own fenced ```ansi block so Discord colors the text
    itself (red/orange/green/gray) -- deliberately its own block rather than inlined next to
    the container name, since a fenced code block is always a standalone element in markdown
    and can't share a line with surrounding text. Falls back to plain (uncolored) text on
    anything that doesn't render Discord's ANSI code-block extension -- still readable, just
    not colored."""
    code = _UPDATE_ANSI_CODE.get(severity, "37")
    label = UPDATE_SEVERITY_LABELS.get(severity, severity.capitalize())
    return f"```ansi\n\x1b[1;{code}m{label}\x1b[0m\n```"


def _worst_notify_type(qualifying: list[dict], has_errors: bool) -> str:
    ranks = [UPDATE_SEVERITY_ORDER.get(i["severity"], 0) for i in qualifying]
    if has_errors:
        ranks.append(UPDATE_SEVERITY_ORDER["breaking"])  # a check failure is as worth flagging as a breaking change
    worst_rank = max(ranks, default=0)
    worst_severity = next(sev for sev, rank in UPDATE_SEVERITY_ORDER.items() if rank == worst_rank)
    return _UPDATE_NOTIFY_TYPE[worst_severity]


def notify_updates_digest(items: list[dict], errors: list[dict]) -> None:
    """One Apprise call for a whole check run's worth of notify-worthy updates (and, if opted
    into via the Settings toggle, check errors) -- never one call per update, so a check that
    finds a dozen updates sends one message, not a dozen. items/errors are the full, unfiltered
    candidate lists from persist.py; every filtering decision (master/feature toggle, severity
    threshold, the registry-error opt-in) happens exactly once here rather than once per item,
    so a big batch costs the same handful of Settings reads as a small one.

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
    qualifying = sorted(
        (i for i in items if UPDATE_SEVERITY_ORDER.get(i["severity"], 0) >= UPDATE_SEVERITY_ORDER.get(threshold, 0)),
        key=lambda i: UPDATE_SEVERITY_ORDER.get(i["severity"], 0), reverse=True,
    )
    qualifying_errors = errors if (errors and db.get_notify_updates_include_errors()) else []

    if not qualifying and not qualifying_errors:
        return

    sections = []
    for item in qualifying:
        url = _dashboard_url(f"/updates/{item['update_id']}")
        sections.append(
            f"{_ansi_severity_block(item['severity'])}\n"
            f"**{item['container_name']}** — `{item['image_repo']}:{item['tag']}` — [View]({url})"
        )
    if qualifying_errors:
        error_lines = [f"**{len(qualifying_errors)} container{'s' if len(qualifying_errors) != 1 else ''} couldn't be checked**"]
        for err in qualifying_errors:
            error_lines.append(f"- **{err['container_name']}** — {err['error']}")
        sections.append("\n".join(error_lines))

    body = "\n\n---\n\n".join(sections)
    body += f"\n\n[View all updates]({_dashboard_url('/updates')})"

    title_parts = []
    if qualifying:
        title_parts.append(f"{len(qualifying)} update{'s' if len(qualifying) != 1 else ''}")
    if qualifying_errors:
        title_parts.append(f"{len(qualifying_errors)} check error{'s' if len(qualifying_errors) != 1 else ''}")
    title = " · ".join(title_parts)

    _send(title, body, _worst_notify_type(qualifying, bool(qualifying_errors)))


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
