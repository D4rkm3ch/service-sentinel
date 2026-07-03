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

        self.check_schedule_cron: str = os.environ.get("CHECK_SCHEDULE_CRON", "0 6 * * *")

        self.compose_root: Path = Path(os.environ.get("COMPOSE_ROOT", "/compose"))
        self.data_dir: Path = Path(os.environ.get("DATA_DIR", "/data"))
        self.db_path: Path = self.data_dir / "release_radar.db"

        self.webhook_token: str = os.environ.get("WEBHOOK_TOKEN", "")

        self.docker_socket: str = os.environ.get("DOCKER_SOCKET", "unix://var/run/docker.sock")

        self.tz: str = os.environ.get("TZ", "UTC")

        self.discord_webhook_url: str = os.environ.get("DISCORD_WEBHOOK_URL", "")
        # Comma or newline separated list of Apprise service URLs, e.g.
        # "mailto://user:pass@gmail.com, ntfy://topic". See https://github.com/caronc/apprise
        # for the full list of supported services and URL formats.
        raw_apprise = os.environ.get("APPRISE_URLS", "")
        self.apprise_urls: list[str] = [
            u.strip() for u in raw_apprise.replace("\n", ",").split(",") if u.strip()
        ]
        # Base URL this app is reachable at (e.g. http://192.168.4.59:8420), used to build
        # clickable links in notifications. Optional — links just won't be clickable without it.
        self.public_url: str = os.environ.get("PUBLIC_URL", "").rstrip("/")

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