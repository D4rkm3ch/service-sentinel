"""Direct tests for docker_client's container-listing functions -- previously exercised only
through their callers' mocks (test_improvement_plan.md section 2). Fakes the docker SDK client
the same way test_logs_lookback_window.py fakes it for the log-fetch path: the point is the
label filtering, image-ref parsing, and digest extraction logic, not the Docker API itself."""

from unittest.mock import patch

from app import docker_client


class _FakeImage:
    def __init__(self, repo_digests):
        self.attrs = {"RepoDigests": repo_digests}


class _FakeContainer:
    def __init__(self, name, image_ref, labels=None, repo_digests=None):
        self.name = name
        self.labels = labels or {}
        self.attrs = {"Config": {"Image": image_ref}}
        self.image = _FakeImage(repo_digests or [])


class _FakeContainers:
    def __init__(self, containers):
        self._containers = containers

    def list(self, filters=None):
        assert filters == {"status": "running"}  # only running containers are ever tracked
        return self._containers


class _FakeClient:
    def __init__(self, containers):
        self.containers = _FakeContainers(containers)
        self.closed = False

    def close(self):
        self.closed = True


def _with_containers(containers):
    return patch("app.docker_client.docker.DockerClient", return_value=_FakeClient(containers))


# ---------------------------------------------------------------------------
# list_tracked_containers
# ---------------------------------------------------------------------------

def test_a_plain_container_is_tracked_with_repo_tag_and_digest():
    c = _FakeContainer(
        "sonarr", "linuxserver/sonarr:latest",
        repo_digests=["linuxserver/sonarr@sha256:abc123"],
    )
    with _with_containers([c]):
        result = docker_client.list_tracked_containers()

    assert len(result) == 1
    tracked = result[0]
    assert tracked.name == "sonarr"
    assert tracked.image_repo == "linuxserver/sonarr"
    assert tracked.tag == "latest"
    assert tracked.current_digest == "sha256:abc123"


def test_ignore_label_excludes_a_container_from_update_tracking():
    ignored = _FakeContainer("skipme", "owner/app:1.0", labels={"servicesentinel.ignore": "true"})
    kept = _FakeContainer("keepme", "owner/other:1.0")
    with _with_containers([ignored, kept]):
        result = docker_client.list_tracked_containers()
    assert [t.name for t in result] == ["keepme"]


def test_legacy_releaseradar_ignore_label_still_works():
    ignored = _FakeContainer("oldskip", "owner/app:1.0", labels={"releaseradar.ignore": "true"})
    with _with_containers([ignored]):
        assert docker_client.list_tracked_containers() == []


def test_missing_repo_digests_leaves_current_digest_none():
    c = _FakeContainer("fresh-build", "locally/built:dev", repo_digests=[])
    with _with_containers([c]):
        result = docker_client.list_tracked_containers()
    assert result[0].current_digest is None


def test_untagged_image_defaults_to_latest():
    c = _FakeContainer("bare", "owner/app")
    with _with_containers([c]):
        result = docker_client.list_tracked_containers()
    assert result[0].tag == "latest"


def test_client_is_closed_even_on_success():
    fake = _FakeClient([])
    with patch("app.docker_client.docker.DockerClient", return_value=fake):
        docker_client.list_tracked_containers()
    assert fake.closed


# ---------------------------------------------------------------------------
# list_running_containers_for_logs -- independent ignore label
# ---------------------------------------------------------------------------

def test_logs_listing_respects_the_logs_ignore_label_not_the_updates_one():
    """A container can be excluded from update-checking without being excluded from log
    watching, and vice versa -- the two labels are independent."""
    updates_ignored = _FakeContainer("u", "owner/a:1", labels={"servicesentinel.ignore": "true"})
    logs_ignored = _FakeContainer("l", "owner/b:1", labels={"servicesentinel.logs.ignore": "true"})
    with _with_containers([updates_ignored, logs_ignored]):
        logs_result = docker_client.list_running_containers_for_logs()

    # updates-ignored container still shows up for logs; logs-ignored doesn't.
    assert [t.name for t in logs_result] == ["u"]


def test_legacy_logs_ignore_label_still_works():
    c = _FakeContainer("old", "owner/a:1", labels={"releaseradar.logs.ignore": "true"})
    with _with_containers([c]):
        assert docker_client.list_running_containers_for_logs() == []


# ---------------------------------------------------------------------------
# TrackedContainer label-override properties
# ---------------------------------------------------------------------------

def test_source_and_changelog_overrides_read_from_labels():
    tracked = docker_client.TrackedContainer(
        name="x", image_repo="owner/repo", tag="latest", current_digest=None,
        labels={
            "servicesentinel.source": "owner/real-repo",
            "servicesentinel.changelog_url": "https://example.com/CHANGELOG.md",
        },
    )
    assert tracked.source_override == "owner/real-repo"
    assert tracked.changelog_url_override == "https://example.com/CHANGELOG.md"


def test_overrides_are_none_when_labels_absent():
    tracked = docker_client.TrackedContainer(
        name="x", image_repo="owner/repo", tag="latest", current_digest=None, labels={},
    )
    assert tracked.source_override is None
    assert tracked.changelog_url_override is None
    assert tracked.logs_ignored is False
