"""A real-world report: editing a compose file to fix a flagged issue, then clicking Check Now,
didn't make the (now stale) finding disappear -- only Reset & re-check did. Root cause:
compose_reviewer.py only ever added/updated findings, never cleared one a fresh review no longer
reproduced, unlike Logs (which has AI-driven resolution). Fixed with a plain diff against each
review's own fresh output -- unlike Logs, a compose review always sees the file's whole current
content, so no AI judgment call is needed, just "was this title in the findings this pass
produced or not.\""""

from pathlib import Path
from unittest.mock import patch

from app import compose_reviewer, db
from app.config import settings


def _compose_file(name: str, *services: str) -> Path:
    body = "services:\n" + "".join(f"  {s}:\n    image: owner/{s}\n" for s in services)
    path = Path(settings.compose_root) / name
    path.write_text(body)
    return path


def _cleanup(path: Path):
    path.unlink()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM compose_file_state WHERE file_path = ?", (str(path),))
        conn.execute("DELETE FROM findings WHERE subject = ?", (str(path),))


def test_check_now_resolves_a_finding_the_fresh_review_no_longer_produces():
    compose_file = _compose_file("resolve-fixed.yml", "resolve-fixed-svc")
    try:
        with patch("app.compose_reviewer.review_compose_file", return_value=[
            {"title": "Bad mount", "category": "security", "severity": "warning", "description": "d"},
        ]):
            compose_reviewer.run_compose_check_for([compose_file])
        findings = db.list_findings_for_subject("compose", str(compose_file), include_silenced=True)
        assert [f["title"] for f in findings] == ["Bad mount"]

        # The operator edits the file to fix it -- content actually changes, so the hash moves
        # and a real re-review happens, but this time the AI (correctly) finds nothing wrong.
        compose_file.write_text(compose_file.read_text() + "    restart: unless-stopped\n")
        with patch("app.compose_reviewer.review_compose_file", return_value=[]):
            compose_reviewer.run_compose_check_for([compose_file])

        findings = db.list_findings_for_subject("compose", str(compose_file), include_silenced=True)
        assert findings == []
    finally:
        _cleanup(compose_file)


def test_check_now_keeps_findings_still_present_in_the_fresh_review_and_resolves_only_the_others():
    compose_file = _compose_file("resolve-partial.yml", "resolve-partial-svc")
    try:
        with patch("app.compose_reviewer.review_compose_file", return_value=[
            {"title": "Still broken", "category": "reliability", "severity": "warning", "description": "d"},
            {"title": "Now fixed", "category": "security", "severity": "critical", "description": "d"},
        ]):
            compose_reviewer.run_compose_check_for([compose_file])

        compose_file.write_text(compose_file.read_text() + "    restart: unless-stopped\n")
        with patch("app.compose_reviewer.review_compose_file", return_value=[
            {"title": "Still broken", "category": "reliability", "severity": "warning", "description": "d"},
        ]):
            compose_reviewer.run_compose_check_for([compose_file])

        findings = db.list_findings_for_subject("compose", str(compose_file), include_silenced=True)
        assert [f["title"] for f in findings] == ["Still broken"]
    finally:
        _cleanup(compose_file)


def test_check_now_does_not_resolve_anything_for_a_file_whose_hash_is_unchanged():
    """A file whose content hasn't changed is skipped entirely in the fast pass -- never
    reviewed this round, so nothing about its findings should be touched, resolved or
    otherwise."""
    compose_file = _compose_file("resolve-unchanged.yml", "resolve-unchanged-svc")
    try:
        with patch("app.compose_reviewer.review_compose_file", return_value=[
            {"title": "Some issue", "category": "reliability", "severity": "warning", "description": "d"},
        ]):
            compose_reviewer.run_compose_check_for([compose_file])

        with patch("app.compose_reviewer.review_compose_file") as mocked_review:
            compose_reviewer.run_compose_check_for([compose_file])
            mocked_review.assert_not_called()

        findings = db.list_findings_for_subject("compose", str(compose_file), include_silenced=True)
        assert [f["title"] for f in findings] == ["Some issue"]
    finally:
        _cleanup(compose_file)


def test_check_now_does_not_resolve_a_silenced_finding():
    compose_file = _compose_file("resolve-silenced.yml", "resolve-silenced-svc")
    try:
        with patch("app.compose_reviewer.review_compose_file", return_value=[
            {"title": "Silenced issue", "category": "reliability", "severity": "warning", "description": "d"},
        ]):
            compose_reviewer.run_compose_check_for([compose_file])
        db.silence_all_findings_for_subjects("compose", [str(compose_file)])

        compose_file.write_text(compose_file.read_text() + "    restart: unless-stopped\n")
        with patch("app.compose_reviewer.review_compose_file", return_value=[]):
            compose_reviewer.run_compose_check_for([compose_file])

        findings = db.list_findings_for_subject("compose", str(compose_file), include_silenced=True)
        assert len(findings) == 1
        assert findings[0]["status"] == "silenced"
    finally:
        _cleanup(compose_file)


def test_a_silenced_finding_reaches_review_compose_file_as_tracked_context():
    """The wiring behind the silenced-duplicate fix (a real-world report: the same misconfigured
    setting kept getting re-titled on separate reviews of the same file, e.g. "Insecure
    authentication setting" vs "Mousehole allows no authentication", spawning a fresh unread
    duplicate instead of bumping the original -- because a silenced finding used to drop out of
    review_compose_file's context entirely). run_compose_check_for must fetch this file's tracked
    findings with include_silenced=True and pass them through as active_findings."""
    compose_file = _compose_file("silenced-context.yml", "silenced-context-svc")
    try:
        with patch("app.compose_reviewer.review_compose_file", return_value=[
            {"title": "Insecure authentication setting", "category": "security",
             "severity": "critical", "description": "no-auth is enabled"},
        ]):
            compose_reviewer.run_compose_check_for([compose_file])
        db.silence_all_findings_for_subjects("compose", [str(compose_file)])

        compose_file.write_text(compose_file.read_text() + "    restart: unless-stopped\n")
        captured = {}

        def fake_review(path_str, redacted, include_fix=False, active_findings=None):
            captured["active_findings"] = active_findings
            return [{"title": "Insecure authentication setting", "category": "security",
                     "severity": "critical", "description": "no-auth is enabled"}]

        with patch("app.compose_reviewer.review_compose_file", side_effect=fake_review):
            compose_reviewer.run_compose_check_for([compose_file])

        tracked = captured["active_findings"]
        assert len(tracked) == 1
        assert tracked[0]["title"] == "Insecure authentication setting"
        assert tracked[0]["status"] == "silenced"

        # And it stayed the single silenced row -- reusing the title merged the recurrence
        # instead of spawning a second, unread duplicate.
        findings = db.list_findings_for_subject("compose", str(compose_file), include_silenced=True)
        assert len(findings) == 1
        assert findings[0]["status"] == "silenced"
        assert findings[0]["occurrence_count"] == 2
    finally:
        _cleanup(compose_file)
