from dataclasses import dataclass, field

import docker

from app.config import settings

IGNORE_LABEL = "releaseradar.ignore"
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


def _split_image_ref(image_ref: str) -> tuple[str, str]:
    """Split 'repo:tag' into (repo, tag), defaulting to 'latest' when no tag is present.

    Handles registry hosts with ports, e.g. 'registry.example.com:5000/owner/repo:tag'.
    """
    if "/" in image_ref:
        host_part, rest = image_ref.split("/", 1)
    else:
        host_part, rest = "", image_ref

    last_segment = rest if not host_part else rest
    if ":" in last_segment.rsplit("/", 1)[-1]:
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
