"""Direct tests for registry.get_latest_digest's auth + fallback flow -- previously the least
covered module in the app (test_improvement_plan.md section 2): _normalize_repo had its own
tests (test_image_ref_parsing.py) but the actual token negotiation, challenge parsing, HEAD->GET
fallback, and error handling were never exercised at all. Mocks httpx.Client the same way
test_release_notes.py does -- the point is the decision flow, not network connectivity."""

from unittest.mock import MagicMock, patch

from app import registry


def _response(status_code=200, headers=None, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    if json_body is not None:
        resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


def _client_with(head_responses=None, get_responses=None):
    """Builds a mock httpx.Client context manager with scripted HEAD/GET responses."""
    client = MagicMock()
    if head_responses:
        client.head.side_effect = head_responses
    if get_responses:
        client.get.side_effect = get_responses
    return client


# ---------------------------------------------------------------------------
# The known-realm fast path (Docker Hub / GHCR / lscr.io)
# ---------------------------------------------------------------------------

def test_docker_hub_repo_skips_straight_to_token_then_heads_the_manifest():
    token_resp = _response(json_body={"token": "tok123"})
    manifest_resp = _response(headers={"Docker-Content-Digest": "sha256:abc"})

    with patch("app.registry.httpx.Client") as mock_client_cls:
        client = _client_with(head_responses=[manifest_resp], get_responses=[token_resp])
        mock_client_cls.return_value.__enter__.return_value = client

        digest = registry.get_latest_digest("linuxserver/sonarr", "latest")

    assert digest == "sha256:abc"
    # The token request went to Docker Hub's known realm with a pull scope for this repo.
    token_call = client.get.call_args_list[0]
    assert token_call.args[0] == "https://auth.docker.io/token"
    assert token_call.kwargs["params"]["scope"] == "repository:linuxserver/sonarr:pull"
    # And the manifest HEAD carried the bearer token.
    head_call = client.head.call_args_list[0]
    assert head_call.kwargs["headers"]["Authorization"] == "Bearer tok123"


def test_ghcr_repo_uses_ghcrs_realm():
    token_resp = _response(json_body={"token": "ghcr-tok"})
    manifest_resp = _response(headers={"Docker-Content-Digest": "sha256:def"})

    with patch("app.registry.httpx.Client") as mock_client_cls:
        client = _client_with(head_responses=[manifest_resp], get_responses=[token_resp])
        mock_client_cls.return_value.__enter__.return_value = client

        digest = registry.get_latest_digest("ghcr.io/owner/repo", "v1")

    assert digest == "sha256:def"
    assert client.get.call_args_list[0].args[0] == "https://ghcr.io/token"


# ---------------------------------------------------------------------------
# The unknown-registry try-then-challenge flow
# ---------------------------------------------------------------------------

def test_unknown_registry_tries_unauthenticated_first_and_succeeds_without_a_token():
    manifest_resp = _response(headers={"Docker-Content-Digest": "sha256:selfhosted"})

    with patch("app.registry.httpx.Client") as mock_client_cls:
        client = _client_with(head_responses=[manifest_resp])
        mock_client_cls.return_value.__enter__.return_value = client

        digest = registry.get_latest_digest("registry.example.com/owner/repo", "latest")

    assert digest == "sha256:selfhosted"
    client.get.assert_not_called()  # no token round trip for an open registry


def test_unknown_registry_401_challenge_is_parsed_and_retried_with_a_token():
    challenge = 'Bearer realm="https://reg.example.com/token",service="reg.example.com",scope="repository:owner/repo:pull"'
    first_head = _response(status_code=401, headers={"WWW-Authenticate": challenge})
    token_resp = _response(json_body={"token": "discovered-tok"})
    second_head = _response(headers={"Docker-Content-Digest": "sha256:challenged"})

    with patch("app.registry.httpx.Client") as mock_client_cls:
        client = _client_with(head_responses=[first_head, second_head], get_responses=[token_resp])
        mock_client_cls.return_value.__enter__.return_value = client

        digest = registry.get_latest_digest("reg.example.com/owner/repo", "latest")

    assert digest == "sha256:challenged"
    # The realm came from the challenge header itself, params carried through.
    token_call = client.get.call_args_list[0]
    assert token_call.args[0] == "https://reg.example.com/token"
    assert token_call.kwargs["params"]["service"] == "reg.example.com"
    # Retry carried the discovered token.
    assert client.head.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer discovered-tok"


def test_a_non_bearer_challenge_is_not_retried():
    first_head = _response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="private"'})
    first_head.raise_for_status.side_effect = registry.httpx.HTTPStatusError(
        "401", request=MagicMock(), response=MagicMock(),
    )

    with patch("app.registry.httpx.Client") as mock_client_cls:
        client = _client_with(head_responses=[first_head])
        mock_client_cls.return_value.__enter__.return_value = client

        digest = registry.get_latest_digest("reg.example.com/owner/repo", "latest")

    assert digest is None
    client.get.assert_not_called()


# ---------------------------------------------------------------------------
# Fallbacks and failure modes
# ---------------------------------------------------------------------------

def test_head_405_falls_back_to_get():
    token_resp = _response(json_body={"token": "tok"})
    head_405 = _response(status_code=405)
    get_manifest = _response(headers={"Docker-Content-Digest": "sha256:via-get"})

    with patch("app.registry.httpx.Client") as mock_client_cls:
        client = _client_with(head_responses=[head_405], get_responses=[token_resp, get_manifest])
        mock_client_cls.return_value.__enter__.return_value = client

        digest = registry.get_latest_digest("owner/repo", "latest")

    assert digest == "sha256:via-get"


def test_known_realm_401_falls_back_to_the_actual_challenge():
    """The hardcoded realm assumption turning out wrong for a host must fall back to
    discovering the realm from the real challenge rather than giving up."""
    stale_token_resp = _response(json_body={"token": "stale"})
    challenge = 'Bearer realm="https://other.example.com/token",service="other.example.com"'
    head_401 = _response(status_code=401, headers={"WWW-Authenticate": challenge})
    fresh_token_resp = _response(json_body={"token": "fresh"})
    head_ok = _response(headers={"Docker-Content-Digest": "sha256:fallback"})

    with patch("app.registry.httpx.Client") as mock_client_cls:
        client = _client_with(
            head_responses=[head_401, head_ok],
            get_responses=[stale_token_resp, fresh_token_resp],
        )
        mock_client_cls.return_value.__enter__.return_value = client

        digest = registry.get_latest_digest("owner/repo", "latest")

    assert digest == "sha256:fallback"
    assert client.get.call_args_list[1].args[0] == "https://other.example.com/token"


def test_network_error_returns_none_never_raises():
    with patch("app.registry.httpx.Client") as mock_client_cls:
        client = MagicMock()
        client.get.side_effect = registry.httpx.ConnectError("registry unreachable")
        mock_client_cls.return_value.__enter__.return_value = client

        assert registry.get_latest_digest("owner/repo", "latest") is None


def test_missing_digest_header_returns_none():
    token_resp = _response(json_body={"token": "tok"})
    manifest_resp = _response(headers={})  # 200 but no Docker-Content-Digest

    with patch("app.registry.httpx.Client") as mock_client_cls:
        client = _client_with(head_responses=[manifest_resp], get_responses=[token_resp])
        mock_client_cls.return_value.__enter__.return_value = client

        assert registry.get_latest_digest("owner/repo", "latest") is None
