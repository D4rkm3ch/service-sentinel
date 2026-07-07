import re
from dataclasses import dataclass, field

import docker

from app.config import settings

IGNORE_LABEL = "releaseradar.ignore"
LOGS_IGNORE_LABEL = "releaseradar.logs.ignore"
SOURCE_LABEL = "releaseradar.source"
CHANGELOG_LABEL = "releaseradar.changelog_url"


@dataclass
class TrackedContainer:
    name: str
    image_repo: str  # e.g. "linuxserver/sonarr" or "ghcr.io/owner/repo"
    tag: str  # e.g. "latest", "v4.0.1"
    current_digest: str | None  # sha256:... of the image actually running, if resolvable
    labels: dict = field(default_factory=dict)

    @property
    def source_override(self) -> str | None:
        return self.labels.get(SOURCE_LABEL)

    @property
    def changelog_url_override(self) -> str | None:
        return self.labels.get(CHANGELOG_LABEL)

    @property
    def logs_ignored(self) -> bool:
        return self.labels.get(LOGS_IGNORE_LABEL, "").lower() == "true"


_DIGEST_SUFFIX = re.compile(r"@[a-zA-Z0-9]+:[0-9a-fA-F]{32,}$")


def _split_image_ref(image_ref: str) -> tuple[str, str]:
    """Split 'repo:tag' into (repo, tag), defaulting to 'latest' when no tag is present.

    Handles registry hosts with ports, e.g. 'registry.example.com:5000/owner/repo:tag', and
    images pinned by both tag and digest (e.g. 'valkey/valkey:8-bookworm@sha256:...', which
    Immich's own compose recommendations use) by dropping the '@sha256:...' suffix first —
    otherwise the digest's own colon gets mistaken for the tag separator.
    """
    image_ref = _DIGEST_SUFFIX.sub("", image_ref)

    last_segment = image_ref.rsplit("/", 1)[-1]
    if ":" in last_segment:
        repo, tag = image_ref.rsplit(":", 1)
    else:
        repo, tag = image_ref, "latest"
    return repo, tag


def list_tracked_containers() -> list[TrackedContainer]:
    client = docker.DockerClient(base_url=settings.docker_socket)
    try:
        containers = client.containers.list(filters={"status": "running"})
        result = []
        for c in containers:
            labels = c.labels or {}
            if labels.get(IGNORE_LABEL, "").lower() == "true":
                continue

            image_ref = c.attrs["Config"]["Image"]
            repo, tag = _split_image_ref(image_ref)

            digest = None
            repo_digests = c.image.attrs.get("RepoDigests") or []
            if repo_digests:
                # Format is 'repo@sha256:...'; take the digest portion of the first match.
                digest = repo_digests[0].split("@")[-1]

            result.append(
                TrackedContainer(
                    name=c.name,
                    image_repo=repo,
                    tag=tag,
                    current_digest=digest,
                    labels=labels,
                )
            )
        return result
    finally:
        client.close()


def list_running_containers_for_logs() -> list[TrackedContainer]:
    """Like list_tracked_containers, but only excludes containers via LOGS_IGNORE_LABEL
    rather than IGNORE_LABEL — a container can be excluded from update-checking without
    being excluded from log watching, or vice versa."""
    client = docker.DockerClient(base_url=settings.docker_socket)
    try:
        containers = client.containers.list(filters={"status": "running"})
        result = []
        for c in containers:
            labels = c.labels or {}
            if labels.get(LOGS_IGNORE_LABEL, "").lower() == "true":
                continue
            image_ref = c.attrs["Config"]["Image"]
            repo, tag = _split_image_ref(image_ref)
            result.append(TrackedContainer(name=c.name, image_repo=repo, tag=tag, current_digest=None, labels=labels))
        return result
    finally:
        client.close()


def get_container_logs_since(container_name: str, since_iso: str | None, max_lines: int) -> str | None:
    """Returns up to max_lines of log text for a container since the given ISO timestamp
    (or the configured lookback window if since_iso is None — first time this container has
    been checked). Returns None if the container can't be found or logs can't be read."""
    client = docker.DockerClient(base_url=settings.docker_socket)
    try:
        try:
            container = client.containers.get(container_name)
        except docker.errors.NotFound:
            return None

        kwargs = {"tail": max_lines, "timestamps": False}
        if since_iso:
            # docker-py accepts a datetime or a unix timestamp for `since`.
            from datetime import datetime
            try:
                kwargs["since"] = datetime.fromisoformat(since_iso)
            except ValueError:
                pass
        else:
            from datetime import datetime, timedelta, timezone
            kwargs["since"] = datetime.now(timezone.utc) - timedelta(hours=settings.log_lookback_hours)

        raw = container.logs(**kwargs)
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return "\n".join(lines)
    finally:
        client.close()
