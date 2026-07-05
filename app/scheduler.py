import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app import db
from app.compose_reviewer import run_compose_check
from app.config import settings
from app.log_watcher import run_log_check
from app.reconcile import run_check
from app.schedule_spec import build_trigger

logger = logging.getLogger("release_radar.scheduler")

_scheduler = BackgroundScheduler(timezone=settings.tz)

_JOBS = {
    "updates": (run_check, "periodic_updates_check"),
    "logs": (run_log_check, "periodic_logs_check"),
    "compose": (run_compose_check, "periodic_compose_check"),
}


def apply_schedules() -> None:
    """(Re)schedules all three periodic jobs using whatever the database currently says each
    feature's effective schedule is (its own override, or the master schedule). Safe to call
    at any time — e.g. right after the settings page saves a change — since replace_existing
    means it just updates the existing job's trigger rather than duplicating it."""
    for feature, (func, job_id) in _JOBS.items():
        spec = db.get_effective_schedule(feature)
        trigger = build_trigger(spec)
        _scheduler.add_job(func, trigger=trigger, id=job_id, replace_existing=True)
        logger.info("Schedule applied for %s: %s", feature, spec)


def start_scheduler() -> None:
    # Each run_* function checks its own feature toggle internally and no-ops (with no API
    # calls) if that feature is disabled — so all three jobs are always scheduled, and
    # turning a feature on/off from the UI takes effect on the next tick without needing to
    # touch the scheduler at all.
    apply_schedules()
    _scheduler.start()
    logger.info("Scheduler started")


def trigger_check_now() -> None:
    """Runs the updates check immediately in the background, outside the normal schedule."""
    _scheduler.add_job(run_check, id="manual_updates_check", replace_existing=True)


def trigger_log_check_now() -> None:
    _scheduler.add_job(run_log_check, id="manual_logs_check", replace_existing=True)


def trigger_compose_check_now() -> None:
    _scheduler.add_job(run_compose_check, id="manual_compose_check", replace_existing=True)
