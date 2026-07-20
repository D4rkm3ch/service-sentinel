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
