"""Finds the compose service definition matching a running container, so we can tell
Claude what's actually configured (env var names, volumes, ports, labels) rather than
sending a generic 'here's a changelog' prompt.

Secret-looking values are redacted before anything leaves the network — we only need
env var *names* to check relevance (e.g. "does this release note affect DATABASE_URL"),
never the actual values.
"""

import re

import yaml

from app.config import settings

SECRET_KEY_PATTERN = re.compile(r"(PASSWORD|SECRET|TOKEN|KEY|PASS\b|APIKEY|CREDENTIAL)", re.IGNORECASE)


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
    lives somewhere this tool can't see)."""
    if not settings.compose_root.exists():
        return None

    for path in settings.compose_root.rglob("*.yml"):
        _match = _search_file(path, container_name)
        if _match:
            return _match
    for path in settings.compose_root.rglob("*.yaml"):
        _match = _search_file(path, container_name)
        if _match:
            return _match
    return None


def _search_file(path, container_name: str) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError):
        return None

    if not isinstance(data, dict) or "services" not in data:
        return None

    for service_name, service_def in (data.get("services") or {}).items():
        if not isinstance(service_def, dict):
            continue
        if _service_matches_container(service_name, service_def, container_name):
            return {
                "service_name": service_name,
                "image": service_def.get("image"),
                "environment": _redact_env(service_def.get("environment", {})),
                "volumes": service_def.get("volumes", []),
                "ports": service_def.get("ports", []),
                "labels": service_def.get("labels", []),
                "depends_on": service_def.get("depends_on", []),
                "compose_file": str(path),
            }
    return None


def build_stack_index() -> list[dict]:
    """Walks COMPOSE_ROOT and parses every compose file exactly once, returning a list of
    {"stack_id", "service_names", "services"} per file. Callers should build this ONCE per
    page load and reuse it for every row via match_container_to_stack — calling
    find_service_config per-row instead re-walks and re-parses the entire compose tree once
    per row, which is what made pages with many tracked containers slow to load."""
    if not settings.compose_root.exists():
        return []

    index = []
    paths = list(settings.compose_root.rglob("*.yml")) + list(settings.compose_root.rglob("*.yaml"))
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
    None for containers release-radar can't match to any compose file (not Dockge-managed,
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


def get_service_names_for_file(file_path: str) -> list[str]:
    """Returns the service names defined in a compose file, for display purposes — a raw
    absolute path isn't as meaningful to read as 'sonarr' or 'sonarr, sonarr-config'."""
    from pathlib import Path
    path = Path(file_path)
    try:
        data = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(data, dict) or "services" not in data:
        return []
    return list((data.get("services") or {}).keys())


def subject_display_name(source: str, subject: str) -> str:
    """Friendly display label for a finding's subject: container name as-is for logs, or
    the service name(s) defined in the file for compose (falling back to the raw path if
    the file can't be parsed, e.g. it was deleted since the finding was recorded)."""
    if source == "logs":
        return subject
    names = get_service_names_for_file(subject)
    return ", ".join(names) if names else subject


def list_compose_files() -> list:
    """Returns every compose file release-radar can see under COMPOSE_ROOT."""
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
