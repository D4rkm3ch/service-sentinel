"""A real-world report: tautulli's release notes were genuine security fixes (XSS, path
traversal) but landed on the same "Bug Fixes" badge as a routine version bump -- "Bug Fixes"
undersold what was actually a security patch. Rather than add a whole new severity tier (a
bigger structural change touching sort order, notification thresholds, and badge colors), the
underlying "bugfix" severity value is unchanged everywhere; only its display label changed, in
the two places that show it to a human (the web badge and the Discord notification group
title), so both keep reading the same real value honestly."""

from app.main import SEVERITY_LABELS, severity_label
from app.notifications import UPDATE_SEVERITY_LABELS


def test_web_badge_label_covers_both_routine_fixes_and_security_patches():
    assert SEVERITY_LABELS["updates"]["bugfix"] == "Fixes & Security"
    assert severity_label("updates", "bugfix") == "Fixes & Security"


def test_discord_notification_group_title_matches_the_web_badge():
    assert UPDATE_SEVERITY_LABELS["bugfix"] == "Fixes & Security"
