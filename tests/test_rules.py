"""Pure unit tests for the business rules R1 - R5, R8 (no database needed)."""
from datetime import date, datetime, timezone

import pytest

from app import main as m

IST = m.IST


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)


# --- R2 late ---------------------------------------------------------------- #
@pytest.mark.parametrize("h,mi,s,expected", [
    (9, 40, 0, 0),      # exactly 10:00 after start -> inside grace
    (9, 40, 1, 10),     # one second over -> late, counted from shift start
    (10, 15, 59, 45),
    (9, 0, 0, 0),       # early
])
def test_late_minutes(h, mi, s, expected):
    assert m.compute_late_minutes(ist(2026, 7, 6, h, mi, s), "2026-07-06", "09:30", "18:30") == expected


def test_late_ignores_sub_second_via_truncation():
    ts = m.from_ms(int(ist(2026, 7, 6, 9, 40, 0).timestamp() * 1000) + 900)  # 09:40:00.900
    assert m.compute_late_minutes(ts, "2026-07-06", "09:30", "18:30") == 0


# --- R3 overtime ------------------------------------------------------------ #
@pytest.mark.parametrize("h,mi,expected", [(18, 59, 0), (19, 0, 30), (19, 10, 40), (18, 0, 0)])
def test_overtime(h, mi, expected):
    assert m.compute_overtime_minutes(ist(2026, 7, 6, h, mi), "2026-07-06", "09:30", "18:30") == expected


def test_overtime_overnight_shift_ends_next_day():
    out = ist(2026, 7, 7, 6, 40)
    assert m.compute_overtime_minutes(out, "2026-07-06", "22:00", "06:00") == 40


# --- R4 / R5 / R8 ----------------------------------------------------------- #
def test_work_hours_half_up_not_bankers():
    a = ist(2026, 7, 6, 9, 0, 0)
    # 18 s = 0.005 h, an exact tie: half-up -> 0.01 (banker's rounding would give 0.0)
    assert m.compute_work_hours(a, ist(2026, 7, 6, 9, 0, 18)) == 0.01
    assert m.compute_work_hours(a, ist(2026, 7, 6, 18, 7, 0)) == 9.12   # 9.11666..


def test_half_day_uses_rounded_hours():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    pin = ist(2026, 7, 6, 9, 30)
    # 4h29m59s = 4.49972 h -> rounds to 4.50 -> NOT a half day
    assert m.derive_fields("PRESENT", pin, ist(2026, 7, 6, 13, 59, 59), "2026-07-06", emp)["half_day"] is False
    assert m.derive_fields("PRESENT", pin, ist(2026, 7, 6, 13, 59, 0), "2026-07-06", emp)["half_day"] is True


def test_round_half_up_reporting():
    assert m.round_half_up(2.675, 2) == 2.68
    assert m.round_half_up(0.12345, 4) == 0.1235


# --- R1 attendance date -------------------------------------------------- #
def test_attendance_date_plain_and_ist_boundary():
    # 23:50 UTC on the 6th is already 05:20 IST on the 7th
    ts = datetime(2026, 7, 6, 23, 50, tzinfo=timezone.utc)
    assert m.attendance_date(ts, "09:30", "18:30") == "2026-07-07"


def test_attendance_date_overnight():
    assert m.attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == "2026-07-06"
    assert m.attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == "2026-07-07"
    assert m.attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"


def test_epoch_conversion_roundtrip_and_truncation():
    assert m.to_ms(m.from_ms(1783312500999)) == 1783312500000
    assert m.to_ms(datetime(2026, 7, 6, 4, 35)) == m.to_ms(datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc))


# --- R7 ---------------------------------------------------------------------- #
def test_weekdays_between():
    assert m.weekdays_between(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert m.weekdays_between(date(2026, 7, 13), date(2026, 7, 31)) == 15
    assert m.weekdays_between(date(2026, 7, 4), date(2026, 7, 5)) == 0
    assert m.weekdays_between(date(2026, 8, 1), date(2026, 7, 31)) == 0
