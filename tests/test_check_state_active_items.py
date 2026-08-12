"""check_state's active-items tracking -- which subjects (container names for logs, file paths
for compose) are in an AI call RIGHT NOW during a bulk check. A real-world report: a bulk check's
only visible progress was an aggregate "Analyzing logs with AI (4/8)…" count, with no way to tell
which of the containers that "4" referred to -- see app/main.py's GET /logs/active-items and
GET /compose/active-items, and _issues_grouped_table.html's own fast poll of them for a
per-row spinner."""

from app import check_state, db

db.init_db()


def setup_function(_):
    check_state.release_running("logs")
    check_state.release_running("compose")


def teardown_function(_):
    check_state.release_running("logs")
    check_state.release_running("compose")


def test_no_active_items_by_default():
    assert check_state.get_active_items("logs") == []


def test_mark_item_active_adds_it():
    check_state.mark_item_active("logs", "radarr")
    assert check_state.get_active_items("logs") == ["radarr"]


def test_mark_item_done_removes_it():
    check_state.mark_item_active("logs", "radarr")
    check_state.mark_item_done("logs", "radarr")
    assert check_state.get_active_items("logs") == []


def test_marking_an_item_done_that_was_never_active_is_a_harmless_no_op():
    check_state.mark_item_done("logs", "never-was-active")  # must not raise
    assert check_state.get_active_items("logs") == []


def test_multiple_active_items_are_all_returned_sorted():
    check_state.mark_item_active("logs", "sonarr")
    check_state.mark_item_active("logs", "radarr")
    assert check_state.get_active_items("logs") == ["radarr", "sonarr"]


def test_active_items_are_scoped_per_feature():
    check_state.mark_item_active("logs", "radarr")
    check_state.mark_item_active("compose", "/compose/media.yaml")
    assert check_state.get_active_items("logs") == ["radarr"]
    assert check_state.get_active_items("compose") == ["/compose/media.yaml"]


def test_release_running_clears_active_items():
    check_state.mark_item_active("logs", "radarr")
    check_state.release_running("logs")
    assert check_state.get_active_items("logs") == []


def test_set_finished_clears_active_items():
    check_state.mark_item_active("logs", "radarr")
    check_state.set_finished("logs", {"checked": 1, "findings_found": 0, "errors": 0})
    assert check_state.get_active_items("logs") == []


# ---------------------------------------------------------------------------
# Routes -- GET /logs/active-items, GET /compose/active-items
# ---------------------------------------------------------------------------

def test_logs_active_items_route_reflects_current_state(client):
    assert client.get("/logs/active-items").json() == {"active": []}
    check_state.mark_item_active("logs", "radarr")
    try:
        assert client.get("/logs/active-items").json() == {"active": ["radarr"]}
    finally:
        check_state.mark_item_done("logs", "radarr")


def test_compose_active_items_route_reflects_current_state(client):
    assert client.get("/compose/active-items").json() == {"active": []}
    check_state.mark_item_active("compose", "/compose/media.yaml")
    try:
        assert client.get("/compose/active-items").json() == {"active": ["/compose/media.yaml"]}
    finally:
        check_state.mark_item_done("compose", "/compose/media.yaml")


def test_logs_and_compose_active_items_routes_are_independent(client):
    check_state.mark_item_active("logs", "radarr")
    try:
        assert client.get("/compose/active-items").json() == {"active": []}
    finally:
        check_state.mark_item_done("logs", "radarr")


def test_updates_active_items_route_reflects_current_state(client):
    assert client.get("/updates/active-items").json() == {"active": []}
    check_state.mark_item_active("updates", "sonarr")
    try:
        assert client.get("/updates/active-items").json() == {"active": ["sonarr"]}
    finally:
        check_state.mark_item_done("updates", "sonarr")
