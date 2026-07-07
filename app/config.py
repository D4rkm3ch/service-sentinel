import os
from pathlib import Path


class Settings:
    """All configuration in one place, read once at import time.

    Everything is sourced from environment variables (see .env.example) so the
    only thing that changes between deployments is the compose file, not the code.
    """

    def __init__(self) -> None:
        self.anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
        self.github_token: str = os.environ.get("GITHUB_TOKEN", "")
        self.claude_model: str = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

        self.compose_root: Path = Path(os.environ.get("COMPOSE_ROOT", "/compose"))
        self.data_dir: Path = Path(os.environ.get("DATA_DIR", "/data"))
        self.db_path: Path = self.data_dir / "release_radar.db"

        self.docker_socket: str = os.environ.get("DOCKER_SOCKET", "unix://var/run/docker.sock")

        self.tz: str = os.environ.get("TZ", "UTC")

        # Base URL this app is reachable at (e.g. http://192.168.4.59:8420), used to build
        # clickable links in notifications. Optional — links just won't be clickable without it.
        self.public_url: str = os.environ.get("PUBLIC_URL", "").rstrip("/")

        # How many registries to check at once. Registry checks are almost pure network
        # wait time, so doing them one at a time is what makes a large stack slow to check.
        self.registry_check_concurrency: int = int(os.environ.get("REGISTRY_CHECK_CONCURRENCY", "10"))

        # Release notes + AI summarization involve real network and model latency per
        # container (several seconds to tens of seconds each, especially with the web search
        # fallback) — running these one at a time is what makes a check with many pending
        # updates at once (e.g. right after a full reset) take many minutes. Kept lower than
        # the registry check concurrency since these are meaningfully more expensive calls.
        self.ai_summarize_concurrency: int = int(os.environ.get("AI_SUMMARIZE_CONCURRENCY", "4"))

        # Log watcher tuning — schedule itself now lives in the database (Settings tab),
        # not here, but these control how much log data gets pulled and pre-filtered.
        self.log_lookback_hours: int = int(os.environ.get("LOG_LOOKBACK_HOURS", "24"))
        self.log_max_lines_per_container: int = int(os.environ.get("LOG_MAX_LINES_PER_CONTAINER", "5000"))

    def validate(self) -> list[str]:
        """Returns a list of human-readable problems. Empty list means we're good to start."""
        problems = []
        if not self.anthropic_api_key:
            problems.append(
                "ANTHROPIC_API_KEY is not set. The app will start but every check will fail "
                "at the summarization step."
            )
        if not self.compose_root.exists():
            problems.append(
                f"COMPOSE_ROOT ({self.compose_root}) does not exist inside the container. "
                "Check the volume mount in your compose file."
            )
        return problems


settings = Settings()
