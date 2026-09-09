"""Configuration loaded from environment variables / a .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ConfigError(f"Expected an integer, got {value!r}") from exc


def _str(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass
class Config:
    # Sleeper
    sleeper_username: str | None
    sleeper_league_id: str | None
    sleeper_token: str | None

    # Discord
    discord_bot_token: str | None
    discord_channel_id: int | None
    discord_owner_id: int | None
    discord_guild_id: int | None

    # Claude
    model: str
    effort: str

    # Behaviour (cron strings, standard 5-field, 0 = Sunday; "off" disables a run)
    cron_fa_sweep: str
    cron_market: str
    cron_post_waivers: str
    cron_late_week: str
    league_notes: str | None
    roast_rating: str  # "pg13" or "r"
    timezone: str | None
    data_dir: Path
    dry_run: bool
    max_proposals: int
    web_search: bool
    espn_news: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ffm.sqlite3"

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_bot_token)

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        load_dotenv(env_file or PROJECT_ROOT / ".env")
        data_dir = Path(os.environ.get("FFM_DATA_DIR") or (PROJECT_ROOT / "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            sleeper_username=_str(os.environ.get("SLEEPER_USERNAME")),
            sleeper_league_id=_str(os.environ.get("SLEEPER_LEAGUE_ID")),
            sleeper_token=_str(os.environ.get("SLEEPER_TOKEN")),
            discord_bot_token=_str(os.environ.get("DISCORD_BOT_TOKEN")),
            discord_channel_id=_int(os.environ.get("DISCORD_CHANNEL_ID")),
            discord_owner_id=_int(os.environ.get("DISCORD_OWNER_ID")),
            discord_guild_id=_int(os.environ.get("DISCORD_GUILD_ID")),
            model=_str(os.environ.get("FFM_MODEL")) or "claude-opus-5",
            effort=_str(os.environ.get("FFM_EFFORT")) or "high",
            cron_fa_sweep=_str(os.environ.get("FFM_CRON_FA_SWEEP")) or "0 8 * * 1",
            cron_market=_str(os.environ.get("FFM_CRON_MARKET")) or "0 9 * * 2",
            cron_post_waivers=_str(os.environ.get("FFM_CRON_POST_WAIVERS")) or "30 3 * * 4",
            cron_late_week=_str(os.environ.get("FFM_CRON_LATE_WEEK")) or "0 18 * * 6",
            league_notes=_str(os.environ.get("FFM_LEAGUE_NOTES")),
            roast_rating=(_str(os.environ.get("FFM_ROAST_RATING")) or "pg13").lower().replace("-", ""),
            timezone=_str(os.environ.get("FFM_TIMEZONE")),
            data_dir=data_dir,
            dry_run=_bool(os.environ.get("FFM_DRY_RUN"), default=False),
            max_proposals=_int(os.environ.get("FFM_MAX_PROPOSALS")) or 6,
            web_search=_bool(os.environ.get("FFM_WEB_SEARCH"), default=True),
            espn_news=_bool(os.environ.get("FFM_ESPN_NEWS"), default=True),
        )

    def require_sleeper(self) -> None:
        if not self.sleeper_username:
            raise ConfigError("SLEEPER_USERNAME is not set (add it to .env)")

    def require_discord(self) -> None:
        missing = [
            name
            for name, value in (
                ("DISCORD_BOT_TOKEN", self.discord_bot_token),
                ("DISCORD_CHANNEL_ID", self.discord_channel_id),
                ("DISCORD_OWNER_ID", self.discord_owner_id),
            )
            if not value
        ]
        if missing:
            raise ConfigError("Missing Discord settings in .env: " + ", ".join(missing))
