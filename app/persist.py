"""Stage 3 of the ground-up rebuild: persistence. Stage 5 adds the guarded entry point that
lets automatic scheduled checks and manual UI checks share one "only one at a time" invariant.
Stage 7 adds AI summarization: a per-service summary_markdown + severity classification,
generated from Stage 6's fetched release notes plus the operator's own compose config for
that service, for the exact same set of genuinely-new/changed updates that get fresh release
notes. Deliberately no toggle for this one — it's the "single path" base tier, unlike the
optional stack-level cross-service analysis (see stacks.run_stack_analysis_pass(), also called
from here, once the write transaction commits, gated by its own Deep Analysis toggle). Stage 10
wires up real notifications: every write that's genuinely new/changed (not a repeat of the
exact same pending transition) is collected into a candidate list, and offered to
notifications.notify_updates_digest() as a single batched call once the write transaction has
committed — see persist_check_outcome()'s to_notify list. Stage 11 deduplicates release notes
fetching: containers sharing an image:tag (see reconcile.py's matching registry-check dedup)
fetch notes once between them, not once each — see _fetch_release_notes_deduped().

Wraps reconcile.run_check() and writes its outcome into SQLite, so the Tracked Containers
table and per-update/per-stack detail pages survive restarts and get real database ids to
link to, instead of Stage 1/2's request-lifetime-only in-memory cache. Kept as its own module
rather than folded into reconcile.py, so reconcile.py stays exactly what its own docstring
promises: a pure, database-free check function.

An update record exists in the database if and only if that container currently needs
attention (a pending update or a check error) — there's no separate "resolved" flag. Once a
container catches up to the digest a record was tracking, or that record gets superseded by a
newer transition, the old row is deleted rather than marked done. This mirrors exactly what
Stage 1/2's in-memory outcome already showed on every check (current state, not a history
log) — just made durable across restarts, with at most one update row per container at a
time.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable

from app import ai_provider, check_state, compose_lookup, db, notifications, reconcile, release_notes, stacks
from app.summarizer import generate_upgrade_guidance, summarize_update

logger = logging.getLogger("service_sentinel.persist")

_REGISTRY_ERROR_TEXT = "Could not reach the registry to check for an update."


ProgressFunc = Callable[[str, int, int], None]


def run_and_persist_check(on_progress: ProgressFunc | None = None) -> dict:
    """Runs one check and persists its outcome, unconditionally — no running-flag guard, no
    exception handling. This is the low-level building block; almost every real caller should
    use run_updates_check_if_not_running() below instead, which adds both.

    on_progress(stage, done, total), if given, is called for every phase of the pipeline — the
    registry check itself (stage="checking"), release notes fetching (stage="release_notes"),
    and AI summarization (stage="summarizing") — each skipped entirely (never announced) if
    there's nothing for that phase to do this round. See persist_check_outcome()'s docstring
    for why a phase that doesn't report progress here looks exactly like a hang."""
    reconcile_progress = (lambda done, total: on_progress("checking", done, total)) if on_progress else None
    outcome = reconcile.run_check(on_progress=reconcile_progress)
    persist_check_outcome(outcome, on_progress=on_progress)
    return outcome


def try_start_updates_check() -> bool:
    """Atomically claims the "a check is running" slot if it's free. Returns True if this
    caller now owns it and must go on to call run_claimed_updates_check(); False if a check
    was already in progress, meaning the caller should do nothing further.

    Split out from run_updates_check_if_not_running() specifically so the UI's Check now
    button can claim the slot synchronously in the request-handling thread — before the HTTP
    response is even built — so that response deterministically shows "running" instead of
    racing a background thread that might not have claimed it yet by the time the response
    renders (see main.py's _launch_check_if_not_running). The actual check work still happens
    on a background thread; only the claim itself needs to be synchronous."""
    if check_state.get_state("updates").get("running"):
        return False
    check_state.set_running("updates")
    return True


def run_claimed_updates_check() -> None:
    """Does the actual check + persist work, assuming try_start_updates_check() already
    claimed the running slot. Includes the Stage 4 safety net: even if the check fails
    partway through, check_state ends up "not running" regardless, so a single bad run (a DB
    error, a bug in a later stage's code, anything) can never wedge the app the way the
    pre-rebuild version did ("ran all night and was still checking"). A failed check just
    reports itself as failed and lets the next trigger — scheduled or manual — try again."""
    ai_provider.reset_rate_limited_count()
    try:
        outcome = run_and_persist_check(
            on_progress=lambda stage, done, total: check_state.set_progress("updates", stage, done, total)
        )
        result = {
            "checked": len(outcome["containers"]),
            "updates_found": sum(1 for c in outcome["containers"] if c["status"] == "update_available"),
            "errors": outcome["errors"],
            "rate_limited": ai_provider.rate_limited_count(),
            "cancelled": check_state.is_cancel_requested("updates"),
        }
    except Exception:
        logger.exception("Update check failed unexpectedly")
        result = {
            "checked": 0, "updates_found": 0, "errors": 1, "rate_limited": ai_provider.rate_limited_count(),
            "cancelled": check_state.is_cancel_requested("updates"),
        }

    check_state.set_finished("updates", result)


def run_updates_check_if_not_running() -> bool:
    """Combines try_start_updates_check() + run_claimed_updates_check() into one synchronous
    call — the right shape for the automatic schedule (Stage 5), which already runs on
    APScheduler's own worker thread and has no HTTP response that needs to return immediately,
    unlike the UI's Check now button. Returns True if a check actually ran, False if one was
    already in progress and this call was skipped."""
    if not try_start_updates_check():
        return False
    run_claimed_updates_check()
    return True


def persist_check_outcome(outcome: dict, on_progress: ProgressFunc | None = None, prune: bool = True,
                           force_stack_analysis: bool = False) -> None:
    """Writes one check's results into container_state/updates. An empty container list is
    always treated as "the check itself didn't complete" (Docker socket unreachable, etc.)
    rather than "there are genuinely zero containers" — existing persisted state is left
    completely untouched rather than risking a wipe from a transient failure.

    Stage 6/7 add a read-fetch-summarize-write shape: first a read-only pass figures out which
    containers are genuinely new (or changed) update_available transitions, then release notes
    are fetched for just those (real GitHub API calls), then an AI summary + severity is
    generated for whichever of those actually got real notes text back (a real AI provider
    call) — all done with no database transaction open, each phase fanned out across its own
    thread pool via _run_concurrent_phase() (ai_provider.concurrency_limit() caps both; lower
    than the registry check concurrency since these are meaningfully more expensive,
    rate-limited calls) — and only then does the actual write batch run. This keeps the
    "one transaction for the whole write batch" property described below while never holding a
    SQLite transaction open across however long a batch of these calls takes.
    on_progress(stage, done, total) is called once per completion during each of the two fetch
    phases (stage="release_notes", stage="summarizing"; done-count updated under a lock, safe
    to call from multiple worker threads at once — same approach as reconcile.run_check()'s
    progress callback), exactly like reconcile.run_check() already does for stage="checking" —
    each skipped entirely (never called with that stage) if there's nothing to do this round,
    so the caller never has to render a meaningless "0/0". A phase that fetches or generates
    things but never reports progress here is exactly the "hangs at N/N" bug Stage 6 initially
    reintroduced.

    Holds a single connection/transaction for the whole write batch rather than letting each
    db.py call open and commit its own — with 59 containers and up to four db.py calls each,
    that was ~200 separate connect+commit+close cycles (each a real fsync in WAL mode)
    happening silently after the progress bar already showed the check as finished, which is
    what a "hangs for several seconds after reaching N/N" report traced back to. One
    transaction also means a check that fails partway through leaves the database exactly as
    it was — readers never see a half-applied check, and there's nothing to reconcile after
    the retry the Stage 4 safety net triggers.

    prune=False skips prune_removed_containers() — required for a scoped single-container
    outcome (see run_and_persist_single_check below), since that outcome's container list
    legitimately contains just the one container being re-checked, not every tracked
    container; pruning against it would wrongly delete every other container's state.

    force_stack_analysis=True threads straight through to stacks.run_stack_analysis_pass() —
    used by the stack page's own Reset & re-check button (see run_and_persist_many_reset_and_
    check and run_claimed_stack_reset_and_recheck below) so an explicit "start over" click
    always gets a fresh cross-service blurb, not just whenever a member's digest happens to
    have moved since the last one."""
    containers = outcome["containers"]
    if not containers:
        return

    read_conn = db.open_conn()
    try:
        existing_by_name = {
            c["container_name"]: db.get_latest_update_for_container(c["container_name"], conn=read_conn)
            for c in containers
        }
        # Read once here, batched onto the same connection as the lookup above, rather than
        # _fetch_release_notes opening its own connection per container to answer "how far
        # back should this one's notes compilation look" -- that was exactly the "one
        # connection per container" regression this whole read phase exists to avoid (see
        # this function's own docstring, and test_persist.py's connection-count test).
        container_state_by_name = {
            c["container_name"]: db.get_container_state(c["container_name"], conn=read_conn)
            for c in containers
        }
        lookback_cap_days = db.get_release_notes_lookback_days(conn=read_conn)
    finally:
        read_conn.close()

    to_fetch = [c for c in containers if _is_new_or_changed_update(c, existing_by_name[c["container_name"]])]
    release_notes_by_name = _fetch_release_notes_deduped(to_fetch, on_progress, container_state_by_name, lookback_cap_days)

    # Only worth summarizing a container that actually has real notes text to work from --
    # either freshly fetched this round, or (see _needs_summary_retry) already on file from an
    # earlier check whose summarization attempt never landed (e.g. a rate-limited/quota-
    # exhausted provider) and hasn't been retried since. Skipped entirely (not just
    # per-container) when no provider is configured, matching every other AI call site's own
    # early-out, so this never logs a stream of "not configured" exceptions once per update.
    to_summarize = []
    if ai_provider.is_configured():
        to_summarize = [
            c for c in to_fetch
            if (release_notes_by_name.get(c["container_name"]) or (None, None))[0]
        ]
        already_summarizing = {c["container_name"] for c in to_summarize}
        to_summarize += [
            c for c in containers
            if c["container_name"] not in already_summarizing
            and _needs_summary_retry(c, existing_by_name[c["container_name"]])
        ]
    summaries_by_name = _run_concurrent_phase(
        "summarizing", to_summarize,
        lambda c: _summarize_container(
            c, _release_notes_for_summary(c, release_notes_by_name, existing_by_name),
        ),
        on_progress,
    )

    conn = db.open_conn()
    to_notify = []
    try:
        if prune:
            db.prune_removed_containers([c["container_name"] for c in containers], conn=conn)
        for container in containers:
            name = container["container_name"]
            notify_args = _persist_one(
                container, existing_by_name[name], release_notes_by_name.get(name),
                summaries_by_name.get(name), conn,
            )
            if notify_args is not None:
                to_notify.append(notify_args)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Fired only after the transaction above has committed, and only ever outside of it --
    # notify_updates_digest() can make a real outbound HTTP call (Apprise), the same reason
    # release notes fetching and AI summarization above are never done while a SQLite
    # transaction is open (see this function's own docstring). One call for the whole batch,
    # not one per container -- see notify_updates_digest()'s own docstring for why.
    items = [n for n in to_notify if n["severity"]]
    errors = [n for n in to_notify if n["error"]]
    if items or errors:
        try:
            notifications.notify_updates_digest(items, errors)
        except Exception:
            logger.exception("Notification digest failed for this check")

    # Also fired only after the transaction has committed -- a real AI call per affected stack.
    # Naturally a no-op unless 2+ members of the same stack are both present in this particular
    # outcome's containers (always true for a full check, true for a stack-scoped re-check,
    # never true for a single-container scoped check) and the Deep Analysis toggle is on -- see
    # stacks.run_stack_analysis_pass()'s own docstring for exactly why no special-casing per
    # caller is needed here.
    try:
        stacks.run_stack_analysis_pass(containers, force=force_stack_analysis)
    except Exception:
        logger.exception("Stack analysis pass failed for this check")


def _run_concurrent_phase(stage: str, containers: list[dict], worker: Callable[[dict], object],
                           on_progress: ProgressFunc | None) -> dict[str, object]:
    """Shared shape for every fan-out phase in the pipeline (release notes fetching, AI
    summarization) — a thread pool capped by ai_provider.concurrency_limit() (per-provider,
    UI-editable in Settings -- see that function's docstring), progress reported once per completion under a lock
    (safe from multiple worker threads at once, same approach reconcile.run_check() uses), and
    the stage never announced at all (on_progress is never called with this stage name) if
    there's nothing to do this round — see persist_check_outcome()'s docstring for why a phase
    that goes quiet without reporting anything looks exactly like a hang."""
    results: dict[str, object] = {}
    if not containers:
        return results

    total = len(containers)
    if on_progress:
        on_progress(stage, 0, total)

    progress_lock = threading.Lock()
    done_count = 0

    def _call_and_report(container: dict):
        nonlocal done_count
        # A container already picked up by a free worker thread still gets its real AI call --
        # only ones still queued behind the concurrency cap skip straight to "not done" once
        # Cancel has been clicked (see check_state.request_cancel/is_cancel_requested), which is
        # what "in-flight calls finish naturally, queued ones don't start" means in practice.
        result = None if check_state.is_cancel_requested("updates") else worker(container)
        if on_progress:
            with progress_lock:
                done_count += 1
                current = done_count
            on_progress(stage, current, total)
        return container["container_name"], result

    max_workers = min(ai_provider.concurrency_limit(), total)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for name, result in pool.map(_call_and_report, containers):
            results[name] = result
    return results


def _is_new_or_changed_update(container: dict, existing) -> bool:
    """True for a container that actually needs release notes fetched: either it's a genuinely
    new/changed update_available transition (nothing recorded before, or what's recorded no
    longer matches), or it's an existing pending update that's still missing release_notes_raw
    entirely -- a prior fetch came up empty, so Check now retries it on every subsequent check
    rather than leaving it permanently stuck with no notes. Unchanged from the last check with
    notes already on file, or not an update at all, both return False."""
    if container["status"] != "update_available":
        return False
    if existing is not None and not existing["release_notes_raw"]:
        return True
    existing_unchanged = (
        existing is not None
        and existing["old_digest"] == container.get("current_digest")
        and existing["new_digest"] == container.get("latest_digest")
        and existing["error"] is None
    )
    return not existing_unchanged


def _needs_summary_retry(container: dict, existing) -> bool:
    """True for a pending update that already has real release_notes_raw on file but never got
    a successful summary/severity out of it -- a prior summarization attempt failed (most
    commonly a rate-limited or quota-exhausted AI provider, see ai_provider.py's Gemini
    handling) and nothing has retried it since, because _is_new_or_changed_update() only
    re-triggers the release-notes fetch phase, not summarization on its own. Digest must be
    unchanged (a genuinely new transition already goes through the normal to_fetch path)."""
    if container["status"] != "update_available" or existing is None:
        return False
    return bool(
        existing["release_notes_raw"]
        and not existing["severity"]
        and existing["old_digest"] == container.get("current_digest")
        and existing["new_digest"] == container.get("latest_digest")
        and existing["error"] is None
    )


def _release_notes_for_summary(container: dict, release_notes_by_name: dict, existing_by_name: dict) -> str | None:
    """Notes text to summarize from: freshly fetched this round if there is one, otherwise
    whatever's already on file (the _needs_summary_retry path -- notes were fine, only the
    summarization attempt needs another try)."""
    name = container["container_name"]
    fetched = release_notes_by_name.get(name)
    if fetched:
        return fetched[0]
    existing = existing_by_name.get(name)
    return existing["release_notes_raw"] if existing else None


def _release_notes_since(container_state, cap_days: int | None) -> datetime | None:
    """How far back to compile missed releases from -- the container's own last-checked time
    (a pre-fetched container_state row, read once for the whole batch before this check's own
    upsert_container_state call overwrites it further down the pipeline in _persist_one -- see
    persist_check_outcome's container_state_by_name -- so this always sees the PREVIOUS check's
    timestamp, never the one in progress), further capped by the Settings lookback limit so a
    container that's gone unchecked for a very long time doesn't pull an unbounded number of
    releases into one AI prompt. None (today's single-latest behavior, no compilation) for a
    container's very first check ever, when there's no prior check to measure a window from.

    cap_days is passed in (db.get_release_notes_lookback_days(), read once for the whole batch
    by the caller) rather than read here per container/group -- reading a Settings value is its
    own SQLite connection, and this is called once per fetch group, so reading it internally
    here would reintroduce exactly the "many small connections" cost the batched read/write
    split elsewhere in this pipeline exists to avoid (see test_persist.py's connection-count
    test)."""
    if container_state is None or not container_state["last_checked_at"]:
        return None
    since = datetime.fromisoformat(container_state["last_checked_at"])
    if cap_days is not None:
        since = max(since, datetime.now(timezone.utc) - timedelta(days=cap_days))
    return since


def _fetch_release_notes(container: dict, container_state_by_name: dict, lookback_cap_days: int | None) -> tuple[str | None, str | None]:
    try:
        since = _release_notes_since(container_state_by_name.get(container["container_name"]), lookback_cap_days)
        return release_notes.get_release_notes(
            container["image_repo"], container["tag"],
            source_override=container.get("source_override"),
            changelog_url_override=container.get("changelog_url_override"),
            since=since,
        )
    except Exception:
        logger.exception("Release notes fetch failed for %s", container["container_name"])
        return None, None


def _release_notes_dedup_key(container: dict) -> tuple:
    return (
        container["image_repo"], container["tag"],
        container.get("source_override"), container.get("changelog_url_override"),
    )


def _fetch_release_notes_deduped(to_fetch: list[dict], on_progress: ProgressFunc | None,
                                  container_state_by_name: dict, lookback_cap_days: int | None) -> dict[str, object]:
    """Stage 11: fetches release notes once per unique (image_repo, tag, source_override,
    changelog_url_override) among to_fetch, not once per container -- containers sharing an
    image (a real fleet, not a hypothetical: two instances of the same *arr app, a spare
    container mirroring a primary one) get the exact same notes text back regardless of which
    one is asking, since the notes describe what changed in the image itself, never anything
    about the individual container. The label overrides are part of the key rather than
    assumed identical across sharing containers -- rare, but two services on the same image
    could genuinely point their servicesentinel.source/changelog_url labels at different places,
    and deduping past that would silently hand one of them the wrong container's notes.

    This is deliberately scoped to fetching only -- AI summarization (see _summarize_container)
    stays one call per container even when it shares this exact same notes text, because that
    step is config-aware by design (Stage 7): two containers on the same image can have
    genuinely different compose configs, and a summary that's correct for one could be
    misleading for the other. Deduplicating a factual fetch is always safe; deduplicating an
    analysis that's meant to vary per-service is not.

    Progress still reports against len(to_fetch) (the number of containers actually waiting on
    notes), not the smaller number of real fetches underneath -- see
    reconcile._fetch_latest_digests()'s docstring for why, and this mirrors it exactly."""
    results: dict[str, object] = {}
    if not to_fetch:
        return results

    total = len(to_fetch)
    if on_progress:
        on_progress("release_notes", 0, total)

    groups: dict[tuple, list[dict]] = {}
    for c in to_fetch:
        groups.setdefault(_release_notes_dedup_key(c), []).append(c)

    progress_lock = threading.Lock()
    done_count = 0

    def _fetch_group(key: tuple) -> None:
        nonlocal done_count
        members = groups[key]
        result = _fetch_release_notes(members[0], container_state_by_name, lookback_cap_days)
        for member in members:
            results[member["container_name"]] = result

        if on_progress:
            with progress_lock:
                done_count += len(members)
                current = done_count
            on_progress("release_notes", current, total)

    max_workers = min(ai_provider.concurrency_limit(), len(groups))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_fetch_group, groups.keys()))

    return results


def _summarize_container(container: dict, release_notes_raw: str) -> tuple[str, str, str | None] | None:
    """Returns (summary_markdown, severity, upgrade_guidance), or None on any failure -- a
    failed summarization is never fatal to the check itself; _persist_one() below just falls
    back to storing no summary, and the detail page already falls back to showing the raw
    release notes whenever summary_markdown is empty (Stage 6), so the operator still sees
    real content either way.

    upgrade_guidance is Deep Analysis for Updates (opt-in, off by default, see
    db.get_deep_analysis_enabled("updates")) -- a second, separate AI call for concrete
    upgrade/migration steps, mirroring Logs/Compose's per-finding suggested fix. Only attempted
    once the main summary already succeeded, and its own failure never invalidates that
    summary -- same "never fatal" treatment, just logged and left as None."""
    try:
        compose_config = compose_lookup.find_service_config(container["container_name"])
        summary_markdown, severity = summarize_update(
            container_name=container["container_name"],
            image_repo=container["image_repo"],
            old_tag_or_digest=container.get("current_digest"),
            new_tag_or_digest=container.get("latest_digest"),
            release_notes=release_notes_raw,
            compose_config=compose_config,
        )
    except Exception:
        logger.exception("AI summarization failed for %s", container["container_name"])
        return None

    upgrade_guidance = None
    if db.get_deep_analysis_enabled("updates"):
        try:
            upgrade_guidance = generate_upgrade_guidance(
                container_name=container["container_name"],
                image_repo=container["image_repo"],
                release_notes=release_notes_raw,
                compose_config=compose_config,
                summary_markdown=summary_markdown,
            ) or None
        except Exception:
            logger.exception("Upgrade guidance generation failed for %s", container["container_name"])

    return summary_markdown, severity, upgrade_guidance


def _persist_one(container: dict, existing, release_notes_result, summary_result, conn) -> dict | None:
    """Returns a candidate item/error dict for notifications.notify_updates_digest() if this
    write is worth notifying about (a genuinely new/changed pending update or check error --
    see the `unchanged` check below), or None if there's nothing new to say (nothing was
    written, or the container resolved back to up_to_date). Never calls into notifications.py
    itself -- see persist_check_outcome(), which collects these across the whole batch and
    fires one digest call only after its write transaction commits."""
    name = container["container_name"]
    image_repo = container["image_repo"]
    tag = container["tag"]
    status = container["status"]
    current_digest = container.get("current_digest")
    latest_digest = container.get("latest_digest")

    db.upsert_container_state(name, image_repo, tag, current_digest, conn=conn)

    if status == "up_to_date":
        if existing is not None:
            db.delete_update(existing["id"], conn=conn)
        return None

    error_text = _REGISTRY_ERROR_TEXT if status == "error" else None
    # Whether this round's container is the exact same pending transition existing already
    # represents -- only then does it make sense to fall back to existing's own
    # notes/summary/severity for whichever piece wasn't (re-)computed this round (an
    # untouched value, or a retried fetch/summarize that came up empty again). A genuinely new
    # or changed transition never carries over stale content from the old one.
    same_transition = (
        existing is not None
        and existing["old_digest"] == current_digest
        and existing["new_digest"] == latest_digest
        and (existing["error"] is not None) == (error_text is not None)
    )

    if release_notes_result:
        release_notes_raw, source_url = release_notes_result
    elif same_transition:
        release_notes_raw, source_url = existing["release_notes_raw"], existing["source_url"]
    else:
        release_notes_raw, source_url = None, None

    if summary_result:
        summary_markdown, severity, upgrade_guidance = summary_result
    elif same_transition:
        summary_markdown, severity, upgrade_guidance = (
            existing["summary_markdown"], existing["severity"], existing["upgrade_guidance"]
        )
    else:
        summary_markdown, severity, upgrade_guidance = None, "", None

    unchanged = (
        same_transition
        and existing["release_notes_raw"] == release_notes_raw
        and existing["summary_markdown"] == summary_markdown
        and existing["severity"] == severity
        and existing["upgrade_guidance"] == upgrade_guidance
    )
    if unchanged:
        return None

    if existing is not None:
        db.delete_update(existing["id"], conn=conn)
    update_id = db.record_update(
        container_name=name, image_repo=image_repo, tag=tag,
        old_digest=current_digest, new_digest=latest_digest,
        summary_markdown=summary_markdown, source_url=source_url,
        error=error_text, severity=severity,
        release_notes_raw=release_notes_raw,
        upgrade_guidance=upgrade_guidance,
        conn=conn,
    )
    return {
        "container_name": name, "image_repo": image_repo, "tag": tag,
        "update_id": update_id, "severity": severity, "error": error_text,
    }


# ---------------------------------------------------------------------------
# Scoped single-container re-check — backs the per-update "Check now" and "Reset & re-check"
# buttons (Stage 6). Both share the exact same "only one check at a time" mutex as the full
# check above (claimed via try_start_updates_check()) so neither can ever run concurrently
# with a full check and race on the same rows, but must NOT overwrite the full check's
# "Last checked: N checked, M found" summary with its own single-container result — see
# check_state.release_running() vs set_finished() for how that's kept separate.
# ---------------------------------------------------------------------------

def run_and_persist_single_check(container_name: str, on_progress: ProgressFunc | None = None) -> dict:
    """Single-container equivalent of run_and_persist_check() — re-checks just container_name
    against the registry and, if it's a genuinely new/changed update, fetches its release
    notes and generates an AI summary fresh, exactly like a full check would for that one
    container. Non-destructive: the row is only touched if the digest actually moved, exactly
    like every other "Check now" in the app (see run_and_persist_single_reset_and_check below
    for the force-fresh variant). prune=False on the persist call below is load-bearing: this
    outcome's container list is deliberately just the one container, and pruning against a
    1-item list would delete every other tracked container's state."""
    reconcile_progress = (lambda done, total: on_progress("checking", done, total)) if on_progress else None
    outcome = reconcile.run_check_one(container_name, on_progress=reconcile_progress)
    persist_check_outcome(outcome, on_progress=on_progress, prune=False)
    return outcome


def run_and_persist_single_reset_and_check(container_name: str, on_progress: ProgressFunc | None = None) -> dict:
    """The per-item "Reset & re-check" button's real behavior: deletes this container's
    existing update row first (if any), then runs the same scoped check as
    run_and_persist_single_check() above. Deleting first is what makes this force a fresh
    release notes fetch and AI summary even when the digest hasn't actually changed since the
    last check (_is_new_or_changed_update() in persist_check_outcome() only fetches for a
    container with no existing row, or one whose recorded digests don't match) — useful for
    retrying a notes fetch or summary that failed without waiting for the image to genuinely
    update again. Mirrors the global Reset & re-check's "wipe history, then check" shape,
    scoped to one container.

    Deliberately does NOT also wipe this container's container_state row the way the global
    button's db.reset_updates_data() wipes that whole table: container_state is just a
    persisted display cache, re-upserted fresh on every check regardless of its prior
    contents, so clearing it first would only risk the container briefly vanishing from the
    Tracked Containers list mid-recheck for no functional benefit."""
    existing = db.get_latest_update_for_container(container_name)
    if existing is not None:
        db.delete_update(existing["id"])
    return run_and_persist_single_check(container_name, on_progress=on_progress)


def run_and_persist_many_check(container_names: list[str], on_progress: ProgressFunc | None = None,
                                force_stack_analysis: bool = False) -> dict:
    """Stack-level counterpart to run_and_persist_single_check() above -- non-destructive: a
    member's row is only touched if its digest actually moved, exactly like every other
    Check now in the app (no wipe, unlike run_and_persist_many_reset_and_check below).
    prune=False and force_stack_analysis are threaded through for the same reasons as that
    function's own docstring explains."""
    reconcile_progress = (lambda done, total: on_progress("checking", done, total)) if on_progress else None
    outcome = reconcile.run_check_many(container_names, on_progress=reconcile_progress)
    persist_check_outcome(outcome, on_progress=on_progress, prune=False, force_stack_analysis=force_stack_analysis)
    return outcome


def run_and_persist_many_reset_and_check(container_names: list[str], on_progress: ProgressFunc | None = None,
                                          force_stack_analysis: bool = False) -> dict:
    """Stack-level counterpart to run_and_persist_single_reset_and_check() -- wipes the
    existing update row (if any) for every named container first, then re-checks all of them
    in one pass via reconcile.run_check_many(). prune=False for the same reason the single-item
    version uses it: this outcome's container list is deliberately just the stack's members,
    not every tracked container. force_stack_analysis is just threaded through to
    persist_check_outcome -- see its own docstring."""
    for name in container_names:
        existing = db.get_latest_update_for_container(name)
        if existing is not None:
            db.delete_update(existing["id"])

    reconcile_progress = (lambda done, total: on_progress("checking", done, total)) if on_progress else None
    outcome = reconcile.run_check_many(container_names, on_progress=reconcile_progress)
    persist_check_outcome(outcome, on_progress=on_progress, prune=False, force_stack_analysis=force_stack_analysis)
    return outcome


def run_claimed_single_check(item_key: str, container_name: str) -> None:
    """Does the actual scoped check + persist work, assuming the caller already claimed the
    shared running slot via try_start_updates_check(). Mirrors run_claimed_updates_check()'s
    Stage 4 safety net (a failed scoped check still releases the mutex, never wedges future
    checks) but reports progress on the item's own channel and releases the shared mutex
    without touching the feature-level last_result — see check_state.py."""
    try:
        run_and_persist_single_check(
            container_name,
            on_progress=lambda stage, done, total: check_state.set_item_progress(item_key, stage, done, total),
        )
    except Exception:
        logger.exception("Scoped re-check failed unexpectedly for %s", container_name)
    finally:
        check_state.finish_item(item_key)
        check_state.release_running("updates")


def run_claimed_single_reset_and_check(item_key: str, container_name: str) -> None:
    """Reset & re-check's counterpart to run_claimed_single_check() above — identical shape
    and safety net, wired to the force-fresh variant instead."""
    try:
        run_and_persist_single_reset_and_check(
            container_name,
            on_progress=lambda stage, done, total: check_state.set_item_progress(item_key, stage, done, total),
        )
    except Exception:
        logger.exception("Scoped reset & re-check failed unexpectedly for %s", container_name)
    finally:
        check_state.finish_item(item_key)
        check_state.release_running("updates")


# ---------------------------------------------------------------------------
# Regenerate AI Response (Stage 7) — re-runs summarization for an update's already-stored
# release_notes_raw in place: no registry check, no fresh notes fetch, just a new attempt at
# turning the same notes into a summary + severity. Shares run_claimed_single_check's exact
# (item_key, container_name) -> None shape, so main.py can launch it through the very same
# _launch_scoped_check helper, and the poll route's "look the container back up by name, land
# on whatever id it currently has" redirect logic works unmodified here too — the id never
# actually changes for this action, it just resolves back to itself.
# ---------------------------------------------------------------------------

def run_and_persist_regenerate_summary(container_name: str) -> bool:
    """Returns True if a summary was actually regenerated, False if there was nothing to
    regenerate from (no release_notes_raw stored, e.g. notes were never found -- the button
    itself is disabled in the UI for that case, this is the server-side backstop) or nothing
    is currently tracked as pending for this container at all (resolved between click and
    background execution). A failed AI call is treated the same as _summarize_container()
    treats one everywhere else: logged, update left exactly as it was."""
    existing = db.get_latest_update_for_container(container_name)
    if existing is None or not existing["release_notes_raw"]:
        return False

    container = {
        "container_name": container_name, "image_repo": existing["image_repo"],
        "current_digest": existing["old_digest"], "latest_digest": existing["new_digest"],
    }
    result = _summarize_container(container, existing["release_notes_raw"])
    if result is None:
        return False

    summary_markdown, severity, upgrade_guidance = result
    db.update_existing_update(
        existing["id"], summary_markdown, severity, existing["error"], existing["source_url"],
        upgrade_guidance=upgrade_guidance,
    )
    return True


def run_claimed_regenerate_summary(item_key: str, container_name: str) -> None:
    """Same claimed-mutex shape as run_claimed_single_check -- just one AI call rather than a
    multi-container fan-out, so progress is reported as a single (0, 1) -> (1, 1) step under
    stage="regenerating" (see main.py's _STAGE_LABELS) purely so the button's spinner text
    says something meaningful rather than falling back to the generic "Checking…"."""
    check_state.set_item_progress(item_key, "regenerating", 0, 1)
    try:
        run_and_persist_regenerate_summary(container_name)
    except Exception:
        logger.exception("Regenerate AI Response failed unexpectedly for %s", container_name)
    finally:
        check_state.set_item_progress(item_key, "regenerating", 1, 1)
        check_state.finish_item(item_key)
        check_state.release_running("updates")


def run_claimed_bulk_regenerate() -> None:
    """The main Updates page's "Regenerate AI Response" button -- affects every currently
    pending update at once rather than just one, reusing the same fan-out helper the real
    check's own release-notes/summarization phases use (_run_concurrent_phase, capped by
    ai_provider.concurrency_limit()) so this doesn't hammer the AI provider any harder than a
    normal check already does. Claims the Updates mutex (see try_start_updates_check) so it
    can't overlap with a real check or another regenerate run, and reports live progress
    through the exact same check_state "updates" channel the status badge already polls, under
    stage="regenerating" (see main.py's _STAGE_LABELS) so it reads as "Regenerating AI Response
    (N/total)…" while it's running.

    release_running (not set_finished) on completion: this isn't itself a check, so it must
    not overwrite the status badge's "Last checked: ..." summary with a result shape that was
    never meant to describe a regenerate pass."""
    try:
        rows = db.list_tracked_containers_with_status()
        candidates = [r for r in rows if r["status"] == "update_available" and r.get("release_notes_raw")]

        def _on_progress(stage: str, done: int, total: int) -> None:
            check_state.set_progress("updates", stage, done, total)

        _run_concurrent_phase(
            "regenerating", candidates,
            lambda c: run_and_persist_regenerate_summary(c["container_name"]),
            _on_progress,
        )
    except Exception:
        logger.exception("Bulk Regenerate AI Response failed unexpectedly")
    finally:
        check_state.release_running("updates")


# ---------------------------------------------------------------------------
# Stack-level Retry / Reset & re-check (Stage 12 follow-up) -- originally plain synchronous
# form posts with no spinner and no client-side "already running" guard, which made hitting
# the shared mutex while a background check was in flight look like a dead button: the click
# went through, the route silently no-op'd, and nothing on screen ever said why. Routed through
# the exact same claimed-mutex + background-thread + spinner/poll machinery as the per-item
# actions above, both so the existing base.html "disable while running" JS now actually covers
# these buttons (it only fires on htmx requests) and so a busy mutex renders a real message
# instead of a silent redirect.
# ---------------------------------------------------------------------------

def run_claimed_stack_check_now(item_key: str, stack_id: str) -> None:
    """Stack-scoped equivalent of the per-item Check now: re-checks every current member of
    this stack via run_and_persist_many_check, only touching a member's row if its digest
    actually moved -- no wipe, no forced AI regeneration, exactly like every other Check now
    button in the app, hence no confirmation dialog on the button that reaches this.

    Member names are re-resolved fresh right before the check (not whatever the page had
    loaded) so a member added to or removed from the compose file between page load and click
    is reflected too -- same reasoning as run_claimed_stack_reset_and_recheck below."""
    try:
        member_names = stacks.stack_member_names(stack_id)
        run_and_persist_many_check(
            member_names,
            on_progress=lambda stage, done, total: check_state.set_item_progress(item_key, stage, done, total),
        )
    except Exception:
        logger.exception("Scoped stack check failed unexpectedly for %s", stack_id)
    finally:
        check_state.finish_item(item_key)
        check_state.release_running("updates")


def run_claimed_stack_retry(item_key: str, stack_id: str) -> None:
    """Force-regenerates this stack's cross-service analysis blurb, bypassing the content-hash
    cache regardless of whether anything's actually changed since the last one -- same
    "an explicit click always regenerates" semantics as the per-update Regenerate AI Response
    button above, just against a stack's members instead of one update."""
    check_state.set_item_progress(item_key, "stack_analysis", 0, 1)
    try:
        members = stacks.members_for_analysis(stack_id)
        stacks.regenerate_stack_analysis(stack_id, members, force=True)
    except Exception:
        logger.exception("Stack Retry failed unexpectedly for %s", stack_id)
    finally:
        check_state.set_item_progress(item_key, "stack_analysis", 1, 1)
        check_state.finish_item(item_key)
        check_state.release_running("updates")


def run_claimed_stack_reset_and_recheck(item_key: str, stack_id: str) -> None:
    """Wipes and re-checks every current member of this stack, force-regenerating the stack's
    cross-service blurb as part of the very same persisted outcome (force_stack_analysis=True,
    still gated behind the Deep Analysis toggle -- see stacks.run_stack_analysis_pass's
    docstring) rather than as a separate follow-up AI call. Without that forced regeneration an
    explicit "start over" click could leave the exact same blurb on screen even though every
    member underneath it was just re-checked from scratch (e.g. every member's registry digest
    happens to come back unchanged this round even though their release-note text or AI summary
    was just refreshed) -- which reads as broken to someone who clicked expecting a fresh take,
    not as "correctly nothing changed." A separate explicit force=True call after the fact would
    just double the AI cost for the exact same fingerprint, since nothing changes between the
    automatic pass and a follow-up call microseconds later.

    Member names are re-resolved fresh right before the recheck (not whatever the page had
    loaded) so a member added to or removed from the compose file between page load and click
    is reflected too."""
    try:
        member_names = stacks.stack_member_names(stack_id)
        run_and_persist_many_reset_and_check(
            member_names,
            on_progress=lambda stage, done, total: check_state.set_item_progress(item_key, stage, done, total),
            force_stack_analysis=True,
        )
    except Exception:
        logger.exception("Stack Reset & re-check failed unexpectedly for %s", stack_id)
    finally:
        check_state.finish_item(item_key)
        check_state.release_running("updates")
