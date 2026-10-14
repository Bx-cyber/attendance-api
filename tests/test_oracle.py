"""The oracle must agree with numbers worked out by hand (so the oracle itself is trustworthy)."""
from . import oracle as o
from .dataset import EMPLOYEES as E, LOGS as L


def test_monthly_by_hand():
    a = o.employee_monthly(E, L, "EMP0001", "2026-07")
    assert a == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 2.5, "leave_days": 1,
                 "late_count": 2, "total_late_minutes": 47, "total_overtime_minutes": 75, "attendance_pct": 10.87}
    b = o.employee_monthly(E, L, "EMP0002", "2026-07")
    assert (b["working_days"], b["present_days"], b["attendance_pct"], b["late_count"]) == (15, 2.0, 13.33, 1)
    c = o.employee_monthly(E, L, "EMP0003", "2026-07")
    assert (c["working_days"], c["present_days"], c["attendance_pct"]) == (23, 0.0, 0.0)
    z = o.employee_monthly(E, L, "EMP0007", "2026-07")
    assert (z["working_days"], z["attendance_pct"]) == (0, None)


def test_summary_by_hand():
    s = {r["department"]: r for r in o.department_summary(E, L, "2026-07")}
    assert list(s) == ["Engineering", "Sales"]            # Ghost omitted, sorted
    assert s["Engineering"] == {"department": "Engineering", "headcount": 3, "present_days": 4.5, "avg_work_hours": 7.72,
                                "late_count": 3, "total_late_minutes": 67, "leave_count": 1, "on_duty_count": 1}
    assert (s["Sales"]["headcount"], s["Sales"]["present_days"], s["Sales"]["avg_work_hours"]) == (3, 3.0, 8.0)


def test_leaderboard_by_hand():
    rows = o.late_leaderboard(E, L, "2026-07", limit=2)
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP0001"), (2, "EMP0002"), (2, "EMP0004"), (2, "EMP0005")]
    rows = o.late_leaderboard(E, L, "2026-07", limit=10)
    assert [r["rank"] for r in rows] == [1, 2, 2, 2, 5]
    rows = o.late_leaderboard(E, L, "2026-07", limit=1, department="Sales")
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP0004"), (1, "EMP0005")]


def test_trend_by_hand():
    rows = o.department_trend(E, L, "Engineering", "2026-07-06", "2026-07-19")
    assert len(rows) == 14
    assert [r["attendance_rate"] for r in rows] == [0.5, 0.25, 0.0, 0.5, 0.0, None, None, 0.0,
                                                     0.3333, 0.3333, 0.0, 0.0, None, None]
    assert [r["moving_avg_7d"] for r in rows] == [0.5, 0.375, 0.25, 0.3125, 0.25, 0.25, 0.25, 0.15,
                                                   0.1667, 0.2333, 0.1333, 0.1333, 0.1333, 0.1333]
    assert [r["headcount"] for r in rows] == [2] * 7 + [3] * 7
    assert [r["late_count"] for r in rows] == [1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0]
    assert rows[5]["present_count"] == 1.0 and rows[5]["is_working_day"] is False
