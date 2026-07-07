import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app import db, persist
from app.compose_reviewer import run_compose_check
from app.config import settings
from app.log_watcher import run_log_check
from app.schedule_spec import build_trigger

logger = logging.getLogger("release_radar.scheduler")

# settings.tz (the TZ env var) only ever seeds the scheduler's own bootstrap default here,
# used for the brief window before apply_schedules() first runs (which happens immediately
# after this, inside start_scheduler(), once the database is available). Every individual
# job's trigger is built with its own explicit timezone from db.get_timezone() from then on,
# so this module-level default becomes irrelevant in practice — real jobs never fall back to
# it. Reading db.get_timezone() here directly isn't safe: this runs at import time, before
# db.init_db() has created the app_settings table.
_scheduler = BackgroundScheduler(timezone=settings.tz)


def run_updates_check() -> None:
    """The scheduled job body for Updates. Goes through
    persist.run_updates_check_if_not_running() (Stage 5) rather than calling
    persist.run_and_persist_check() directly, so a scheduled firing that happens to land
    while a manual Check now (or another scheduled firing that ran long) is still in
    progress gets skipped instead of running two overlapping checks. Runs directly on
    APScheduler's own worker thread — no extra threading.Thread needed, unlike the UI's
    Check now button, which backgrounds itself specifically so the HTTP response can return
    immediately; a scheduled job has no request waiting on it."""
    persist.run_updates_check_if_not_running()


# Stage 1 of the ground-up rebuild removed "updates" from here entirely -- only the manual
# Check now button, which called the check directly rather than going through the scheduler.
# Stage 5 brings it back, now that persistence (Stage 3) and the background-job hardening
# (Stage 4) are both proven solid. Logs and Compose are untouched and keep working exactly
# as they did before.
_JOBS = {
    "updates": (run_updates_check, "periodic_updates_check"),
    "logs": (run_log_check, "periodic_logs_check"),
    "compose": (run_compose_check, "periodic_compose_check"),
}


def apply_schedules() -> None:
    """(Re)schedules the periodic jobs using whatever the database currently says each
    feature's effective schedule is (its own override, or the master schedule), and whatever
    timezone is currently configured (Stage 5c — also called right after the Settings page
    saves a timezone change, so a running job's times reinterpret immediately rather than
    waiting for the next restart). Safe to call at any time since replace_existing means it
    just updates the existing job's trigger rather than duplicating it."""
    tz = db.get_timezone()
    for feature, (func, job_id) in _JOBS.items():
        spec = db.get_effective_schedule(feature)
        trigger = build_trigger(spec, tz=tz)
        _scheduler.add_job(func, trigger=trigger, id=job_id, replace_existing=True)
        logger.info("Schedule applied for %s: %s (tz=%s)", feature, spec, tz)


def start_scheduler() -> None:
    apply_schedules()
    _scheduler.start()
    logger.info("Scheduler started")


def trigger_log_check_now() -> None:
    _scheduler.add_job(run_log_check, id="manual_logs_check", replace_existing=True)


def trigger_compose_check_now() -> None:
    _scheduler.add_job(run_compose_check, id="manual_compose_check", replace_existing=True)
