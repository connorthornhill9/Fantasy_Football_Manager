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
    ("report_card", "report_card", "Report card", "cron_report"),
    ("fa_sweep", "fa_sweep", "Free-agent sweep", "cron_fa_sweep"),
    ("market", "weekly_waivers", "Market review", "cron_market"),
    ("post_waivers", "post_waivers", "Post-waiver sweep", "cron_post_waivers"),
    ("late_week", "late_week", "Injury replacements + lineup", "cron_late_week"),
)


def resolve_timezone(config: Config) -> tzinfo | None:
    return ZoneInfo(config.timezone) if config.timezone else None


_DOW_NAMES = "mon tue wed thu fri sat sun".split()


def _cron_dow_to_apscheduler(field: str) -> str:
    """Translate a standard-cron day-of-week field (0 = Sunday) into APScheduler's (0 = Monday).

    Names (sun, tue) and '*' pass through; numbers, lists, ranges and steps are converted.
    """
    def convert(token: str) -> str:
        if token.isdigit():
            return _DOW_NAMES[(int(token) - 1) % 7]
        return token

    out = []
    for part in field.split(","):
        step = None
        if "/" in part:
            part, step = part.split("/", 1)
        if "-" in part and part[0].isdigit():
            a, b = part.split("-", 1)
            part = f"{convert(a)}-{convert(b)}"
        else:
            part = convert(part)
        out.append(f"{part}/{step}" if step else part)
    return ",".join(out)


def cron_trigger(expr: str, tz: tzinfo | None) -> CronTrigger:
    """CronTrigger from a standard 5-field cron string (Sunday = 0, like crontab and most docs)."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields: {expr!r}")
    minute, hour, day, month, dow = fields
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=_cron_dow_to_apscheduler(dow), timezone=tz)


def build_scheduler(config: Config, run: Callable[[str], Awaitable[object]]) -> AsyncIOScheduler:
    tz = resolve_timezone(config)
    scheduler = AsyncIOScheduler(timezone=tz) if tz else AsyncIOScheduler()
    for job_id, trigger, name, attr in JOBS:
        expr = getattr(config, attr)
        if not expr or expr.lower() == "off":
            continue
        scheduler.add_job(
            run,
            cron_trigger(expr, tz),
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
