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

_ASSET below strips Apprise's own default branding (the "Apprise" author line and its megaphone
avatar) entirely rather than replacing it with our own -- the webhook's own name/icon (set
directly in Discord, e.g. "Spidey Bot") already identifies the source, so repeating it inside
every message would just be noise.
"""

import logging

import apprise
from apprise import NotifyFormat, NotifyType

from app import db

logger = logging.getLogger("service_sentinel.notifications")

# app_id="" removes the small embed author line entirely (Apprise defaults it to "Apprise").
# image_url_mask/image_url_logo="" makes Apprise's own asset.image_url() return None for every
# notify_type, so the Discord plugin's `if self.avatar and (image_url or self.avatar_url):`
# check never has anything to set avatar_url to -- the payload simply omits it, and Discord
# falls back to the webhook's own configured avatar instead of Apprise's branded icon. No
# webhook URL change needed for any of this (no `?avatar=no` required) -- it's all asset-level.
#
# html_notify_map overrides Apprise's own default embed colors (a generic blue/green/yellow/red)
# with this app's own severity colors, so a Discord message's accent bar matches the same
# severity's badge color on the dashboard instead of looking unrelated -- see style.css's
# --text-dim/--accent/--warn/--error and the .badge-sev-*/.severity-btn-* rules built on them.
# Keep these hex values in sync with style.css's :root if that palette ever changes.
_ASSET = apprise.AppriseAsset(
    app_id="", image_url_mask="", image_url_logo="",
    html_notify_map={
        NotifyType.INFO: "#868c98",     # --text-dim (bugfix, suggestion)
        NotifyType.SUCCESS: "#5ec9a6",  # --accent (feature)
        NotifyType.WARNING: "#d9a441",  # --warn (action_needed, warning)
        NotifyType.FAILURE: "#d9705e",  # --error (breaking, critical)
    },
)

FINDING_SEVERITY_ORDER = {"suggestion": 0, "warning": 1, "critical": 2}
UPDATE_SEVERITY_ORDER = {"bugfix": 0, "feature": 1, "action_needed": 2, "breaking": 3}
UPDATE_SEVERITY_LABELS = {
    # Kept in sync with main.py's SEVERITY_LABELS["updates"] -- see that map's own comment for
    # why "bugfix" reads as "Fixes & Security" now (it also covers genuine security patches).
    "bugfix": "Fixes & Security", "feature": "New Features",
    "action_needed": "Action Needed", "breaking": "Breaking Change",
}
_UPDATE_NOTIFY_TYPE = {
    "bugfix": NotifyType.INFO, "feature": NotifyType.SUCCESS,
    "action_needed": NotifyType.WARNING, "breaking": NotifyType.FAILURE,
}
# Sent lowest-severity-first so, in a Discord channel (newest message at the bottom), the most
# severe group of updates ends up as the most recent, most visible message.
_SEVERITY_SEND_ORDER = ("bugfix", "feature", "action_needed", "breaking")


def _send(title: str, body: str, notify_type: str = NotifyType.INFO) -> None:
    urls = db.get_apprise_urls()
    if not urls:
        return
    try:
        a = apprise.Apprise(asset=_ASSET)
        for url in urls:
            a.add(url)
        a.notify(title=title, body=body, notify_type=notify_type, body_format=NotifyFormat.MARKDOWN)
    except Exception:
        logger.exception("Apprise notification failed")


def _format_update_line(item: dict) -> str:
    """"container" alone, or "container • vX.Y.Z" when persist.py could resolve a new version
    from the release notes (see release_notes.extract_latest_version) -- most reliably true for
    the GitHub-releases path, None (so just the bare name) for anything else, including a
    still-unresolved version. No "old version" shown -- this app tracks image digests, not
    versions, so there's no reliably known "what it was running before" to pair it with."""
    line = f"**{item['container_name']}**"
    version = item.get("new_version")
    if version:
        line += f" • v{version.lstrip('vV')}"
    return line


def _send_severity_group(severity: str, group: list[dict]) -> None:
    """Title is just the feature name ("Update Issues") -- the severity + count moves into the
    body's first line instead, since Discord renders an embed title larger/bolder than its body,
    giving the same "big line, smaller line under it" reading the severity used to get from
    being the title on its own, without needing two separate Apprise calls."""
    label = UPDATE_SEVERITY_LABELS.get(severity, severity.capitalize())
    count = len(group)
    sections = [_format_update_line(item) for item in sorted(group, key=lambda i: i["container_name"].lower())]
    body = f"**{label} ({count})**\n\n" + "\n\n".join(sections)
    _send("Update Issues", body, _UPDATE_NOTIFY_TYPE.get(severity, NotifyType.INFO))


def _send_error_group(group: list[dict]) -> None:
    count = len(group)
    title = f"Check errors ({count})"
    lines = [
        f"- **{err['container_name']}** — {err['error']}"
        for err in sorted(group, key=lambda e: e["container_name"].lower())
    ]
    body = "\n".join(lines)
    _send(title, body, NotifyType.FAILURE)


def notify_updates_digest(items: list[dict], errors: list[dict]) -> None:
    """One Apprise call per severity level present in a whole check run's worth of
    notify-worthy updates, plus one more for check errors if opted into via the Settings
    toggle -- never one call per update. items/errors are the full, unfiltered candidate lists
    from persist.py; every filtering decision (master/feature toggle, severity threshold, the
    registry-error opt-in) happens exactly once here rather than once per item, so a big batch
    costs the same handful of Settings reads as a small one.

    items: [{"container_name", "image_repo", "tag", "update_id", "severity", "new_version"}, ...]
    -- severity expected non-blank (persist.py only includes rows that actually have one).
    new_version may be None (see release_notes.extract_latest_version) -- shown inline next to
    the container name when resolved, omitted otherwise (see _format_update_line).
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


def notify_logs_check_errors(errors: list[dict]) -> None:
    """Logs' counterpart to notify_updates_digest's error-group half -- a container whose logs
    couldn't be fetched this check (Docker socket blip, container removed mid-check) doesn't
    have a severity to threshold against the way a real finding does, so it's opt-in only via
    the same 'notify on check errors' shape Updates has (db.get_notify_logs_include_errors),
    independent of Logs' own severity threshold, and kept as its own call rather than folded
    into notify_findings_digest below (see log_watcher.py, which calls both once per check).

    errors: [{"container_name", "error"}, ...]."""
    if not errors:
        return
    if not db.get_notifications_enabled() or not db.get_feature_notify_enabled("logs"):
        return
    if not db.get_notify_logs_include_errors():
        return
    _send_error_group(errors)


def notify_compose_check_errors(errors: list[dict]) -> None:
    """Compose's counterpart to notify_logs_check_errors -- a file that couldn't be read or
    reviewed this check doesn't have a severity to threshold against the way a real finding
    does, so it's opt-in only via the same 'notify on check errors' shape Updates/Logs have
    (db.get_notify_compose_include_errors), independent of Compose's own severity threshold.

    errors: [{"container_name": file_path, "error"}, ...] -- reuses _send_error_group's own
    "container_name" key (it just needs SOME identifying name per row to bold in the message,
    the file path fits that exactly the same way a container name does)."""
    if not errors:
        return
    if not db.get_notifications_enabled() or not db.get_feature_notify_enabled("compose"):
        return
    if not db.get_notify_compose_include_errors():
        return
    _send_error_group(errors)


FINDING_SEVERITY_LABELS = {"suggestion": "Suggestions", "warning": "Warnings", "critical": "Critical"}
_FINDING_NOTIFY_TYPE = {
    "suggestion": NotifyType.INFO, "warning": NotifyType.WARNING, "critical": NotifyType.FAILURE,
}
# Sent lowest-severity-first, same reasoning as _SEVERITY_SEND_ORDER above.
_FINDING_SEVERITY_SEND_ORDER = ("suggestion", "warning", "critical")
_FINDING_SOURCE_LABELS = {"logs": "Runtime Issues", "compose": "Configuration Issues"}


def _send_finding_severity_group(source: str, severity: str, group: list[dict]) -> None:
    """Title is just the feature's display name ("Runtime Issues"/"Configuration Issues") --
    same title/body split as Updates' _send_severity_group above, for the same reason (see its
    docstring)."""
    label = FINDING_SEVERITY_LABELS.get(severity, severity.capitalize())
    count = len(group)
    title = _FINDING_SOURCE_LABELS.get(source, source.capitalize())
    sections = [
        f"**{item['subject']}** • {item['title']}"
        for item in sorted(group, key=lambda i: i["subject"].lower())
    ]
    body = f"**{label} ({count})**\n\n" + "\n\n".join(sections)
    _send(title, body, _FINDING_NOTIFY_TYPE.get(severity, NotifyType.INFO))


def notify_findings_digest(source: str, items: list[dict]) -> None:
    """Logs/Compose's counterpart to notify_updates_digest -- one Apprise call per severity
    level present among a check run's genuinely new findings, matching Updates' shape exactly
    (see notify_updates_digest's docstring for why: one message per severity keeps the accent
    color trustworthy, one batched call per check keeps a busy run from flooding the channel).
    Callers (log_watcher.py, compose_reviewer.py) collect every genuinely new finding across one
    whole check run/scope and call this once at the end, the same way persist.py collects a
    whole check's worth of updates before calling notify_updates_digest once.

    items: [{"subject", "severity", "title"}, ...] -- subject is the container name (Logs) or a
    display name for the compose file (Compose -- see compose_lookup.subject_display_name,
    callers resolve this before it ever reaches here so the message shows a real service name
    instead of a raw file path). "title" is the finding's own short headline (e.g. "Missing
    healthcheck"), shown inline next to the subject so the message says what's wrong without
    a click -- not to be confused with the Apprise message's own title, see below. Recurrences
    of an already-known finding must never be included -- callers only pass genuinely new
    findings.

    The Apprise message's own title ("Runtime Issues" vs "Configuration Issues") is what tells
    two features' messages apart in the same channel -- Logs and Compose share the same severity
    label set (unlike Updates' distinct bugfix/feature/action_needed/breaking labels), so
    without a distinct title their messages would otherwise be indistinguishable."""
    if not items:
        return
    if not db.get_notifications_enabled() or not db.get_feature_notify_enabled(source):
        return

    threshold = db.get_effective_severity(source)
    qualifying = [
        i for i in items
        if FINDING_SEVERITY_ORDER.get(i["severity"], 0) >= FINDING_SEVERITY_ORDER.get(threshold, 0)
    ]
    if not qualifying:
        return

    by_severity: dict[str, list[dict]] = {}
    for item in qualifying:
        by_severity.setdefault(item["severity"], []).append(item)
    for severity in _FINDING_SEVERITY_SEND_ORDER:
        group = by_severity.get(severity)
        if group:
            _send_finding_severity_group(source, severity, group)


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
        a = apprise.Apprise(asset=_ASSET)
        all_valid = True
        for url in urls:
            if not a.add(url):
                all_valid = False
        if not all_valid:
            return False, "One or more Apprise URLs look malformed — check the format against https://github.com/caronc/apprise."
        success = a.notify(
            title="Service Sentinel test notification",
            body="If you're seeing this, your Apprise URL is configured correctly.",
        )
        if success:
            return True, "Test notification sent successfully."
        return False, "Apprise reported the send failed — double check the URL and the target service."
    except Exception as exc:
        logger.exception("Test notification failed")
        return False, f"Error: {exc}"
