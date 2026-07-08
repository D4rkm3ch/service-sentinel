"""_emphasize_stack_mentions() (app/main.py) bolds a stack's own service names when they appear
in the AI-generated cross-service analysis blurb on the stack detail page. Replaces an earlier
version that turned these into same-page "#row-<service>" jump-links -- pointless since the
table listing every member is already visible on the same page, so the link never went
anywhere the reader wasn't already looking at."""

from app.main import _emphasize_stack_mentions


def test_exact_mentions_get_bolded():
    text = _emphasize_stack_mentions("sonarr depends on qbittorrent for downloads.", ["sonarr", "qbittorrent"])
    assert "<strong>sonarr</strong>" in text
    assert "<strong>qbittorrent</strong>" in text
    assert "<a " not in text


def test_no_service_names_or_empty_text_is_a_no_op():
    assert _emphasize_stack_mentions("", ["sonarr"]) == ""
    assert _emphasize_stack_mentions("some text", []) == "some text"


def test_longer_names_are_not_stolen_by_a_shorter_substring_match():
    text = _emphasize_stack_mentions(
        "readarr-ebooks shares config with readarr.", ["readarr", "readarr-ebooks"],
    )
    assert "<strong>readarr-ebooks</strong>" in text
    assert "<strong>readarr</strong>." in text  # the standalone mention, not part of the longer name
    assert "<strong>readarr</strong>-ebooks" not in text


def test_word_boundaries_prevent_partial_matches():
    text = _emphasize_stack_mentions("qbittorrentspare is a separate container.", ["qbittorrent"])
    assert "<strong>" not in text
