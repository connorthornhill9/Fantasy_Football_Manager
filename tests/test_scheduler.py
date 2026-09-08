from datetime import datetime
from zoneinfo import ZoneInfo

from ffm.scheduler import _cron_dow_to_apscheduler, cron_trigger

TZ = ZoneInfo("America/Toronto")


def next_fire(expr: str, after: datetime) -> datetime:
    return cron_trigger(expr, TZ).get_next_fire_time(None, after)


def test_day_of_week_uses_standard_cron_numbering():
    assert _cron_dow_to_apscheduler("0") == "sun"
    assert _cron_dow_to_apscheduler("2") == "tue"
    assert _cron_dow_to_apscheduler("6") == "sat"
    assert _cron_dow_to_apscheduler("1-5") == "mon-fri"
    assert _cron_dow_to_apscheduler("0,6") == "sun,sat"
    assert _cron_dow_to_apscheduler("*") == "*"
    assert _cron_dow_to_apscheduler("tue") == "tue"


def test_defaults_fire_on_the_intended_days():
    # Tuesday 2026-09-08 10:00 Toronto: the next Tuesday 09:00 is a week later
    now = datetime(2026, 9, 8, 10, 0, tzinfo=TZ)
    assert next_fire("0 9 * * 2", now).strftime("%a %Y-%m-%d %H:%M") == "Tue 2026-09-15 09:00"
    assert next_fire("0 18 * * 6", now).strftime("%a %Y-%m-%d %H:%M") == "Sat 2026-09-12 18:00"
    assert next_fire("0 9 * * 0", now).strftime("%a %Y-%m-%d %H:%M") == "Sun 2026-09-13 09:00"
