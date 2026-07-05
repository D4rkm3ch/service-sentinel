import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.compose_reviewer import run_compose_check
from app.config import settings
from app.log_watcher import run_log_check
from app.reconcile import run_check

logger = logging.getLogger("release_radar.scheduler")

_scheduler = BackgroundScheduler(timezone=settings.tz)


def start_scheduler() -> None:
    # Each run_* function checks its own feature toggle internally and no-ops (with no API
    # calls) if that feature is disabled — so all three jobs are always scheduled, and
    # turning a feature on/off from the UI takes effect on the next tick without needing to
    # touch the scheduler at all.
    _scheduler.add_job(
        run_check, trigger=CronTrigger.from_crontab(settings.check_schedule_cron),
        id="periodic_updates_check", replace_existing=True,
    )
    _scheduler.add_job(
        run_log_check, trigger=CronTrigger.from_crontab(settings.log_check_schedule_cron),
        id="periodic_logs_check", replace_existing=True,
    )
    _scheduler.add_job(
        run_compose_check, trigger=CronTrigger.from_crontab(settings.compose_check_schedule_cron),
        id="periodic_compose_check", replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started: updates='%s' logs='%s' compose='%s'",
        settings.check_schedule_cron, settings.log_check_schedule_cron, settings.compose_check_schedule_cron,
    )


def trigger_check_now() -> None:
    """Runs the updates check immediately in the background, outside the normal schedule."""
    _scheduler.add_job(run_check, id="manual_updates_check", replace_existing=True)


def trigger_log_check_now() -> None:
    _scheduler.add_job(run_log_check, id="manual_logs_check", replace_existing=True)


def trigger_compose_check_now() -> None:
    _scheduler.add_job(run_compose_check, id="manual_compose_check", replace_existing=True)
