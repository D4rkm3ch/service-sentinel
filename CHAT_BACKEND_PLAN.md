# Chat AI backend — implementation plan

Planning-only doc, written before any backend code exists. The front-end shell (floating
launcher + slide-out panel, `#chat-launcher`/`#chat-panel` in `base.html`, styles in
`style.css`) already shipped on `polish` as of v0.8.7 and currently just echoes whatever's
typed with an honest "not connected yet" note. This doc is the plan for replacing that echo
with a real AI backend. Delete this file once the feature has shipped and stabilized — it's a
working doc, not permanent documentation (same spirit as the old, never-committed
`security_hardening_plan.md` this repo's comments still reference).

## Hard constraints (non-negotiable, stated explicitly by the user)

1. **Read-only. No exceptions.** *"the chat box is just someone to talk to. We aren't going to
   give it any permissions to edit/change anything."* The AI must never be able to call any
   `db.py` function that mutates state (`set_*`, `record_*`, `upsert_*`, `mark_*`, `delete_*`,
   `silence_*`, `resolve_*`, etc.), trigger a check, or touch Docker/compose files in any way.
2. Floating widget on every page, not a sidebar destination (already built).
3. Reuses whatever AI provider/model/key is already configured in Settings — no separate
   provider config for chat.

## Architecture decision: context-snapshot chat, not tool-calling

Two ways to give the model live system data:

- **(A) Tool-calling loop** — model requests `list_pending_updates()`, `get_finding(id)`, etc.
  mid-conversation; we execute the read-only function and feed the result back. More
  "agentic," scales better to large systems, but: `ai_provider.py` has zero tool-calling
  plumbing today (only `complete_text()`/`web_search()`, single-turn, no `tools=` param
  anywhere); every one of the 4 providers (Anthropic/Gemini/OpenAI/OpenAI-compatible) has a
  *different* tool-call wire format, so this is real per-provider work; and OpenAI-compatible
  covers arbitrary local models (Ollama, llama.cpp, ...) where tool-calling support is
  inconsistent-to-absent — a core supported provider could just not work.
- **(B) Context-snapshot** — before calling the model, build a plain-text digest of current
  read-only state (pending updates, open findings, health streaks, schedules) and hand it to
  the model as system-prompt context on every turn. No tool schemas, no per-provider
  differences beyond message-array shape (which `complete_text` already abstracts today), works
  identically on every provider including small local models with no function-calling support
  at all.

**Recommendation: (B).** Beyond being less work, it's *categorically* safer than (A) for this
specific feature: there is no callable surface at all, so "the model can't do anything but
read" is true by construction (nothing to invoke), not just true because we only registered
read-only tools and have to trust that stays true forever as the tool registry grows. Given
constraint #1 is the one the user has repeated most emphatically across this whole feature,
that property is worth more here than tool-calling's extra flexibility.

Note this as a **decision to confirm with the user before writing code**, not something to
silently lock in — it's a real fork, and (A) remains a plausible v2 if snapshots turn out too
coarse in practice (e.g. someone wants to ask about one container's full log tail, which a
fixed-size snapshot can't include for every container up front).

## New/changed files

### `app/chat.py` (new)
Mirrors how `summarizer.py`/`release_notes.py` hold feature logic while `main.py` stays thin
routing glue.

- `SYSTEM_PROMPT_HEADER` — static instructions (see draft below).
- `build_context_snapshot() -> str` — plain-text/markdown digest, see spec below. Pure
  read-only `db.py`/`check_state` calls only.
- `MAX_HISTORY_MESSAGES` / `MAX_MESSAGE_CHARS` constants — bound what gets sent to the model.
- `answer(history: list[dict]) -> str` — validates/trims `history`, builds the system prompt
  (header + fresh snapshot), calls `ai_provider.complete_chat(...)`, returns the raw markdown
  reply. Raises on provider failure (same contract as `complete_text`) — `main.py`'s route
  catches and turns that into the JSON error shape.
- **Guardrail test to write alongside this file**: a static/structural test (grep this
  module's own source) asserting it never references any `db.set_`/`db.record_`/`db.upsert_`/
  `db.mark_`/`db.delete_`/`db.silence_`/`db.resolve_`/`db.unsilence_` call — a deterministic
  guard in the same spirit as the existing docker.sock `:ro` misread guard, enforcing
  constraint #1 at the source level, not just by code review.

### `app/ai_provider.py` (extend, don't replace)
Add one new public function alongside `complete_text`/`web_search`, following the exact same
dispatch-table + `_with_truncation_retry` pattern already used for those two:

```python
def complete_chat(system: str, messages: list[dict], max_tokens: int) -> str:
    """messages: [{"role": "user"|"assistant", "content": "..."}, ...]. Multi-turn version of
    complete_text -- same provider dispatch, same truncation-retry wrapper."""
```

Per-provider private impls (`_chat_anthropic`, `_chat_gemini`, `_chat_openai`,
`_chat_openai_compat`), reusing the existing `_call_gemini`/`_call_openai` retry wrappers and
client constructors (`_gemini_client`, `_openai_client`, `_openai_compat_client`) — no new
retry/backoff logic needed, just a message-array instead of a single `user_message`.

- Anthropic: `messages=[...]` (role names already match: `user`/`assistant`) + top-level
  `system=`.
- Gemini: needs a role remap (`assistant` → `model`) into `contents=[{"role":..., "parts":[...]}]`.
- OpenAI/OpenAI-compat: prepend `{"role": "system", ...}` to the messages array, roles already
  match.

No new SDK dependencies — `anthropic`/`openai`/`google-genai` are already imported and already
support multi-turn; only single-turn call sites exist in this codebase today.

### `app/main.py` (extend)
- `@app.post("/chat/send")` — parse `{"history": [{"role", "content"}, ...]}`, call
  `chat.answer(history)`, run the reply through the **existing** `render_markdown()` (same
  sanitizer already used for release notes / AI findings / summaries — no new sanitization
  logic needed), return `{"ok": true, "markdown": "<raw reply>", "html": "<sanitized html>"}`
  on success or `{"ok": false, "error": "<message>"}` on failure (provider not configured →
  friendly "set one up in Settings" message, same tone as other features' `is_configured()`
  early-outs; provider call raised → generic "couldn't reach the AI provider" message, no raw
  exception text leaked to the client).
- Add `"/chat/send"` to `_RATE_LIMITED_EXACT_PATHS` (reuse the existing 30-req/60s window to
  start; it's the same "don't let an unauthenticated visitor burn AI spend" reasoning that
  motivated rate-limiting the check-triggering routes in the first place — revisit the number
  only if it proves too tight for a real back-and-forth conversation).

### `app/templates/base.html` (rewire, don't restructure)
Markup stays as-is. Replace the JS `submit()` function's fake-echo body:

- Track a client-side `history` array (`{role, content}`, `content` = **plain markdown text**,
  not the rendered HTML — see below) alongside the DOM. Reset it in the existing `clearBtn`
  handler (currently only clears the DOM).
- `submit()`: push the user's message onto `history` and the DOM (unchanged — `textContent`,
  already safe), show a loading indicator (reuse the existing `.spinner` class already used
  elsewhere in the app) in place of the empty state / as its own transient bubble, `fetch()`
  `/chat/send` with `{history}`, on success append an assistant bubble via `innerHTML =
  data.html` (already sanitized server-side — safe) **and** push `{role: "assistant", content:
  data.markdown}` onto `history` (the *un-rendered* markdown, so re-sending history to the
  model next turn doesn't feed it back its own HTML tags), on failure append an error bubble
  (reuse `.chat-msg-system` styling, or give errors their own red-tinted variant — minor call,
  make it when building).
- Cap `history` sent to the server client-side too (trim oldest once past
  `chat.MAX_HISTORY_MESSAGES`) so a very long-lived open panel doesn't keep growing the
  request payload forever — belt-and-suspenders with the server-side cap in `chat.answer()`.

### Settings — no changes for v1
Chat reuses whatever's already configured in the existing "AI Provider" section. If nothing's
configured, `/chat/send` returns the friendly "set one up in Settings" error and the widget
displays it like any other error bubble. No new provider/key/model UI, no new enable/disable
toggle — simplest possible v1 surface. (A dedicated "Enable AI Chat" toggle, mirroring the
per-feature `updates`/`logs`/`compose` enabled pattern, is an easy add later if wanted — not
needed to ship v1.)

## System prompt draft

```
You are the AI assistant built into Service Sentinel, a homelab Docker container monitoring
tool. You can see the operator's current system state below and answer questions about it.

You are strictly read-only: you have no ability to change, fix, restart, silence, resolve, or
configure anything in this system, and you must never imply otherwise. If asked to do
something like that, say plainly that you can't, and point to where in the UI (Updates /
Runtime / Configuration / Settings) they'd do it themselves.

Be concise and specific -- reference actual container/service names and real data from the
snapshot below rather than generic advice, when the snapshot has relevant information. If
something isn't in the snapshot, say you don't have that information rather than guessing.

<snapshot>
...
</snapshot>
```

## Context snapshot spec

One function, `chat.build_context_snapshot()`, built fresh on every request (state changes
between messages, no caching) from existing read-only calls:

| Section | Source | Notes |
|---|---|---|
| Updates | `db.list_recent_updates()` (or `list_tracked_containers_with_status()`), `check_state.get_state("updates")` / `db.get_effective_schedule("updates")` | Pending count + up to ~10 items (container, current→new version, severity, one-line summary); "+N more" beyond the cap |
| Runtime Health | `db.list_findings("logs")` / `db.findings_health_summary("logs")`, `db.get_feature_health_streak("logs")` | Same cap/shape, grouped by subject like the Overview page already does |
| Configuration Health | same as above, `source="compose"` | same |
| Schedules / notifications | `db.get_effective_schedule(feature)`, `db.get_notifications_enabled()`, `db.get_feature_notify_enabled(feature)` | one line per feature |

Cap list items per section (~10–15) to bound token cost on systems with a lot of open findings
(the real dev screenshot already showed "20 Issues" on Runtime Health) — total counts always
included even when the itemized list is truncated, so the model doesn't undercount. Exact caps
are a tuning detail, not a design blocker.

**Secrets must never enter the snapshot** — no API keys, webhook tokens, passwords, or the
auth secret, even though `db.py` has getters for all of them. `build_context_snapshot()`
should only ever call the read functions listed above, never anything under the "AI
Provider"/"Access Control" settings groups.

## HTTP contract

```
POST /chat/send
{ "history": [ {"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ... ] }

200 { "ok": true, "markdown": "...", "html": "<p>...</p>" }
200 { "ok": false, "error": "No AI provider is configured yet -- set one up in Settings." }
429 (existing RateLimitMiddleware, same shape as every other rate-limited route)
```

## Explicitly out of scope for v1

- **Streaming responses.** Every provider supports it, but it'd mean either streaming raw
  unsanitized markdown to the client (conflicts with the server-side-sanitize-then-send
  decision above) or building a client-side markdown+sanitize pipeline (new JS dependency,
  more surface). Single blocked request + spinner for v1; streaming is a real but separable
  follow-up.
- **Server-side conversation persistence.** History lives in the DOM/JS only, same as today —
  no new DB table, no migration. Already resets on page navigation, which nobody's asked to
  change.
- **Tool-calling** (see architecture decision above) — noted as a considered-and-deferred v2
  path, not a v1 gap to feel bad about.
- **A dedicated chat enable/disable Settings toggle** — falls out naturally from
  `is_configured()` for v1; add later if wanted.

## Testing checklist for next session

- `test_chat_route_errors_cleanly_when_ai_not_configured`
- `test_chat_route_calls_ai_provider_and_returns_sanitized_html` (mock `ai_provider.complete_chat`)
- `test_chat_route_never_leaks_raw_exception_text_on_failure`
- `test_chat_snapshot_includes_pending_updates_and_open_findings` (seed DB, assert content)
- `test_chat_snapshot_never_includes_secrets` (seed API keys/tokens, assert none of their
  values appear in the built snapshot string)
- `test_chat_module_never_calls_a_mutating_db_function` (the static guardrail described above)
- `test_chat_send_is_rate_limited` (extend `test_rate_limiting.py`'s existing pattern)
- `test_complete_chat_dispatches_to_the_configured_provider` (one per provider, same shape as
  existing `ai_provider` tests for `complete_text`)
- Full suite + ruff + coverage floor, same as every other push this session.

## Suggested build order (next session)

1. Confirm the (B) context-snapshot architecture decision with the user before writing code
   (it's the one real open fork above).
2. `ai_provider.complete_chat()` + per-provider impls + provider-specific unit tests (no chat.py
   dependency yet, testable in isolation).
3. `chat.py`: snapshot builder + `answer()` + the guardrail test.
4. `main.py`: `/chat/send` route + rate-limit registration + route-level tests.
5. Wire `base.html`'s `submit()`/`clearBtn` to the real endpoint; manual Playwright pass
   (configured provider happy path, unconfigured-provider error path, a multi-turn exchange,
   Clear resets both DOM and history array).
6. Full suite + lint + push to `polish` as usual; only bump the version / push to `main` once
   the user says it's ready to be a real release (this was the feature originally floated as
   "bumping to 0.9" -- worth asking at that point whether it lands as 0.9 specifically or just
   the next frozen dev-cycle number, rather than assuming).

---

# v2 — Self-tuning steering rules (user-editable AI guidance)

A follow-on the user explicitly asked for: let the operator refine what the AI does and
doesn't flag, on an ongoing basis, without a code change every time. Not model training /
fine-tuning (impossible with hosted APIs, wrong tool anyway) — instead a persistent,
user-editable store of natural-language **steering rules** that get injected into the AI
prompts on every check. This is the generalization of the by-hand prompt-tuning that made up a
large fraction of this whole session (PUID/PGID, media-manager RW mounts, the five compose
false-positive fixes, etc.).

## The refined constraint boundary (corrected by the user — supersedes v1's framing)

The v1 "read-only" wording above was about the user's **actual system** and stays absolute.
The full three-tier boundary, as the user has now spelled it out:

- **Never, ever:** the user's real system — Docker containers, compose files, host files. The
  hard wall. (This is what "read-only" was always about.)
- **Never by the AI, only by us (developers) in code:** the app's own **operational actions** —
  silencing/resolving findings, marking updates read, changing schedules, flipping feature
  toggles. These change what's monitored/reported and stay dev-controlled, not something the
  chat or any AI path can do. Explicitly ruled out by the user: *"It will never be able to edit
  its operational actions. That's what you and I will always be in control of."*
- **Allowed — the AI/app editing itself:** its own **steering / analysis guidance**. The app
  may write new steering rules and prompt guidance to its own store, at the user's request, so
  it refines *how it analyzes* (not *what it operates*). This is the whole point of v2.

So the write surface v2 opens is exactly one table: steering rules. Nothing else.

## What can and can't become an editable rule (important honest limitation)

Auditing the steering that exists in the codebase today (`summarizer.py`) turns up **two
distinct kinds**, and only one can become a user-editable text rule:

1. **Prompt-text steering — becomes editable default rules.** The
   `COMPOSE_REVIEW_SYSTEM_PROMPT_BASE` already contains an explicit, structured block literally
   introduced as *"the most common harmless patterns this operator has already told you about
   by name"* (summarizer.py ~L470), followed by ~11 bullets. These are already exactly "user
   ignore rules," just hardcoded into a string. Each becomes a seeded default rule (see seed
   list below). The log-triage and update-summary prompts have smaller amounts of the same kind
   of language.

2. **Deterministic code guards — stay as code, NOT user-editable.** Several of this session's
   fixes are Python functions that post-filter the AI's output because prompt compliance alone
   proved unreliable on the highest-severity checks: `_docker_socket_mounts_are_all_read_only()`
   (drops hallucinated/misread docker.sock findings), `_services_with_a_real_restart_policy()`
   (drops false "missing restart policy" findings), the self-negating-no-op prefilter. These
   aren't language and can't be exposed as "edit this sentence" without ceasing to be what they
   are. They live in the same dev-controlled tier as operational actions. The UI should NOT
   pretend these are editable rules.

3. **Prompt machinery — stays hardcoded, never user-touchable.** The JSON-only output
   instruction, the field schema, the `:ro`/`:rw` re-read discipline, the tracked-findings
   matching logic. A stray user edit here breaks response parsing. When extracting rules from a
   prompt, split it into: fixed machinery (stays in code) + steering rules (moves to the store,
   seeded with current defaults). Only the second half is ever user-facing.

## New/changed files (v2)

### `app/db.py` (extend) — new `steering_rules` table
Columns (sketch): `id`, `scope` (which AI site: `"compose"` / `"logs"` / `"updates"` /
`"chat"` / `"all"`), `text` (the natural-language rule), `enabled` (bool), `source`
(`"default"` seeded vs `"user"` created), `created_at`. Standard CRUD helpers mirroring the
existing `get_*/set_*/list_*` conventions: `list_steering_rules(scope=None)`,
`add_steering_rule(scope, text)`, `update_steering_rule(id, text, enabled)`,
`delete_steering_rule(id)`. **Seeded on first boot** (in `init_db()`, guarded so it only seeds
once — a `steering_seeded` flag in `app_settings`, same pattern as other one-time init) from
the extracted defaults so a brand-new user immediately sees all the steering we built as
editable starting points, exactly as the user described.

### `app/steering.py` (new) — rule rendering + the seed defaults
- `DEFAULT_RULES` — the seed list (below), each `(scope, text)`. Single source of truth for
  what `init_db()` seeds.
- `render_rules_for(scope) -> str` — pulls active rules for a scope (its own + the `"all"`
  scope) and formats them into the bullet block that gets interpolated into that prompt. The
  prompts change from a hardcoded bullet list to a `{steering_rules}` placeholder this fills.
- Deliberately data + one formatter, no per-rule logic — same "dispatch/format, not a
  framework" spirit as `ai_provider.py`.

### `summarizer.py` (refactor the three prompt sites)
Replace the hardcoded "harmless patterns" bullet block in `COMPOSE_REVIEW_SYSTEM_PROMPT_BASE`
(and the smaller equivalents in the log-triage / update-summary prompts) with a
`{steering_rules}` placeholder filled by `steering.render_rules_for(scope)` at call time. The
fixed machinery and code guards around them are untouched. This is the one genuinely
delicate refactor in v2 — it's editing prompts that took this whole session to tune, so it
needs a careful before/after diff and a re-run of the existing compose-false-positive
regression tests (`test_compose_review_*`) to prove the seeded defaults reproduce today's
behavior exactly.

### New Settings page or section — rules CRUD
User's ask: *"a settings area or own page that lists all the rules/steering prompts… new,
edit, delete."* Leaning **its own page** (reachable from Settings and/or the sidebar) rather
than a cramped Settings section, since the seed list alone is ~a dozen multi-line rules and it
'll grow — open question to confirm. Shows all rules grouped by scope, each with
enable/disable toggle + edit + delete, plus an "add rule" affordance. Defaults and
user-created rules look the same and are equally editable/deletable (the user was explicit that
the defaults are just a starting point to tweak, not locked). A deleted default does not come
back on next boot (the one-time seed flag ensures seeding never re-runs).

### `chat.py` + `/chat/send` (extend, once v1 chat exists)
The chat becomes one way to author rules: on an explicit user request ("stop flagging
qbittorrent's rw mount"), it calls `db.add_steering_rule(...)`. This is the one write the chat
is allowed (per the refined boundary above). Surface every rule the chat adds visibly in the
new rules page so they never accumulate silently. Whether the chat writes directly vs. drafts-
then-user-confirms is a UX call to make at build time (I lean: chat proposes, shows the exact
rule text, user confirms with a click — keeps authorship explicit — but direct-write is
defensible too since it's the user's own explicit instruction).

## Seed default rules (extracted from today's `COMPOSE_REVIEW_SYSTEM_PROMPT_BASE`)

All `scope="compose"` unless noted. These are the ~11 "harmless patterns" bullets plus the
top-level judgment rule, lifted verbatim-ish into standalone rules:

1. (top-level, keep prominent) Only report a finding with a real, concrete negative consequence
   you can name; if a choice is harmless, don't report it even if another arrangement is cleaner.
2. Don't flag missing resource limits (CPU/memory).
3. Don't flag image tag / version-pinning choices in either direction; assume deliberate.
4. Don't flag `network_mode: host` — assume deliberate and required.
5. Don't flag missing healthchecks in any form.
6. Don't flag `${VAR}` / `${VAR:-default}` references to names not defined in the file.
7. Don't flag `[REDACTED]` values as missing/blank — redaction only ever replaces a present value.
8. Don't flag an empty `networks: {}` block (Dockge etc. insert it).
9. Don't flag PUID/PGID/GUID/UID/TZ env vars as redundant/unnecessary.
10. Never recommend adding an explicit `:rw` to a mount that already defaults to read-write.
11. Don't recommend read-only for a service's own config/cache/database/download dir.
12. Don't recommend read-only for a library mount used by a media manager / download client /
    ROM manager (managing files, not just reading) — with the current image-name examples.

(Exact final wording is a build-time detail; the point is these twelve are the concrete seed.)
Log-triage and update prompts contribute a few more once their prompt text is audited the same
way at build time.

## Open questions to confirm before building v2

- Own page vs Settings section for the rules UI (leaning own page).
- Per-scope rules (a rule tagged compose/logs/updates/chat) vs a single global list applied
  everywhere. Leaning per-scope — a "don't flag missing healthchecks" rule is meaningless for
  the log analyzer — but a simple global list is less UI. **User decision.**
- Chat authoring: direct-write vs propose-then-confirm (leaning propose-then-confirm).
- Editing a seeded default: edit-in-place vs. the seed stays and the user's edit is an override.
  Leaning edit-in-place (simpler, and the seed flag means it never gets clobbered on reboot).

## Sequencing

v2 depends on v1 only for the chat-authoring path — the **rules store + seeding + prompt
injection + CRUD page are independent of the chat entirely** and could even ship first or in
parallel. Recommended order regardless: land v1 read-only chat, get it stable, then build v2 as
its own arc (store + seed + refactor prompts + CRUD page first; wire chat-authoring last). The
prompt refactor is the riskiest single step in either version — it touches the hardest-won
tuning in the app — so it gets its own careful commit with the existing regression tests as the
safety net.
