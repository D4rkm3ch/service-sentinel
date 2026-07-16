"""Real-world reports against the Compose review AI: it kept flagging network_mode: host (even
though an earlier instruction already tried to soften this to "security concern only, not an
optimization nitpick"), kept flagging missing healthchecks (even though this was explicitly
asked for previously and is now being reversed), flagged an ${VAR} reference to a secret defined
elsewhere as "missing", and flagged Dockge's auto-inserted empty `networks: {}` block as
removable cruft. Locks in the system prompt's current suppression instructions so a future edit
can't silently drop one of these without a test catching it -- this prompt has already regressed
once (network_mode: host's carve-out for "still fine as a security concern" turned out to still
generate real-world noise)."""

from app.summarizer import COMPOSE_REVIEW_SYSTEM_PROMPT_BASE, FIX_FIELD_COMPOSE

_RENDERED = COMPOSE_REVIEW_SYSTEM_PROMPT_BASE.format(fix_field=FIX_FIELD_COMPOSE)


def test_prompt_renders_without_a_format_error():
    """The prompt is passed through str.format() at call time (see review_compose_file) -- any
    literal brace or ${...} text added to it must be escaped ({{ }}), or this blows up with a
    KeyError/IndexError the moment a real review runs."""
    assert "{fix_field}" not in _RENDERED
    assert "${VARIABLE}" in _RENDERED


def test_prompt_fully_suppresses_network_mode_host():
    assert "network_mode: host, in any form" in _RENDERED
    # The old wording still allowed flagging it as a security concern -- that carve-out is what
    # kept generating real-world false positives, so it must be gone now, not just softened.
    assert "still fine to flag it as" not in _RENDERED.lower()


def test_prompt_fully_suppresses_missing_healthchecks():
    assert "Any missing healthcheck, in any form" in _RENDERED
    # The old wording explicitly asked for exactly this flag (the opposite instruction) --
    # confirm it's gone, not just present alongside the new suppression.
    assert "always \"warning\" severity" not in _RENDERED
    assert "can never be satisfied" not in _RENDERED


def test_prompt_suppresses_undefined_env_var_references():
    assert "${VARIABLE}" in _RENDERED
    assert "Docker secret" in _RENDERED
    assert "Never describe this as missing, undefined, broken" in _RENDERED


def test_prompt_suppresses_empty_networks_block():
    assert "networks: {}" in _RENDERED
    assert "Dockge" in _RENDERED


def test_prompt_still_reinforces_reading_the_actual_mount_suffix():
    assert ":ro" in _RENDERED
    assert "character by character" in _RENDERED


def test_prompt_forcefully_suppresses_explicit_rw_recommendations():
    """A real-world report: the AI still recommended adding an explicit :rw "for clarity"
    despite an earlier, softer version of this exclusion naming that exact reasoning -- this
    locks in the strengthened wording so a future edit can't quietly soften it back."""
    assert "NEVER recommend this, under any framing" in _RENDERED
    assert "makes the intent clearer" in _RENDERED


def test_prompt_suppresses_puid_pgid_as_redundant():
    for var in ("PUID", "PGID", "GUID", "UID", "TZ"):
        assert var in _RENDERED
    assert "linuxserver.io" in _RENDERED
    assert "Never flag these as redundant" in _RENDERED


def test_prompt_teaches_media_managers_need_rw_on_library_mounts():
    """A real-world report: the AI assumed Sonarr's media mount only needed read access and
    flagged it as an unnecessary security concern -- Sonarr renames/moves/deletes files in that
    library as its normal job. Locks in the specific tool list so a future edit doesn't quietly
    drop it back to the old, wrong "media libraries are read-only" assumption."""
    assert "media *manager* or *processor*" in _RENDERED
    for tool in (
        "Sonarr", "Radarr", "Lidarr", "Readarr", "Whisparr", "Bazarr", "Prowlarr",
        "Tdarr", "FileFlows", "Cleanuparr", "Kapowarr", "Audiobookshelf", "Huntarr",
        "Janitorr", "Unpackerr", "qBittorrent", "Qui",
    ):
        assert tool in _RENDERED, f"expected {tool!r} in the media-manager suppression list"
