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
