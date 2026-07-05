import hashlib
import logging

from app import db
from app.check_state import set_finished, set_running
from app.compose_lookup import list_compose_files, redact_compose_file_text
from app.notifications import notify_finding
from app.summarizer import review_compose_file

logger = logging.getLogger("release_radar.compose_reviewer")


def run_compose_check() -> dict:
    """Hashes every compose file release-radar can see. A file that's new (never hashed
    before) or changed (hash differs from what's stored) gets reviewed by Claude; anything
    unchanged is skipped entirely — this is what keeps the feature cheap over time, since
    editing a stack is infrequent."""
    if not db.get_feature_enabled("compose"):
        return {"skipped": True}

    set_running("compose")
    checked = 0
    reviewed = 0
    findings_found = 0
    errors = 0

    try:
        files = list_compose_files()
    except Exception:
        logger.exception("Could not list compose files — skipping this compose check")
        result = {"checked": 0, "reviewed": 0, "findings_found": 0, "errors": 1}
        set_finished("compose", result)
        return result

    for path in files:
        checked += 1
        try:
            content = path.read_text()
        except OSError:
            errors += 1
            continue

        content_hash = hashlib.sha256(content.encode()).hexdigest()
        previous_hash = db.get_compose_file_hash(str(path))
        if previous_hash == content_hash:
            continue

        redacted = redact_compose_file_text(path)
        if redacted is None:
            db.set_compose_file_hash(str(path), content_hash)
            continue

        try:
            include_fix = db.get_deep_analysis_enabled("compose")
            findings = review_compose_file(str(path), redacted, include_fix=include_fix)
        except Exception:
            logger.exception("Compose review AI call failed for %s", path)
            errors += 1
            continue

        reviewed += 1
        for finding in findings:
            title = finding.get("title")
            if not title:
                continue
            finding_id, is_new = db.upsert_finding(
                source="compose",
                subject=str(path),
                title=title,
                category=finding.get("category", "reliability"),
                severity=finding.get("severity", "warning"),
                description_markdown=finding.get("description", ""),
                suggested_fix=finding.get("fix"),
            )
            findings_found += 1
            if is_new:
                notify_finding("compose", str(path), title, finding.get("severity", "warning"),
                                finding.get("category", "reliability"), finding_id)

        db.set_compose_file_hash(str(path), content_hash)

    logger.info(
        "Compose check complete: %d files checked, %d reviewed, %d findings, %d errors",
        checked, reviewed, findings_found, errors,
    )
    result = {"checked": checked, "reviewed": reviewed, "findings_found": findings_found, "errors": errors}
    set_finished("compose", result)
    return result
