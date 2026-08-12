"""The mutating half of the chat's action-proposal feature -- see app/chat.py's own docstring
for the full picture. Deliberately a SEPARATE module from chat.py rather than one more function
in it: chat.py's read-only guardrail (test_chat.py's source-level scan for any mutating db.*
call) is what makes "the model's own text generation can never touch app state" true by
construction rather than by review discipline, and that property is worth keeping legible and
mechanically enforced. Splitting the file is what lets both guarantees hold at once: chat.py
stays exactly as read-only as it always was, and this module is where the two narrow, explicitly
operator-confirmed exceptions live instead -- visibly separate, and its own thing to review.

execute() is only ever reached from main.py's POST /chat/confirm-action, which only ever fires
on an operator's own click of a Confirm button rendered under a specific proposal the model
produced earlier in the SAME conversation -- never automatically, and never from the model's
reply alone. The action payload arriving here is untrusted input like any other POST body (it
round-tripped through the browser, not passed in-process from chat.py), so every field is
re-validated from scratch rather than trusting that chat.py's own extraction already checked it."""

import logging

from app import db

logger = logging.getLogger("service_sentinel.chat_actions")

VALID_SOURCES = ("logs", "compose")
VALID_RULE_TYPES = ("exclude", "watch")


def _validate_silence_findings(action: dict) -> str | None:
    """Returns an error message, or None if the action is well-formed enough to attempt."""
    if action.get("source") not in VALID_SOURCES:
        return "Unknown source."
    subjects = action.get("subjects")
    if not isinstance(subjects, list) or not subjects or not all(isinstance(s, str) and s.strip() for s in subjects):
        return "No subjects given."
    title_contains = action.get("title_contains")
    if not isinstance(title_contains, str) or not title_contains.strip():
        return "No title pattern given."
    return None


def _validate_add_custom_rule(action: dict) -> str | None:
    if action.get("source") not in VALID_SOURCES:
        return "Unknown source."
    if action.get("rule_type") not in VALID_RULE_TYPES:
        return "Unknown rule type."
    name = action.get("name")
    if not isinstance(name, str) or not name.strip():
        return "No name given."
    instruction = action.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        return "No instruction given."
    return None


def _execute_silence_findings(action: dict) -> dict:
    """Silences every currently ACTIVE finding for the given subjects whose title contains the
    given text (case-insensitive substring, same matching an operator doing this by hand would
    reason about) -- never touches a finding that's already silenced or one that doesn't match.
    A finding silenced this way stays silenced even if it recurs (see db.upsert_finding's own
    docstring), so this already covers "and don't bring this specific one back" for free; it's
    add_custom_rule's job to cover a pattern recurring under a NEW subject that was never
    matched here in the first place."""
    source = action["source"]
    needle = action["title_contains"].strip().lower()
    silenced_count = 0
    matched_subjects: set[str] = set()
    for subject in action["subjects"]:
        for finding in db.list_findings_for_subject(source, subject, include_silenced=False):
            if needle in finding["title"].lower():
                db.set_finding_status(finding["id"], "silenced")
                silenced_count += 1
                matched_subjects.add(subject)
    if silenced_count == 0:
        return {
            "ok": False,
            "message": "No active findings matched that description for those subjects -- "
                       "nothing was changed.",
        }
    plural = "s" if silenced_count != 1 else ""
    subject_plural = "s" if len(matched_subjects) != 1 else ""
    return {
        "ok": True,
        "message": f"Silenced {silenced_count} finding{plural} across {len(matched_subjects)} "
                   f"subject{subject_plural}.",
    }


def _execute_add_custom_rule(action: dict) -> dict:
    source = action["source"]
    rule_type = action["rule_type"]
    name = action["name"].strip()
    instruction = action["instruction"].strip()
    rule_id = db.add_ai_custom_rule(source, rule_type, name, instruction)
    # A rule reshapes the review PROMPT, not any already-open finding -- but an ordinary Check
    # now only sends the AI whatever's new since the last checkpoint/file-hash (see log_watcher.py/
    # compose_reviewer.py), so without this the new rule would sit inert until something happens
    # to log again naturally, or an operator reaches for the far more destructive Reset & re-check.
    if source == "logs":
        db.rewind_logs_checkpoint()
    else:
        db.rewind_compose_checkpoint()
    verb = "never flag" if rule_type == "exclude" else "always flag"
    label = "Runtime" if source == "logs" else "Configuration"
    return {
        "ok": True,
        "message": f"Added a standing rule -- {label} checks will now {verb} this going forward.",
        # Lets the Settings page's own AI Custom Rules table (see settings.html's
        # insertCustomRuleRow) insert this row live, without a reload, if the operator happens
        # to confirm this from the chat widget while already on that page.
        "rule": dict(db.get_ai_custom_rule(rule_id)),
    }


_VALIDATORS = {
    "silence_findings": _validate_silence_findings,
    "add_custom_rule": _validate_add_custom_rule,
}
_EXECUTORS = {
    "silence_findings": _execute_silence_findings,
    "add_custom_rule": _execute_add_custom_rule,
}


def execute(action: dict) -> dict:
    """Validates then executes exactly one action dict, returning {"ok": bool, "message": str}
    for the chat UI to append as its own message. Never raises on a malformed or unrecognized
    action -- the confirm button is reachable by anything a browser can POST, not just the
    exact shape chat.py's own extraction produced, so a bad payload is a normal {"ok": False}
    result, not a 500."""
    if not isinstance(action, dict):
        return {"ok": False, "message": "Malformed action."}
    action_type = action.get("type")
    validator = _VALIDATORS.get(action_type)
    if validator is None:
        return {"ok": False, "message": "Unknown action type."}
    error = validator(action)
    if error:
        return {"ok": False, "message": error}
    try:
        return _EXECUTORS[action_type](action)
    except Exception:
        logger.exception("Chat action execution failed: %s", action_type)
        return {"ok": False, "message": "Something went wrong applying that change -- nothing was saved."}
