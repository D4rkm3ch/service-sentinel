import logging

from app import db
from app.check_state import set_finished, set_running
from app.config import settings
from app.docker_client import get_container_logs_since, list_running_containers_for_logs
from app.log_filter import extract_suspicious_excerpt
from app.notifications import notify_finding
from app.summarizer import analyze_logs_batch

logger = logging.getLogger("release_radar.log_watcher")


def run_log_check() -> dict:
    """Runs one full log-health pass: for every non-ignored running container, pull logs
    since the last check (or the configured lookback window on first run), keep only lines
    that matched a suspicious keyword locally, and — only for containers that actually had
    something worth showing — send those excerpts to Claude for triage. Containers with
    clean logs never reach the API at all."""
    if not db.get_feature_enabled("logs"):
        return {"skipped": True}

    set_running("logs")
    checked = 0
    findings_found = 0

    try:
        containers = list_running_containers_for_logs()
    except Exception:
        logger.exception("Could not reach the Docker socket — skipping this log check")
        result = {"checked": 0, "findings_found": 0, "errors": 1}
        set_finished("logs", result)
        return result

    excerpts_by_container: dict[str, str] = {}
    for container in containers:
        checked += 1
        checkpoint = db.get_log_watch_checkpoint(container.name)
        try:
            log_text = get_container_logs_since(
                container.name, checkpoint, settings.log_max_lines_per_container
            )
        except Exception:
            logger.exception("Could not fetch logs for %s", container.name)
            continue

        db.set_log_watch_checkpoint(container.name)

        excerpt = extract_suspicious_excerpt(log_text) if log_text else None
        if excerpt:
            excerpts_by_container[container.name] = excerpt

    if not excerpts_by_container:
        logger.info("Log check complete: %d containers checked, all clean", checked)
        result = {"checked": checked, "findings_found": 0, "errors": 0}
        set_finished("logs", result)
        return result

    try:
        findings = analyze_logs_batch(excerpts_by_container)
    except Exception:
        logger.exception("Log triage AI call failed")
        result = {"checked": checked, "findings_found": 0, "errors": 1}
        set_finished("logs", result)
        return result

    for finding in findings:
        container_name = finding.get("container")
        title = finding.get("title")
        if not container_name or not title:
            continue
        finding_id, is_new = db.upsert_finding(
            source="logs",
            subject=container_name,
            title=title,
            category=finding.get("category", "error"),
            severity=finding.get("severity", "warning"),
            description_markdown=finding.get("description", ""),
        )
        findings_found += 1
        if is_new:
            notify_finding("logs", container_name, title, finding.get("severity", "warning"),
                            finding.get("category", "error"), finding_id)

    logger.info("Log check complete: %d containers checked, %d findings", checked, findings_found)
    result = {"checked": checked, "findings_found": findings_found, "errors": 0}
    set_finished("logs", result)
    return result
