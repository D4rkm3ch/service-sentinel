"""Finds the compose service definition matching a running container, so we can tell
Claude what's actually configured (env var names, volumes, ports, labels) rather than
sending a generic 'here's a changelog' prompt.

Secret-looking values are redacted before anything leaves the network -- we only need
env var *names* to check relevance (e.g. "does this release note affect DATABASE_URL"),
never the actual values.
"""

import re
import threading

import yaml

from app import db
from app.config import settings

SECRET_KEY_PATTERN = re.compile(r"(PASSWORD|SECRET|TOKEN|KEY|PASS\b|APIKEY|CREDENTIAL)", re.IGNORECASE)

# Value-shape redaction: catches a secret that doesn't live under a secret-looking key name at
# all, the two most common real-world cases in a homelab compose file --
# 1. A connection-string password (DATABASE_URL=postgres://user:hunter2@host/db,
#    REDIS_URL=redis://:hunter2@host:6379): the key name never matches SECRET_KEY_PATTERN
#    above, but the credential is right there in the value. Only the password segment gets
#    redacted, in place -- keeping the scheme/user/host visible preserves exactly the context
#    an AI compose review needs ("this points at the right host/db"), just not the credential.
_CONN_STRING_CREDENTIAL_PATTERN = re.compile(r"://([^\s/:@]*):([^\s/@]+)@")
# 2. A bearer/webhook-token-shaped value (a Discord/Slack webhook URL's own token segment, an
#    API key pasted directly into a URL or command arg): no user:password@ shape to key off of,
#    just a long opaque-looking run of characters. Requires both a letter and a digit so this
#    doesn't fire on an ordinary long English word or a plain numeric ID -- real tokens are
#    reliably mixed alphanumeric, most homelab path segments and identifiers aren't.
_TOKEN_LOOKALIKE_PATTERN = re.compile(r"[A-Za-z0-9_\-]{20,}")


def _looks_like_a_token(candidate: str) -> bool:
    has_letter = any(c.isalpha() for c in candidate)
    has_digit = any(c.isdigit() for c in candidate)
    return has_letter and has_digit


def _redact_value_shaped_secrets(value):
    """Value-shape redaction, independent of whatever key the value is stored under -- see the
    two pattern comments above. Applied on top of (never instead of) the key-name check in
    _redact_env, since a key literally named PASSWORD is still the clearest signal when it's
    there."""
    if not isinstance(value, str):
        return value
    value = _CONN_STRING_CREDENTIAL_PATTERN.sub(r"://\1:[REDACTED]@", value)
    value = _TOKEN_LOOKALIKE_PATTERN.sub(
        lambda m: "[REDACTED]" if _looks_like_a_token(m.group(0)) else m.group(0), value,
    )
    return value


def _redact_value_shapes_recursive(node):
    """Same value-shape redaction as above, walked over an arbitrary YAML-parsed structure --
    for compose sections with no natural per-item key name to check the way environment:/
    labels: have (command:, and the top-level secrets: block), so only value-shape detection
    applies, not the stronger key-name check _redact_env also does."""
    if isinstance(node, str):
        return _redact_value_shaped_secrets(node)
    if isinstance(node, list):
        return [_redact_value_shapes_recursive(item) for item in node]
    if isinstance(node, dict):
        return {key: _redact_value_shapes_recursive(val) for key, val in node.items()}
    return node

# build_stack_index() used to re-walk COMPOSE_ROOT and re-parse every compose file on every
# call -- and it's called on every Updates/Logs page render AND their 20-second self-refreshing
# table partials, so an idle open tab was re-parsing the entire compose tree three times a
# minute forever. Cached against a snapshot of every file's (path, mtime): the directory walk
# still happens per call, but YAML parsing only re-runs when a file actually changed. The
# cached index is shared -- callers treat it as read-only (match_container_to_stack and
# friends only ever iterate it).
_index_lock = threading.Lock()
_index_snapshot: tuple | None = None
_index_cache: list[dict] = []

# The directory walk itself -- assumed "cheap" above -- turned out to be the dominant cost with
# a real-sized compose tree (a homelab-scale ~43-file report showed a single /updates render
# under concurrent load spending ~85% of its time here): two full recursive Path.rglob() passes
# plus a stat() per file, all pure-Python pathlib work that holds the GIL the whole time. With
# several open tabs each independently rendering/auto-refreshing Updates, Logs, and Compose,
# concurrent requests landing at the same moment each did their own full redundant walk,
# serializing under the GIL instead of overlapping -- turning a sub-10ms request into a
# multi-second one under load.
#
# Fixed with request coalescing ("single-flight"), not a time-based cache: a time-based cache
# was tried first and reverted -- it broke the suite everywhere a test writes a compose file
# and immediately expects build_stack_index() to see it, which turned out to be a load-bearing
# assumption across many unrelated test files, not just this module's own. Coalescing instead
# means calls that are genuinely concurrent (arrive while a walk is already in flight) share
# that one walk's result; a call that arrives after the in-flight walk has finished always
# starts a brand new one. No staleness window, ever -- only truly-simultaneous callers are
# deduplicated.
_snapshot_lock = threading.Lock()
_snapshot_inflight: tuple[threading.Event, dict] | None = None


def _redact_env(env) -> dict:
    """Compose env can be a list of 'KEY=value' strings or a dict; normalize to a dict
    with secret-looking values redacted."""
    result = {}
    if isinstance(env, dict):
        items = env.items()
    elif isinstance(env, list):
        items = []
        for entry in env:
            if "=" in entry:
                k, v = entry.split("=", 1)
                items.append((k, v))
            else:
                items.append((entry, None))
    else:
        return {}

    for key, value in items:
        if SECRET_KEY_PATTERN.search(key):
            result[key] = "[REDACTED]"
        else:
            result[key] = _redact_value_shaped_secrets(value)
    return result


def _service_matches_container(service_name: str, service_def: dict, container_name: str) -> bool:
    explicit_name = service_def.get("container_name")
    if explicit_name:
        return explicit_name == container_name
    # Compose defaults container names to '<project>-<service>-<n>' or '<project>_<service>_<n>'.
    return service_name == container_name or container_name.strip("/").endswith(f"-{service_name}") \
        or container_name.strip("/").endswith(f"_{service_name}")


def find_service_config(container_name: str) -> dict | None:
    """Searches COMPOSE_ROOT for a service matching container_name. Returns a redacted,
    trimmed dict of just the fields relevant to a release-note relevance check, or None
    if no match was found (the container may not be Dockge-managed, or the compose file
    lives somewhere this tool can't see). Matches against the cached stack index, so a
    check summarizing N containers no longer re-walks and re-parses the whole compose tree
    N times over."""
    for entry in build_stack_index():
        for service_name, service_def in entry["services"].items():
            if _service_matches_container(service_name, service_def, container_name):
                return {
                    "service_name": service_name,
                    "image": service_def.get("image"),
                    "environment": _redact_env(service_def.get("environment", {})),
                    "volumes": service_def.get("volumes", []),
                    "ports": service_def.get("ports", []),
                    "labels": _redact_env(service_def.get("labels", [])),
                    "depends_on": service_def.get("depends_on", []),
                    "compose_file": entry["stack_id"],
                }
    return None


def _walk_compose_root() -> tuple[list, tuple]:
    """The actual, uncached directory walk -- every compose file under COMPOSE_ROOT plus a
    change-detection fingerprint of their (path, mtime, size) triples. A file deleted mid-walk
    is simply skipped -- it'll be picked up (as a removal) on the next call."""
    paths = list(settings.compose_root.rglob("*.yml")) + list(settings.compose_root.rglob("*.yaml"))
    snapshot = []
    live_paths = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        live_paths.append(path)
        snapshot.append((str(path), stat.st_mtime_ns, stat.st_size))
    return live_paths, tuple(snapshot)


def _compose_paths_snapshot() -> tuple[list, tuple]:
    """Coalesced wrapper around _walk_compose_root() -- see the module-level comment above
    _snapshot_lock for why this exists and why it's coalescing rather than a time-based cache.
    The first caller to arrive becomes the leader and does the real walk; anyone else who
    calls in while that's in flight waits on the same event and gets the identical result
    instead of doing their own. Once the leader finishes, the slot clears immediately -- the
    very next caller (even the leader's own very next call) always triggers a brand new walk."""
    global _snapshot_inflight

    with _snapshot_lock:
        if _snapshot_inflight is not None:
            event, holder = _snapshot_inflight
            am_leader = False
        else:
            event = threading.Event()
            holder = {}
            _snapshot_inflight = (event, holder)
            am_leader = True

    if not am_leader:
        event.wait()
        return holder["result"]

    try:
        holder["result"] = _walk_compose_root()
    finally:
        with _snapshot_lock:
            _snapshot_inflight = None
        event.set()
    return holder["result"]


def build_stack_index() -> list[dict]:
    """Parses every compose file under COMPOSE_ROOT into a list of {"stack_id",
    "service_names", "services"} per file. Callers treat the result as read-only and, within
    one page render, should build it ONCE and reuse it for every row via
    match_container_to_stack. Across calls it's cached against the files' mtimes (see the
    module-level comment), so only an actual compose-file change costs a re-parse."""
    global _index_snapshot, _index_cache

    if not settings.compose_root.exists():
        return []

    paths, snapshot = _compose_paths_snapshot()
    with _index_lock:
        if snapshot == _index_snapshot:
            return _index_cache

    index = []
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text())
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(data, dict) or "services" not in data:
            continue
        services = {
            name: d for name, d in (data.get("services") or {}).items() if isinstance(d, dict)
        }
        if services:
            index.append({
                "stack_id": str(path),
                "service_names": list(services.keys()),
                "services": services,
            })

    with _index_lock:
        _index_snapshot = snapshot
        _index_cache = index
    return index


def match_container_to_stack(container_name: str, index: list[dict]) -> dict | None:
    """Matches a container against an already-built index (see build_stack_index) -- no file
    I/O or YAML parsing here, just an in-memory comparison."""
    for entry in index:
        for service_name, service_def in entry["services"].items():
            if _service_matches_container(service_name, service_def, container_name):
                return {"stack_id": entry["stack_id"], "service_names": entry["service_names"]}
    return None


def get_stack_info(container_name: str) -> dict | None:
    """Resolves which compose file (stack) a container belongs to, and who else lives in
    that same file. The stack's identity is the compose file's own path -- stable as long
    as the file isn't moved, and naturally shared by every service defined in it. Returns
    None for containers Service Sentinel can't match to any compose file (not Dockge-managed,
    or the compose file lives somewhere it can't see) -- these are left ungrouped.

    This does a fresh directory walk -- for annotating many rows at once (e.g. a whole
    table), use build_stack_index() once and match_container_to_stack() per row instead."""
    config = find_service_config(container_name)
    if not config:
        return None
    compose_file = config["compose_file"]
    return {
        "stack_id": compose_file,
        "service_names": get_service_names_for_file(compose_file),
    }


# Same caching story as the stack index above: subject_display_name() below is called once
# per row when rendering the Compose page's tables (and their 20-second self-refresh
# partials), and each call was re-reading and re-parsing that row's YAML from scratch.
_names_lock = threading.Lock()
_names_cache: dict[str, tuple[tuple, list[str]]] = {}


def get_service_names_for_file(file_path: str) -> list[str]:
    """Returns the service names defined in a compose file, for display purposes -- a raw
    absolute path isn't as meaningful to read as 'sonarr' or 'sonarr, sonarr-config'.
    Cached per file against its (mtime, size), so repeated calls (one per table row per
    render) only cost a stat until the file actually changes."""
    from pathlib import Path
    path = Path(file_path)
    try:
        stat = path.stat()
    except OSError:
        return []
    fingerprint = (stat.st_mtime_ns, stat.st_size)
    with _names_lock:
        cached = _names_cache.get(file_path)
        if cached and cached[0] == fingerprint:
            return cached[1]

    try:
        data = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError):
        return []
    names = list((data.get("services") or {}).keys()) if isinstance(data, dict) and "services" in data else []
    with _names_lock:
        _names_cache[file_path] = (fingerprint, names)
    return names


def subject_display_name(source: str, subject: str) -> str:
    """Friendly display label for a finding's subject: for logs, a manual override if one's
    been set (see db.container_names), otherwise the raw container name as-is; for compose --
    a manual override if one's been set (never auto-overwritten by the computed name),
    otherwise the service name(s) defined in the file (falling back to the raw path if the
    file can't be parsed, e.g. it was deleted since the finding was recorded)."""
    if source == "logs":
        return db.get_container_display_name(subject) or subject
    override = db.get_compose_file_name(subject)
    if override and override["name_source"] == "manual":
        return override["display_name"]
    names = get_service_names_for_file(subject)
    return ", ".join(names) if names else subject


def list_compose_files() -> list:
    """Returns every compose file Service Sentinel can see under COMPOSE_ROOT."""
    if not settings.compose_root.exists():
        return []
    return list(settings.compose_root.rglob("*.yml")) + list(settings.compose_root.rglob("*.yaml"))


def redact_compose_file_text(path) -> str | None:
    """Loads a compose file and returns it re-serialized with secret-looking values redacted
    across every service, for sending to Claude for review. Returns None if the file can't be
    parsed. Covers environment: and labels: (the key-name check plus value-shape detection, via
    _redact_env) and command: plus the top-level secrets: block (value-shape detection only,
    via _redact_value_shapes_recursive -- neither has a natural per-item key name the way
    environment/labels do)."""
    try:
        data = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError):
        return None

    if not isinstance(data, dict) or "services" not in data:
        return None

    for service_def in (data.get("services") or {}).values():
        if not isinstance(service_def, dict):
            continue
        if "environment" in service_def:
            service_def["environment"] = _redact_env(service_def["environment"])
        if "labels" in service_def:
            service_def["labels"] = _redact_env(service_def["labels"])
        if "command" in service_def:
            service_def["command"] = _redact_value_shapes_recursive(service_def["command"])
        if "secrets" in service_def:
            service_def["secrets"] = _redact_value_shapes_recursive(service_def["secrets"])

    if "secrets" in data:
        data["secrets"] = _redact_value_shapes_recursive(data["secrets"])

    return yaml.dump(data, default_flow_style=False, sort_keys=False)
