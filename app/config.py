import os
from pathlib import Path


class Settings:
    """All configuration in one place, read once at import time.

    Everything is sourced from environment variables (see .env.example) so the
    only thing that changes between deployments is the compose file, not the code.
    """

    def __init__(self) -> None:
        # AI provider/key/model, and the GitHub token, moved to Settings (see app/ai_provider.py,
        # app/db.py) so they can change without a redeploy -- no longer read from env vars here.
        # ANTHROPIC_API_KEY, CLAUDE_MODEL, and GITHUB_TOKEN are still read once, at db.init_db()
        # time only, to carry over an existing install's compose-file values into the database
        # on upgrade.
        self.compose_root: Path = Path(os.environ.get("COMPOSE_ROOT", "/compose"))
        self.data_dir: Path = Path(os.environ.get("DATA_DIR", "/data"))
        self.db_path: Path = self.data_dir / "service_sentinel.db"

        self.docker_socket: str = os.environ.get("DOCKER_SOCKET", "unix://var/run/docker.sock")

        self.tz: str = os.environ.get("TZ", "UTC")

        # Base URL this app is reachable at (e.g. http://192.168.4.59:8420), used to build
        # clickable links in notifications. Optional — links just won't be clickable without it.
        self.public_url: str = os.environ.get("PUBLIC_URL", "").rstrip("/")

        # How many registries to check at once. Registry checks are almost pure network
        # wait time, so doing them one at a time is what makes a large stack slow to check.
        self.registry_check_concurrency: int = int(os.environ.get("REGISTRY_CHECK_CONCURRENCY", "10"))

        # Release notes fetching and AI summarization concurrency used to live here as one
        # global env-var-backed value (AI_SUMMARIZE_CONCURRENCY). It's now per-provider and
        # UI-editable from Settings instead -- see db.get_anthropic_concurrency/
        # get_gemini_concurrency and ai_provider.concurrency_limit() -- since the right number
        # genuinely differs by provider (and by tier within a provider), not something one
        # deploy-time constant could fit, and operators need to be able to change it without a
        # redeploy the same way the provider/model/key already can.

        # Log watcher tuning — schedule itself now lives in the database (Settings tab),
        # not here, but these control how much log data gets pulled and pre-filtered.
        self.log_lookback_hours: int = int(os.environ.get("LOG_LOOKBACK_HOURS", "24"))
        self.log_max_lines_per_container: int = int(os.environ.get("LOG_MAX_LINES_PER_CONTAINER", "5000"))

    def validate(self) -> list[str]:
        """Returns a list of human-readable problems. Empty list means we're good to start.
        No longer checks for an AI provider key here -- that's now a Settings-page concern
        (see app/ai_provider.py), checked at call time by whichever feature needs it, not a
        startup-time requirement the whole app depends on."""
        problems = []
        if not self.compose_root.exists():
            problems.append(
                f"COMPOSE_ROOT ({self.compose_root}) does not exist inside the container. "
                "Check the volume mount in your compose file."
            )
        return problems


settings = Settings()
