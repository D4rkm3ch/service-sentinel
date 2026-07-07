"""Regression tests for the real-world bug found during Stage 2 testing: immich-redis
(image 'docker.io/valkey/valkey:8-bookworm@sha256:...', the tag+digest pin format Immich's
own compose recommendations use) was reported as "Needs manual check". Root cause was two
separate bugs: (1) splitting repo:tag on the last ':' without stripping the '@sha256:...'
digest first, which split inside the digest instead of before it, and (2) 'docker.io' not
being recognized as a Docker Hub alias for the real API host 'registry-1.docker.io' (plain
docker.io redirects to Docker's marketing site for arbitrary paths, which looks like a
working response but isn't the registry)."""

from app.docker_client import _split_image_ref
from app.registry import _normalize_repo


def test_split_image_ref_strips_digest_pin_before_finding_tag():
    repo, tag = _split_image_ref(
        "docker.io/valkey/valkey:8-bookworm@sha256:"
        "42cba146593a5ea9a622002c1b7cba5da7be248650cbb64ecb9c6c33d29794b1"
    )
    assert repo == "docker.io/valkey/valkey"
    assert tag == "8-bookworm"


def test_split_image_ref_digest_only_pin_defaults_to_latest():
    repo, tag = _split_image_ref(
        "postgres@sha256:" + "a" * 64
    )
    assert repo == "postgres"
    assert tag == "latest"


def test_split_image_ref_unaffected_cases_still_correct():
    assert _split_image_ref("linuxserver/sonarr:latest") == ("linuxserver/sonarr", "latest")
    assert _split_image_ref("postgres") == ("postgres", "latest")
    assert _split_image_ref("registry.example.com:5000/owner/repo:tag") == (
        "registry.example.com:5000/owner/repo", "tag",
    )
    assert _split_image_ref("ghcr.io/immich-app/postgres:14-vectorchord") == (
        "ghcr.io/immich-app/postgres", "14-vectorchord",
    )


def test_normalize_repo_maps_docker_io_alias_to_real_api_host():
    assert _normalize_repo("docker.io/valkey/valkey") == ("registry-1.docker.io", "valkey/valkey")
    assert _normalize_repo("index.docker.io/library/nginx") == ("registry-1.docker.io", "library/nginx")


def test_normalize_repo_unaffected_cases_still_correct():
    assert _normalize_repo("linuxserver/sonarr") == ("registry-1.docker.io", "linuxserver/sonarr")
    assert _normalize_repo("nginx") == ("registry-1.docker.io", "library/nginx")
    assert _normalize_repo("ghcr.io/immich-app/postgres") == ("ghcr.io", "immich-app/postgres")
    assert _normalize_repo("registry.example.com:5000/owner/repo") == (
        "registry.example.com:5000", "owner/repo",
    )
