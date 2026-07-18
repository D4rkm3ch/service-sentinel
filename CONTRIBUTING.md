# Contributing / Architecture Overview

A short orientation for anyone (future you included) opening this codebase cold. This is a
FastAPI + Jinja2 + htmx app with SQLite storage -- no frontend build step, no ORM, no message
queue. Server-rendered pages, htmx for partial swaps and polling, and a handful of inline
scripts in `base.html`.

## The one convention that's easiest to miss

**Runtime configuration lives in the database, not env vars.** Schedules, notification
settings, AI provider keys and models, severity thresholds, feature on/off toggles, the
lookback window, the auth password -- all of it is in the `app_settings` table, written by the
Settings page and read at call time, so everything is changeable from a running instance
without a redeploy. Env vars (`app/config.py`, read once at import) only cover things that
genuinely can't change at runtime: paths, the Docker socket, the port-level stuff in
`.env.example`. If you add a new knob, default to the database unless it's needed before the
database is open. Breaking this convention (an env var for something the UI should own, or
vice versa) is the most common way to re-introduce papercuts this project already fixed once.

## The shared check-pipeline shape

All three features are the same pipeline with different sources, and changes to one usually
have a sibling in the other two:

| Stage | Updates | Runtime health (logs) | Configuration health (compose) |
|---|---|---|---|
| Enumerate | `docker_client.list_tracked_containers` | `docker_client.list_running_containers_for_logs` | `compose_lookup.list_compose_files` |
| Cheap local filter | registry digest compare (`registry.py`) | suspicious-line regex (`log_filter.py`) | file-hash compare (`db.py`) |
| AI call (only if the filter fired) | release-notes summary (`release_notes.py`, `summarizer.py`) | log triage (`log_watcher.py`) | compose review (`compose_reviewer.py`) |
| Persist + dedupe | `persist.py` | findings tables in `db.py` | findings tables in `db.py` |
| Notify | `notifications.py`, batched per severity | same | same |

The cheap-local-filter stage is load-bearing: it's what makes a clean container/file cost zero
tokens. Don't move work from before it to after it.

Check orchestration lives in `scheduler.py` (APScheduler, schedules parsed by
`schedule_spec.py`) and `check_state.py` (the running/finished state machine the topbar polls
via `GET /checks/status`). Checks run in background threads; routes only ever trigger and
report.

## Module map

- `main.py` -- every route, the middleware stack (security headers, no-store, auth gate, rate
  limit -- all plain ASGI or noted otherwise), markdown rendering/sanitization, and the
  Overview-page card assembly. Large by design; split only if a real seam appears.
- `db.py` -- schema, migrations (inline in `init_db`), and every query. Parameterized SQL
  throughout; dynamic `ORDER BY` only from allowlisted column names.
- `ai_provider.py` -- the provider abstraction (Anthropic, Gemini, OpenAI, OpenAI-compatible); retry and concurrency logic lives here,
  not in callers. `ai_json.py` parses imperfect LLM output.
- `compose_lookup.py` -- compose-file indexing (cached + request-coalesced, see its comments)
  and secret redaction (key-name + value-shape).
- `stacks.py` / `reconcile.py` -- grouping containers by compose file, and reconciling DB state
  against what's actually running.
- `secrets_crypto.py` -- optional at-rest encryption for stored secrets.

## Tests

`python -m pytest` (or with `--cov=app` for the coverage gate CI enforces, floor in
`pyproject.toml`). Conventions the suite relies on:

- **One shared session-scoped `client` fixture** (`tests/conftest.py`). Never open a second
  `TestClient` against the app -- the startup event starts APScheduler, and starting it twice in
  one process raises.
- Tests are behavior-named, usually with a docstring explaining the bug or feature that
  motivated them -- keep doing that; it's the project's changelog-of-record.
- Anything that mutates shared state (settings keys, the auth secret, rate-limit buckets) must
  reset it in setup/teardown -- the fixtures are session-scoped, so leaks poison other files.
- Lint is `ruff check app/ tests/` (config in `pyproject.toml`), enforced in CI before tests.

## Style notes

- No emojis anywhere in the product. Severity is text labels + accent colors.
- User-facing copy uses plain hyphens, not em dashes.
- Comments explain *why* (the constraint or the bug that motivated the code), not *what*.
