import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.reconcile import run_check

logger = logging.getLogger("release_radar.scheduler")

_scheduler = BackgroundScheduler(timezone=settings.tz)


def start_scheduler() -> None:
    trigger = CronTrigger.from_crontab(settings.check_schedule_cron)
    _scheduler.add_job(run_check, trigger=trigger, id="periodic_check", replace_existing=True)
    _scheduler.start()
    logger.info("Scheduler started with cron '%s'", settings.check_schedule_cron)


def trigger_check_now() -> None:
    """Runs a check immediately in the background, outside the normal schedule."""
    _scheduler.add_job(run_check, id="manual_check", replace_existing=True)
