"""Recurring analysis runs (APScheduler)."""
from __future__ import annotations

import logging
from datetime import tzinfo
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config

log = logging.getLogger(__name__)

# (job id, advisor trigger, display name, Config attribute holding the cron expression)
JOBS = (
    ("weekly_waivers", "weekly_waivers", "Waiver-day review", "analysis_cron"),
    ("lineup", "lineup", "Pre-game lineup check", "lineup_cron"),
    ("late_news", "lineup", "Late-week injury check", "news_cron"),
    ("trades", "trades", "Trade ideas", "trade_cron"),
)


def resolve_timezone(config: Config) -> tzinfo | None:
    return ZoneInfo(config.timezone) if config.timezone else None


def build_scheduler(config: Config, run: Callable[[str], Awaitable[object]]) -> AsyncIOScheduler:
    tz = resolve_timezone(config)
    scheduler = AsyncIOScheduler(timezone=tz) if tz else AsyncIOScheduler()
    for job_id, trigger, name, attr in JOBS:
        expr = getattr(config, attr)
        if not expr or expr.lower() == "off":
            continue
        scheduler.add_job(
            run,
            CronTrigger.from_crontab(expr, timezone=tz),
            args=[trigger],
            id=job_id,
            name=name,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        log.info("Scheduled %s: cron '%s'%s", name, expr, f" ({config.timezone})" if config.timezone else " (local time)")
    return scheduler


def describe_jobs(scheduler: AsyncIOScheduler | None) -> str:
    if scheduler is None or not scheduler.running:
        return "scheduler not running"
    lines = []
    for job in scheduler.get_jobs():
        nxt = job.next_run_time.strftime("%a %b %d %H:%M %Z") if job.next_run_time else "paused"
        lines.append(f"{job.name}: next {nxt}")
    return "\n".join(lines) or "no jobs"
