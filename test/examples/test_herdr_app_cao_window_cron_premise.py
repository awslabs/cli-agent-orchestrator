"""The scheduler facts the CAO window's day-of-week and day-and-weekday warnings rest on.

The window warns when the day-of-week field of a flow's schedule holds a digit, because the server's
scheduler (APScheduler, ``CronTrigger.from_crontab``) counts days from Monday as ``0``, unlike standard
cron: ``1-5`` is Tuesday to Saturday, and ``mon-fri`` is what the user means by weekdays
(``examples/herdr-app-cao-window/lib/flows.js`` ``numericWeekdayWarning``; spec ``cao-window-flows``,
"Numeric day of week"). ``lib/flows.test.mjs`` checks what the window says; this file checks that what
it says is still true of the installed APScheduler (3.11.3 when this was written), so an upgrade that
changes the numbering fails here instead of leaving the warning wrong.

A second warning rests on the same scheduler: it matches the day of the month AND the day of the week,
where standard cron matches either (``dayAndWeekdayWarning``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from apscheduler.triggers.cron import CronTrigger

# 2026-11-18 is a Wednesday; the 14 days that follow cover every weekday twice.
START = datetime(2026, 11, 18, 0, 0, 0).astimezone()


def _fire_weekdays(cron: str, count: int = 14) -> list[str]:
    """Names of the days a schedule fires on, from START, one name per fire time."""
    trigger = CronTrigger.from_crontab(cron)
    names: list[str] = []
    after = START - timedelta(seconds=1)
    for _ in range(count):
        after = trigger.get_next_fire_time(None, after + timedelta(seconds=1))
        names.append(after.strftime("%a"))
    return names


def test_numeric_one_to_five_is_tuesday_to_saturday() -> None:
    days = set(_fire_weekdays("0 9 * * 1-5", count=10))
    assert days == {"Tue", "Wed", "Thu", "Fri", "Sat"}


def test_the_names_mon_fri_are_monday_to_friday() -> None:
    days = set(_fire_weekdays("0 9 * * mon-fri", count=10))
    assert days == {"Mon", "Tue", "Wed", "Thu", "Fri"}


def test_zero_is_monday_not_sunday() -> None:
    assert set(_fire_weekdays("0 9 * * 0", count=3)) == {"Mon"}


@pytest.mark.parametrize("cron", ["0 9 * * 1-5", "0 9 * * 0", "0 9 * * */2"])
def test_a_digit_in_the_day_of_week_field_does_not_mean_what_standard_cron_means(cron: str) -> None:
    named = {
        "0 9 * * 1-5": "0 9 * * mon-fri",
        "0 9 * * 0": "0 9 * * sun",
        "0 9 * * */2": "0 9 * * sun,tue,thu,sat",
    }
    assert _fire_weekdays(cron, count=8) != _fire_weekdays(named[cron], count=8)


def test_a_day_of_month_and_a_day_of_week_must_both_match() -> None:
    trigger = CronTrigger.from_crontab("0 9 1 * mon")
    after = START - timedelta(seconds=1)
    fires = []
    for _ in range(6):
        after = trigger.get_next_fire_time(None, after + timedelta(seconds=1))
        fires.append(after)
    # Standard cron would also fire on every Monday; this scheduler fires only on a Monday that is the 1st.
    assert all(f.day == 1 and f.strftime("%a") == "Mon" for f in fires), fires
    assert fires[0].date().isoformat() == "2027-02-01"
