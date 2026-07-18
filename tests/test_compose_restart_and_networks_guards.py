"""Deterministic code-level guards added alongside the docker.sock :ro guard
(test_docker_socket_ro_guard.py), for the same "mechanically checkable, don't rely on prompt
compliance alone" reason: a real-world report showed the AI compose reviewer claiming a service
was missing a restart policy despite `restart: unless-stopped` being right there in the file, and
separately flagging Dockge's auto-inserted empty `networks: {}` block despite an existing prompt
instruction saying not to. Also covers the generalized docker-socket guard, which now also drops
a docker-socket finding when the file has no docker.sock mount at all (a real-world report: the
model named "Docker socket mounted read-write" for an unrelated database data-volume mount)."""

import json
from unittest.mock import patch

from app.summarizer import (
    _has_empty_top_level_networks_block,
    _mentions_missing_restart_policy_for,
    _mentions_networks_block,
    _services_with_a_real_restart_policy,
    review_compose_file,
)

_RESTART_SET_YAML = """services:
  romm-db:
    image: mariadb:latest
    container_name: romm-db
    restart: unless-stopped
"""

_RESTART_MISSING_YAML = """services:
  romm-db:
    image: mariadb:latest
    container_name: romm-db
"""

_RESTART_EXPLICIT_NO_YAML = """services:
  romm-db:
    image: mariadb:latest
    restart: "no"
"""

_MISSING_RESTART_FINDING = {
    "title": "romm-db missing restart policy",
    "category": "reliability",
    "severity": "warning",
    "description": "The romm-db service currently has no restart policy defined.",
}

_UNRELATED_FINDING = {
    "title": "Insecure authentication setting",
    "category": "security",
    "severity": "critical",
    "description": "no-auth is enabled",
}

_NO_DOCKER_SOCKET_YAML = """services:
  romm-db:
    image: mariadb:latest
    volumes:
      - /var/lib/mysql:/var/lib/mysql
"""

_HALLUCINATED_DOCKER_SOCKET_FINDING = {
    "title": "romm-db: Docker socket mounted read-write",
    "category": "security",
    "severity": "critical",
    "description": "The romm-db service has a volume mount of /var/lib/mysql which is read-write.",
}

_EMPTY_NETWORKS_YAML = """services:
  app:
    image: owner/app:latest
networks: {}
"""

_NO_NETWORKS_BLOCK_YAML = """services:
  app:
    image: owner/app:latest
"""

_NETWORKS_FINDING = {
    "title": "Remove unused networks block",
    "category": "optimization",
    "severity": "suggestion",
    "description": "The networks: {} block is empty and serves no functional purpose.",
    "fix": "Remove the entire networks: {} block from the compose file.",
}


# ---------------------------------------------------------------------------
# Restart policy guard
# ---------------------------------------------------------------------------

def test_services_with_a_real_restart_policy_recognizes_unless_stopped():
    assert _services_with_a_real_restart_policy(_RESTART_SET_YAML) == {"romm-db"}


def test_services_with_a_real_restart_policy_empty_when_key_is_absent():
    assert _services_with_a_real_restart_policy(_RESTART_MISSING_YAML) == set()


def test_services_with_a_real_restart_policy_empty_when_explicitly_no():
    assert _services_with_a_real_restart_policy(_RESTART_EXPLICIT_NO_YAML) == set()


def test_services_with_a_real_restart_policy_handles_invalid_yaml():
    assert _services_with_a_real_restart_policy("not: valid: yaml: [") == set()


def test_mentions_missing_restart_policy_for_matches_service_name_and_restart_keyword():
    assert _mentions_missing_restart_policy_for(_MISSING_RESTART_FINDING, "romm-db") is True
    assert _mentions_missing_restart_policy_for(_MISSING_RESTART_FINDING, "other-svc") is False
    assert _mentions_missing_restart_policy_for(_UNRELATED_FINDING, "romm-db") is False


def test_review_compose_file_drops_a_restart_finding_when_the_service_already_has_one():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text",
               return_value=json.dumps([_MISSING_RESTART_FINDING, _UNRELATED_FINDING])):
        findings = review_compose_file("romm.yml", _RESTART_SET_YAML)

    titles = [f["title"] for f in findings]
    assert "romm-db missing restart policy" not in titles
    assert "Insecure authentication setting" in titles


def test_review_compose_file_keeps_a_restart_finding_when_genuinely_missing():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text",
               return_value=json.dumps([_MISSING_RESTART_FINDING])):
        findings = review_compose_file("romm.yml", _RESTART_MISSING_YAML)

    assert [f["title"] for f in findings] == ["romm-db missing restart policy"]


# ---------------------------------------------------------------------------
# Generalized docker-socket guard -- now also fires when there's no docker.sock mount at all
# ---------------------------------------------------------------------------

def test_review_compose_file_drops_a_hallucinated_docker_socket_finding_with_no_socket_mount():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text",
               return_value=json.dumps([_HALLUCINATED_DOCKER_SOCKET_FINDING, _UNRELATED_FINDING])):
        findings = review_compose_file("romm.yml", _NO_DOCKER_SOCKET_YAML)

    titles = [f["title"] for f in findings]
    assert "romm-db: Docker socket mounted read-write" not in titles
    assert "Insecure authentication setting" in titles


# ---------------------------------------------------------------------------
# Empty top-level networks: {} guard
# ---------------------------------------------------------------------------

def test_has_empty_top_level_networks_block_true_for_empty_mapping():
    assert _has_empty_top_level_networks_block(_EMPTY_NETWORKS_YAML) is True


def test_has_empty_top_level_networks_block_false_when_key_absent():
    assert _has_empty_top_level_networks_block(_NO_NETWORKS_BLOCK_YAML) is False


def test_has_empty_top_level_networks_block_false_for_a_real_networks_block():
    yaml_text = "services:\n  app:\n    image: x\nnetworks:\n  proxy:\n    external: true\n"
    assert _has_empty_top_level_networks_block(yaml_text) is False


def test_has_empty_top_level_networks_block_handles_invalid_yaml():
    assert _has_empty_top_level_networks_block("not: valid: yaml: [") is False


def test_mentions_networks_block_matches_the_dockge_boilerplate_finding():
    assert _mentions_networks_block(_NETWORKS_FINDING) is True
    assert _mentions_networks_block(_UNRELATED_FINDING) is False


def test_review_compose_file_drops_a_networks_block_finding_when_it_is_dockge_boilerplate():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text",
               return_value=json.dumps([_NETWORKS_FINDING, _UNRELATED_FINDING])):
        findings = review_compose_file("app.yml", _EMPTY_NETWORKS_YAML)

    titles = [f["title"] for f in findings]
    assert "Remove unused networks block" not in titles
    assert "Insecure authentication setting" in titles


def test_review_compose_file_is_unaffected_when_there_is_no_empty_networks_block():
    with patch("app.summarizer.ai_provider.is_configured", return_value=True), \
         patch("app.summarizer.ai_provider.complete_text", return_value=json.dumps([_UNRELATED_FINDING])):
        findings = review_compose_file("app.yml", _NO_NETWORKS_BLOCK_YAML)

    assert [f["title"] for f in findings] == ["Insecure authentication setting"]
