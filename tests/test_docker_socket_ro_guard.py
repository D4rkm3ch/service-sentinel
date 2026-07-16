"""A real, recurring report: the AI compose reviewer twice claimed a docker.sock mount was
read-write ("no explicit :ro suffix") on a file that had :ro right there in the redacted text it
was given -- prompt instructions alone (see test_compose_review_prompt_false_positives.py's own
":ro"/character-by-character reinforcement) weren't enough to reliably stop it. Since this is the
single highest-severity, most security-sensitive check the reviewer makes, and it's mechanically
checkable (one path, one suffix), it now gets a deterministic code-level guard: any finding that
mentions the docker socket is dropped outright if every docker.sock mount in the file is
genuinely already :ro, regardless of what the AI claims."""

import json
from unittest.mock import patch

from app.summarizer import (
    _docker_socket_mounts_are_all_read_only,
    _mentions_docker_socket,
    review_compose_file,
)

_RO_YAML = """services:
  fileflows:
    image: revenz/fileflows:stable
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /mnt/data:/app/Data
"""

_RW_YAML = """services:
  fileflows:
    image: revenz/fileflows:stable
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /mnt/data:/app/Data
"""

_NO_SOCKET_YAML = """services:
  app:
    image: owner/app:latest
    volumes:
      - /mnt/data:/app/Data
"""

_DOCKER_SOCKET_FINDING = {
    "title": "Docker socket mounted read-write",
    "category": "security",
    "severity": "critical",
    "description": "The Docker socket is mounted without an explicit read-only (':ro') suffix.",
    "fix": "Change /var/run/docker.sock:/var/run/docker.sock to /var/run/docker.sock:/var/run/docker.sock:ro",
}

_UNRELATED_FINDING = {
    "title": "Missing restart policy",
    "category": "reliability",
    "severity": "warning",
    "description": "No restart policy is set.",
}


def test_docker_socket_mounts_are_all_read_only_true_when_ro():
    assert _docker_socket_mounts_are_all_read_only(_RO_YAML) is True


def test_docker_socket_mounts_are_all_read_only_false_when_rw():
    assert _docker_socket_mounts_are_all_read_only(_RW_YAML) is False


def test_docker_socket_mounts_are_all_read_only_none_when_not_mounted_at_all():
    assert _docker_socket_mounts_are_all_read_only(_NO_SOCKET_YAML) is None


def test_docker_socket_mounts_are_all_read_only_handles_invalid_yaml():
    assert _docker_socket_mounts_are_all_read_only("not: valid: yaml: [") is None


def test_mentions_docker_socket_matches_title_description_and_fix():
    assert _mentions_docker_socket(_DOCKER_SOCKET_FINDING) is True
    assert _mentions_docker_socket(_UNRELATED_FINDING) is False
    assert _mentions_docker_socket({"title": "", "description": "touches /var/run/docker.sock"}) is True


def test_review_compose_file_drops_a_docker_socket_finding_when_the_mount_is_actually_ro():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text",
               return_value=json.dumps([_DOCKER_SOCKET_FINDING, _UNRELATED_FINDING])):
        findings = review_compose_file("fileflows.yml", _RO_YAML)

    titles = [f["title"] for f in findings]
    assert "Docker socket mounted read-write" not in titles
    assert "Missing restart policy" in titles


def test_review_compose_file_keeps_a_docker_socket_finding_when_the_mount_is_genuinely_rw():
    """The guard must not become a blanket suppression -- a real read-write docker.sock mount
    is a genuine finding and must still surface."""
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text",
               return_value=json.dumps([_DOCKER_SOCKET_FINDING])):
        findings = review_compose_file("fileflows.yml", _RW_YAML)

    assert [f["title"] for f in findings] == ["Docker socket mounted read-write"]


def test_review_compose_file_is_unaffected_when_the_file_has_no_docker_socket_mount():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value=json.dumps([_UNRELATED_FINDING])):
        findings = review_compose_file("app.yml", _NO_SOCKET_YAML)

    assert [f["title"] for f in findings] == ["Missing restart policy"]
