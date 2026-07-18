import functools
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app import check_state, db, persist
from app.compose_reviewer import run_compose_check
from app.config import settings
from app.log_watcher import run_log_check
from app.schedule_spec import build_trigger

logger = logging.getLogger("service_sentinel.scheduler")

# settings.tz (the TZ env var) only ever seeds the scheduler's own bootstrap default here,
# used for the brief window before apply_schedules() first runs (which happens immediately
# after this, inside start_scheduler(), once the database is available). Every individual
# job's trigger is built with its own explicit timezone from db.get_timezone() from then on,
# so this module-level default becomes irrelevant in practice -- real jobs never fall back to
# it. Reading db.get_timezone() here directly isn't safe: this runs at import time, before
# db.init_db() has created the app_settings table.
_scheduler = BackgroundScheduler(timezone=settings.tz)


def run_updates_check() -> None:
    """The scheduled job body for Updates. Goes through
    persist.run_updates_check_if_not_running() (Stage 5) rather than calling
    persist.run_and_persist_check() directly, so a scheduled firing that happens to land
    while a manual Check now (or another scheduled firing that ran long) is still in
    progress gets skipped instead of running two overlapping checks. Runs directly on
    APScheduler's own worker thread -- no extra threading.Thread needed, unlike the UI's
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

# Fixed order features run in when 2+ of them share the master/general schedule and would
# otherwise all fire at the exact same instant -- an explicit ask to run sequentially rather
# than compete for CPU/network/AI-rate-limits at once. Matches the nav tab order.
_FEATURE_ORDER = ("updates", "logs", "compose")

_MASTER_CHAIN_JOB_ID = "periodic_master_schedule_chain"


def _run_chain(funcs) -> None:
    """Runs each feature's scheduled job body one after another rather than concurrently --
    each of run_updates_check/run_log_check/run_compose_check already runs synchronously to
    completion on whatever thread calls it (see run_updates_check's own docstring), so simply
    calling them in sequence here, within one APScheduler job, is sufficient: the next one
    can't start until the previous one has actually returned, not just started."""
    for func in funcs:
        func()


def apply_schedules() -> None:
    """(Re)schedules the periodic jobs using whatever the database currently says each
    feature's effective schedule is (its own override, or the master schedule), and whatever
    timezone is currently configured (Stage 5c -- also called right after the Settings page
    saves a timezone change, so a running job's times reinterpret immediately rather than
    waiting for the next restart). Safe to call at any time since replace_existing means it
    just updates the existing job's trigger rather than duplicating it.

    A feature toggled off (db.get_feature_enabled) has its periodic job removed entirely
    rather than scheduled -- this is the ONLY place that toggle is enforced. It deliberately
    does not touch run_updates_check/run_log_check/run_compose_check themselves, since those
    also back the manual Check now button, which must keep working even when the automatic
    schedule is off (a real-world report: the toggle was meant to just pause the schedule, not
    lock the whole feature, but for logs/compose the check functions used to gate themselves
    too, silently breaking their own Check now button; updates never gated itself at all, so
    its toggle did nothing whatsoever). Also called by /settings/toggle/{feature} so flipping
    it takes effect immediately rather than on next restart.

    Two or more enabled features that both follow the master/general schedule (rather than
    their own custom override) fire at the exact same trigger time -- left as independent
    APScheduler jobs, they'd run concurrently and compete for the same resources (registry
    lookups, AI calls). Instead they're grouped into one combined job (_run_chain, in
    _FEATURE_ORDER) that runs them one after another, and their individual per-feature job ids
    are removed so nothing double-fires. A single feature on the master schedule (the other two
    disabled or on their own override) has nothing to sequence against, so it keeps its own
    ordinary individual job exactly as before -- grouping only ever kicks in with 2+ of them."""
    tz = db.get_timezone()
    master_group = []

    for feature in _FEATURE_ORDER:
        func, job_id = _JOBS[feature]
        if not db.get_feature_enabled(feature):
            if _scheduler.get_job(job_id):
                _scheduler.remove_job(job_id)
            logger.info("Schedule removed for %s: feature is disabled", feature)
            continue
        if db.get_feature_uses_master_schedule(feature):
            master_group.append(feature)
            continue
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)
        spec = db.get_effective_schedule(feature)
        trigger = build_trigger(spec, tz=tz)
        _scheduler.add_job(func, trigger=trigger, id=job_id, replace_existing=True)
        logger.info("Schedule applied for %s: %s (tz=%s)", feature, spec, tz)

    if len(master_group) >= 2:
        for feature in master_group:
            _, job_id = _JOBS[feature]
            if _scheduler.get_job(job_id):
                _scheduler.remove_job(job_id)
        spec = db.get_master_schedule()
        trigger = build_trigger(spec, tz=tz)
        funcs = [_JOBS[feature][0] for feature in master_group]
        _scheduler.add_job(
            functools.partial(_run_chain, funcs), trigger=trigger,
            id=_MASTER_CHAIN_JOB_ID, replace_existing=True,
        )
        logger.info("Schedule applied for %s (sequential): %s (tz=%s)", ", ".join(master_group), spec, tz)
    else:
        if _scheduler.get_job(_MASTER_CHAIN_JOB_ID):
            _scheduler.remove_job(_MASTER_CHAIN_JOB_ID)
        for feature in master_group:
            func, job_id = _JOBS[feature]
            spec = db.get_master_schedule()
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


def run_check_all() -> None:
    """The topbar's "Check All" button (POST /checks/check-all) -- an explicit ask to run
    Updates, then Logs, then Compose one at a time, each waiting for the previous one to fully
    finish before the next starts. Same sequential shape as the master schedule's own grouped
    firing (see _run_chain above), just triggered on demand rather than by a cron trigger, so
    it reuses _JOBS/_FEATURE_ORDER directly instead of duplicating that "run these one after
    another" loop.

    Only Check Now's own light, skip-what-isn't-new work -- the topbar's separate Regenerate AI
    Response / Reset & Re-Check buttons (POST /checks/regenerate-all and
    /checks/reset-and-recheck-all, both in main.py) are their own explicit, dedicated actions,
    never folded into this one, so a click that only meant "check everything" can never
    accidentally trigger the heavier, token-costlier ones.

    A Cancel clicked mid-chain (the same sitewide Cancel every other check uses -- see
    check_state.request_cancel_running_checks) stops the chain rather than continuing on to the
    next feature: is_cancel_requested(feature) is checked right after that feature's own run
    function returns, since a cancelled run's own set_finished() call doesn't clear its cancel
    flag -- only claiming the mutex again (set_running/try_start) does, so this reads as a
    reliable "was this one just cancelled" signal without needing any new state of its own."""
    for feature in _FEATURE_ORDER:
        func, _job_id = _JOBS[feature]
        func()
        if check_state.is_cancel_requested(feature):
            break


def run_single_check(feature: str) -> None:
    """Runs one feature's own scheduled-job check function directly and synchronously -- the
    exact same raw function run_check_all/the master schedule use (_JOBS[feature][0]), not the
    async-scheduling trigger_log_check_now/trigger_compose_check_now the UI's per-feature
    Reset & Re-Check buttons use (those hand the work to APScheduler and return immediately).
    Used by main.py's topbar-level Reset & Re-Check All chain, which needs each feature's
    re-check to genuinely finish -- not just get queued -- before it wipes and starts the next
    one, same sequential reasoning as run_check_all above."""
    func, _job_id = _JOBS[feature]
    func()
