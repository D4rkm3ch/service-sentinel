"""Security hardening: a container's servicesentinel.changelog_url label (docker_client.py's
changelog_url_override property) reaches release_notes._fetch_manual_url with no validation of
scheme or host, before this fix -- httpx.Client(follow_redirects=True).get(url) issued an
outbound request to whatever the label said. A container's labels aren't necessarily something
the operator typed themselves; they can come baked into a third-party image's own Dockerfile, so
any container carrying that label could make the server probe other devices on the LAN it can
reach but the label author never should, or (in a cloud-hosted deployment) the cloud metadata
endpoint -- a classic SSRF setup, since the server, not the operator's browser, makes the request.

Fixed by validating the URL with _is_safe_public_url before every request, including each
redirect hop (follow_redirects=False plus a manual loop in _fetch_manual_url, since httpx's own
follow_redirects=True would otherwise let a validated-safe initial URL redirect straight into an
internal address one hop later)."""

import socket
from unittest.mock import MagicMock, patch

import pytest

from app import release_notes

# ---------------------------------------------------------------------------
# _is_public_ip -- pure IP classification, no DNS involved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ip", [
    "8.8.8.8",           # public
    "1.1.1.1",           # public
    "93.184.216.34",     # public (example.com's old real address, still a valid public IP)
])
def test_is_public_ip_accepts_real_public_addresses(ip):
    import ipaddress
    assert release_notes._is_public_ip(ipaddress.ip_address(ip)) is True


@pytest.mark.parametrize("ip", [
    "127.0.0.1",         # loopback
    "10.0.0.5",          # RFC 1918 private
    "172.16.0.1",        # RFC 1918 private
    "192.168.1.1",       # RFC 1918 private
    "169.254.169.254",   # link-local -- the cloud metadata address specifically
    "169.254.1.1",       # link-local, general
    "224.0.0.1",         # multicast
    "0.0.0.0",           # unspecified
    "::1",                # IPv6 loopback
    "fc00::1",            # IPv6 unique local (private)
    "fe80::1",            # IPv6 link-local
])
def test_is_public_ip_rejects_internal_and_special_ranges(ip):
    import ipaddress
    assert release_notes._is_public_ip(ipaddress.ip_address(ip)) is False


# ---------------------------------------------------------------------------
# _is_safe_public_url -- scheme + hostname + real DNS resolution
# ---------------------------------------------------------------------------


def test_is_safe_public_url_rejects_a_non_http_scheme():
    assert release_notes._is_safe_public_url("file:///etc/passwd") is False
    assert release_notes._is_safe_public_url("ftp://example.com/x") is False


def test_is_safe_public_url_rejects_a_url_with_no_hostname():
    assert release_notes._is_safe_public_url("https://") is False


def test_is_safe_public_url_rejects_when_dns_resolution_fails():
    with patch("app.release_notes.socket.getaddrinfo", side_effect=socket.gaierror("no such host")):
        assert release_notes._is_safe_public_url("https://this-does-not-resolve.example") is False


def test_is_safe_public_url_accepts_a_hostname_resolving_to_a_public_address():
    with patch("app.release_notes.socket.getaddrinfo", return_value=[
        (2, 1, 6, "", ("93.184.216.34", 0)),
    ]):
        assert release_notes._is_safe_public_url("https://example.com/CHANGELOG.md") is True


def test_is_safe_public_url_rejects_a_hostname_resolving_to_a_private_address():
    """The core SSRF case: a container label pointing at an internal LAN address, or a hostname
    an attacker controls the DNS for and points at one."""
    with patch("app.release_notes.socket.getaddrinfo", return_value=[
        (2, 1, 6, "", ("192.168.1.50", 0)),
    ]):
        assert release_notes._is_safe_public_url("https://internal.example/admin") is False


def test_is_safe_public_url_rejects_the_cloud_metadata_address_by_hostname():
    with patch("app.release_notes.socket.getaddrinfo", return_value=[
        (2, 1, 6, "", ("169.254.169.254", 0)),
    ]):
        assert release_notes._is_safe_public_url("https://metadata.internal/latest/meta-data/") is False


def test_is_safe_public_url_rejects_if_any_resolved_address_is_internal():
    """A hostname resolving to multiple addresses (round-robin DNS, or a DNS response an
    attacker partially controls) is rejected if ANY of them is internal, not just the first."""
    with patch("app.release_notes.socket.getaddrinfo", return_value=[
        (2, 1, 6, "", ("93.184.216.34", 0)),
        (2, 1, 6, "", ("10.0.0.1", 0)),
    ]):
        assert release_notes._is_safe_public_url("https://mixed.example/x") is False


# ---------------------------------------------------------------------------
# _fetch_manual_url -- the actual fetch, guard wired in
# ---------------------------------------------------------------------------


def test_fetch_manual_url_refuses_to_even_connect_to_an_unsafe_host():
    with patch("app.release_notes._is_safe_public_url", return_value=False), \
         patch("app.release_notes.httpx.Client") as mock_client_cls:
        notes, url = release_notes._fetch_manual_url("http://169.254.169.254/latest/meta-data/")

    assert (notes, url) == (None, None)
    mock_client_cls.assert_not_called()  # never even opened a connection


def test_fetch_manual_url_succeeds_for_a_safe_url():
    with patch("app.release_notes._is_safe_public_url", return_value=True), \
         patch("app.release_notes.httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        resp = MagicMock(status_code=200, text="changelog text")
        resp.raise_for_status.return_value = None
        mock_client.get.return_value = resp

        notes, url = release_notes._fetch_manual_url("https://example.com/CHANGELOG.md")

    assert notes == "changelog text"
    assert url == "https://example.com/CHANGELOG.md"


def test_fetch_manual_url_revalidates_every_redirect_hop_not_just_the_initial_url():
    """The exact scenario the plan called out: an initially-safe URL redirects to an internal
    address. follow_redirects=True would have followed it transparently; this must not."""
    calls = []

    def fake_is_safe(url):
        calls.append(url)
        # First URL (public) is safe; the redirect target (an internal address) is not.
        return url == "https://example.com/redirect-me"

    with patch("app.release_notes._is_safe_public_url", side_effect=fake_is_safe), \
         patch("app.release_notes.httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        redirect_resp = MagicMock(status_code=302, headers={"location": "http://169.254.169.254/steal-me"})
        mock_client.get.return_value = redirect_resp

        notes, url = release_notes._fetch_manual_url("https://example.com/redirect-me")

    assert (notes, url) == (None, None)
    assert calls == ["https://example.com/redirect-me", "http://169.254.169.254/steal-me"]
    # Only the first hop was ever actually requested -- the second was blocked before connecting.
    assert mock_client.get.call_count == 1


def test_fetch_manual_url_follows_a_safe_redirect_chain_to_a_safe_final_url():
    with patch("app.release_notes._is_safe_public_url", return_value=True), \
         patch("app.release_notes.httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        redirect_resp = MagicMock(status_code=301, headers={"location": "https://example.com/final"})
        final_resp = MagicMock(status_code=200, text="the real changelog")
        final_resp.raise_for_status.return_value = None
        mock_client.get.side_effect = [redirect_resp, final_resp]

        notes, url = release_notes._fetch_manual_url("https://example.com/start")

    assert notes == "the real changelog"
    assert url == "https://example.com/final"


def test_fetch_manual_url_gives_up_after_too_many_redirects():
    with patch("app.release_notes._is_safe_public_url", return_value=True), \
         patch("app.release_notes.httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        redirect_resp = MagicMock(status_code=302, headers={"location": "https://example.com/next"})
        mock_client.get.return_value = redirect_resp

        notes, url = release_notes._fetch_manual_url("https://example.com/loop")

    assert (notes, url) == (None, None)
    assert mock_client.get.call_count == release_notes._MAX_MANUAL_REDIRECTS + 1
