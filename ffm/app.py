"""Wires the components together for both the CLI and the Discord bot."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

from .agent import Advisor
from .config import Config
from .executor import Executor
from .sleeper.auth import SleeperAuth
from .sleeper.public import SleeperPublic
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class App:
    config: Config
    public: SleeperPublic
    store: Store
    advisor: Advisor
    executor: Executor
    auth: SleeperAuth | None

    async def close(self) -> None:
        await self.public.aclose()
        if self.auth:
            await self.auth.aclose()
        self.store.close()


def setup_logging(config: Config, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
        file_handler = RotatingFileHandler(config.data_dir / "ffm.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    for noisy in ("httpx", "httpcore", "anthropic", "discord.gateway", "discord.client", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_app(config: Config) -> App:
    public = SleeperPublic(config.data_dir)
    store = Store(config.db_path)
    advisor = Advisor(config, public, store)
    auth: SleeperAuth | None = None
    if config.sleeper_token:
        auth = SleeperAuth(config.sleeper_token)
        info = auth.token_info
        remaining = info.seconds_remaining
        if info.is_expired:
            log.warning("SLEEPER_TOKEN is expired; roster moves will fail until you capture a new one")
        elif remaining is not None and remaining < 7 * 86400:
            log.warning("SLEEPER_TOKEN expires in %.1f days", remaining / 86400)
    else:
        log.warning("SLEEPER_TOKEN not set: proposals can be reviewed but not executed (dry-run mode)")
    advisor.auth = auth
    executor = Executor(auth, dry_run=config.dry_run, public=public, auto_claim=config.auto_claim)
    if config.dry_run:
        log.info("FFM_DRY_RUN is on: approved proposals are logged, not sent to Sleeper")
    return App(config=config, public=public, store=store, advisor=advisor, executor=executor, auth=auth)
