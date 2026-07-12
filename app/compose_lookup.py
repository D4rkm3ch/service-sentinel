"""Finds the compose service definition matching a running container, so we can tell
Claude what's actually configured (env var names, volumes, ports, labels) rather than
sending a generic 'here's a changelog' prompt.

Secret-looking values are redacted before anything leaves the network — we only need
env var *names* to check relevance (e.g. "does this release note affect DATABASE_URL"),
never the actual values.
"""

import re
import threading

import yaml

from app import db
from app.config import settings

SECRET_KEY_PATTERN = re.compile(r"(PASSWORD|SECRET|TOKEN|KEY|PASS\b|APIKEY|CREDENTIAL)", re.IGNORECASE)

# build_stack_index() used to re-walk COMPOSE_ROOT and re-parse every compose file on every
# call -- and it's called on every Updates/Logs page render AND their 20-second self-refreshing
# table partials, so an idle open tab was re-parsing the entire compose tree three times a
# minute forever. Cached against a snapshot of every file's (path, mtime): the directory walk
# still happens per call (cheap, and it's what detects adds/removes), but YAML parsing only
# re-runs when a file actually changed. The cached index is shared -- callers treat it as
# read-only (match_container_to_stack and friends only ever iterate it).
_index_lock = threading.Lock()
_index_snapshot: tuple | None = None
_index_cache: list[dict] = []


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
            result[key] = value
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
                    "labels": service_def.get("labels", []),
                    "depends_on": service_def.get("depends_on", []),
                    "compose_file": entry["stack_id"],
                }
    return None


def _compose_paths_snapshot() -> tuple[list, tuple]:
    """Every compose file under COMPOSE_ROOT plus a change-detection fingerprint of their
    (path, mtime, size) triples. A file deleted mid-walk is simply skipped -- it'll be picked
    up (as a removal) on the next call."""
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
    """Matches a container against an already-built index (see build_stack_index) — no file
    I/O or YAML parsing here, just an in-memory comparison."""
    for entry in index:
        for service_name, service_def in entry["services"].items():
            if _service_matches_container(service_name, service_def, container_name):
                return {"stack_id": entry["stack_id"], "service_names": entry["service_names"]}
    return None


def get_stack_info(container_name: str) -> dict | None:
    """Resolves which compose file (stack) a container belongs to, and who else lives in
    that same file. The stack's identity is the compose file's own path — stable as long
    as the file isn't moved, and naturally shared by every service defined in it. Returns
    None for containers Service Sentinel can't match to any compose file (not Dockge-managed,
    or the compose file lives somewhere it can't see) — these are left ungrouped.

    This does a fresh directory walk — for annotating many rows at once (e.g. a whole
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
    """Returns the service names defined in a compose file, for display purposes — a raw
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
    """Friendly display label for a finding's subject: container name as-is for logs, or
    for compose -- a manual override if one's been set (never auto-overwritten by the
    computed name), otherwise the service name(s) defined in the file (falling back to the
    raw path if the file can't be parsed, e.g. it was deleted since the finding was
    recorded)."""
    if source == "logs":
        return subject
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
    """Loads a compose file and returns it re-serialized with secret-looking env values
    redacted across every service, for sending to Claude for review. Returns None if the
    file can't be parsed."""
    try:
        data = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError):
        return None

    if not isinstance(data, dict) or "services" not in data:
        return None

    for service_def in (data.get("services") or {}).values():
        if isinstance(service_def, dict) and "environment" in service_def:
            service_def["environment"] = _redact_env(service_def["environment"])

    return yaml.dump(data, default_flow_style=False, sort_keys=False)
