"""Analytics + explain tests. They need a real MongoDB (aggregation features), e.g. `docker run -p 27017:27017 mongo:7`."""
import json
import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import main as m
from . import oracle as o
from .dataset import EMPLOYEES, LOGS

pytestmark = pytest.mark.real_mongo


def load(emps, logs):
    m.db.employees.insert_many([dict(e) for e in emps])
    m.db.attendance_logs.insert_many([dict(l) for l in logs])


@pytest.fixture()
def seeded(api):
    load(EMPLOYEES, LOGS)
    return api


# --- employee monthly --------------------------------------------------------- #
@pytest.mark.parametrize("code", ["EMP0001", "EMP0002", "EMP0003", "EMP0007", "EMP0004"])
def test_employee_monthly_matches_oracle(seeded, code):
    got = seeded.get(f"/analytics/employees/{code}/monthly", params={"month": "2026-07"}).json()
    assert got == o.employee_monthly(EMPLOYEES, LOGS, code, "2026-07")


def test_employee_monthly_by_hand(seeded):
    got = seeded.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert got["working_days"] == 23 and got["present_days"] == 2.5 and got["attendance_pct"] == 10.87
    assert got["total_late_minutes"] == 47 and got["total_overtime_minutes"] == 75 and got["leave_days"] == 1


def test_employee_monthly_errors(seeded):
    assert seeded.get("/analytics/employees/EMP4040/monthly", params={"month": "2026-07"}).status_code == 404
    for bad in ("2026-13", "2026-7", "26-07", "2026-00", "0000-01", "abcd-ef"):
        assert seeded.get("/analytics/employees/EMP0001/monthly", params={"month": bad}).status_code == 422, bad
    assert seeded.get("/analytics/employees/EMP0001/monthly").status_code == 422


# --- department summary ------------------------------------------------------- #
def test_department_summary_matches_oracle_and_hand(seeded):
    got = seeded.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert got["month"] == "2026-07"
    assert got["items"] == o.department_summary(EMPLOYEES, LOGS, "2026-07")
    eng = got["items"][0]
    assert eng["headcount"] == 3 and eng["present_days"] == 4.5 and eng["avg_work_hours"] == 7.72
    assert [i["department"] for i in got["items"]] == ["Engineering", "Sales"]   # Ghost omitted
    only = seeded.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()
    assert [i["department"] for i in only["items"]] == ["Sales"]
    assert seeded.get("/analytics/departments/summary", params={"month": "2026-07", "department": "sales"}).json()["items"] == []
    # August: the Ghost employee has joined, with no logs at all
    aug = {i["department"]: i for i in seeded.get("/analytics/departments/summary", params={"month": "2026-08"}).json()["items"]}
    assert aug["Ghost"]["headcount"] == 1 and aug["Ghost"]["avg_work_hours"] is None and aug["Ghost"]["present_days"] == 0


# --- leaderboard ----------------------------------------------------------------- #
def test_leaderboard_ties_and_limit_after_ranking(seeded):
    rows = seeded.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP0001"), (2, "EMP0002"), (2, "EMP0004"), (2, "EMP0005")]
    assert set(rows[0]) == {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"}
    rows = seeded.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"]
    assert [r["rank"] for r in rows] == [1, 2, 2, 2, 5]                      # 1, 2, 2, 2, 5 - not array positions
    assert "EMP9999" not in [r["emp_code"] for r in rows]                    # unknown employee ignored
    sales = seeded.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Sales", "limit": 1}).json()
    assert [(r["rank"], r["emp_code"]) for r in sales["items"]] == [(1, "EMP0004"), (1, "EMP0005")]
    assert seeded.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1}).json()["items"][0]["total_late_minutes"] == 47
    for bad in ({"limit": 0}, {"limit": 51}, {"month": "bad"}):
        assert seeded.get("/analytics/leaderboard/late", params={"month": "2026-07", **bad}).status_code == 422
    assert seeded.get("/analytics/leaderboard/late", params={"month": "2020-01"}).json()["items"] == []


# --- trend ------------------------------------------------------------------------ #
def test_trend_by_hand_and_oracle(seeded):
    p = {"from": "2026-07-06", "to": "2026-07-19"}
    got = seeded.get("/analytics/departments/Engineering/trend", params=p).json()
    assert got["department"] == "Engineering"
    assert got["items"] == o.department_trend(EMPLOYEES, LOGS, "Engineering", p["from"], p["to"])
    assert len(got["items"]) == 14
    assert [r["moving_avg_7d"] for r in got["items"]][:3] == [0.5, 0.375, 0.25]       # short windows at the start
    assert got["items"][5]["attendance_rate"] is None and got["items"][5]["present_count"] == 1.0


def test_trend_range_with_no_logs_still_has_every_day(seeded):
    # Sales has 3 people and nobody punched in that week: rate is 0.0 (not null) on working days
    got = seeded.get("/analytics/departments/Sales/trend", params={"from": "2026-07-20", "to": "2026-07-26"}).json()["items"]
    assert len(got) == 7 and all(r["headcount"] == 3 for r in got)
    assert [r["attendance_rate"] for r in got] == [0.0] * 5 + [None, None]
    assert [r["moving_avg_7d"] for r in got] == [0.0] * 7
    # Before anyone joined: headcount 0, so every rate and every moving average is null
    got = seeded.get("/analytics/departments/Engineering/trend", params={"from": "2025-03-03", "to": "2025-03-09"}).json()["items"]
    assert len(got) == 7 and all(r["headcount"] == 0 for r in got)
    assert all(r["attendance_rate"] is None and r["moving_avg_7d"] is None for r in got)
    assert [r["is_working_day"] for r in got] == [True] * 5 + [False] * 2


def test_trend_errors(seeded):
    ok = {"from": "2026-07-01", "to": "2026-07-10"}
    assert seeded.get("/analytics/departments/Nope/trend", params=ok).status_code == 404
    assert seeded.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-10", "to": "2026-07-01"}).status_code == 422
    assert seeded.get("/analytics/departments/Engineering/trend", params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422   # 93 days
    assert seeded.get("/analytics/departments/Engineering/trend", params={"from": "2026-01-01", "to": "2026-04-02"}).status_code == 200   # 92 days
    assert seeded.get("/analytics/departments/Engineering/trend", params={"from": "x", "to": "2026-04-02"}).status_code == 422
    assert seeded.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01"}).status_code == 422
    one = seeded.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-06"}).json()["items"]
    assert len(one) == 1 and one[0]["moving_avg_7d"] == 0.5


# --- randomized comparison against the oracle ----------------------------------------- #
def random_world(seed):
    rnd = random.Random(seed)
    depts = ["Eng", "Ops", "HR"]
    emps = [{**EMPLOYEES[0], "emp_code": f"EMP{i:04d}", "name": f"N{i}", "department": rnd.choice(depts),
             "joined_on": (datetime(2026, 5, 20) + timedelta(days=rnd.randint(0, 80))).date().isoformat()}
            for i in range(1, 26)]
    logs, seen = [], set()
    for _ in range(500):
        e = rnd.choice(emps)
        day = (datetime(2026, 6, 20) + timedelta(days=rnd.randint(0, 60))).date().isoformat()
        if (e["emp_code"], day) in seen:
            continue
        seen.add((e["emp_code"], day))
        status = rnd.choice(["PRESENT"] * 5 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE"])
        doc = {"emp_code": e["emp_code"], "date": day, "status": status, "punch_in": None, "punch_out": None,
               "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
        if status in o.PRESENT:
            wh = round(rnd.uniform(1, 11), 2) if rnd.random() < 0.9 else None
            doc.update(work_hours=wh, half_day=bool(wh is not None and wh < 4.5),
                       late_minutes=rnd.choice([0, 0, 0, 11, 25, 25, 40]), overtime_minutes=rnd.choice([0, 0, 30, 45]))
        logs.append(doc)
    logs.append({**logs[0], "emp_code": "EMP9998", "late_minutes": 77})
    return emps, logs


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_worlds_match_oracle(api, seed):
    emps, logs = random_world(seed)
    load(emps, logs)
    for month in ("2026-06", "2026-07", "2026-08"):
        assert api.get("/analytics/departments/summary", params={"month": month}).json()["items"] == o.department_summary(emps, logs, month)
        for limit in (1, 3, 50):
            for dept in (None, "Eng"):
                params = {"month": month, "limit": limit, **({"department": dept} if dept else {})}
                assert api.get("/analytics/leaderboard/late", params=params).json()["items"] == \
                    o.late_leaderboard(emps, logs, month, limit, dept), (month, limit, dept)
        for e in emps[:8]:
            assert api.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": month}).json() == \
                o.employee_monthly(emps, logs, e["emp_code"], month)
    for dept in ("Eng", "Ops", "HR"):
        p = {"from": "2026-06-25", "to": "2026-08-30"}
        assert api.get(f"/analytics/departments/{dept}/trend", params=p).json()["items"] == \
            o.department_trend(emps, logs, dept, p["from"], p["to"])


# --- explain -------------------------------------------------------------------------- #
EXPLAIN_CALLS = {
    "attendance_list": {},
    "attendance_list#2": {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31"},
    "attendance_list#3": {"status": "LEAVE", "page": 2},
    "attendance_list#4": {"date_from": "2026-07-01", "date_to": "2026-07-31"},
    "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"},
    "department_summary": {"month": "2026-07"},
    "department_summary#2": {"month": "2026-07", "department": "Engineering"},
    "late_leaderboard": {"month": "2026-07", "limit": 5},
    "late_leaderboard#2": {"month": "2026-07", "department": "Sales"},
    "department_trend": {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"},
}


def assert_indexed(body):
    text = json.dumps(body["explain"])
    assert "IXSCAN" in text, text[:500]
    assert "COLLSCAN" not in text, text[:500]


@pytest.mark.parametrize("key", list(EXPLAIN_CALLS))
def test_explain_uses_indexes(seeded, key):
    r = seeded.get(f"/admin/explain/{key.split('#')[0]}", params=EXPLAIN_CALLS[key])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"endpoint", "collection", "explain"} and isinstance(body["explain"], dict)
    assert_indexed(body)


def test_explain_missing_params_422(seeded):
    for ep, p in [("employee_monthly", {"month": "2026-07"}), ("employee_monthly", {"emp_code": "EMP0001"}),
                  ("department_summary", {}), ("late_leaderboard", {}), ("department_trend", {"department": "Engineering"}),
                  ("nonsense", {})]:
        assert seeded.get(f"/admin/explain/{ep}", params=p).status_code == 422, (ep, p)


def test_everything_is_fast_and_indexed_at_100k(api):
    """~100k attendance records: no collection scans and sane response times."""
    rnd = random.Random(7)
    emps = [{**EMPLOYEES[0], "emp_code": f"EMP{i:04d}", "name": f"N{i}", "department": f"D{i % 8}", "joined_on": "2025-01-06"}
            for i in range(1, 501)]
    m.db.employees.insert_many(emps)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    docs = []
    for e in emps:
        for k in range(200):
            day = (base + timedelta(days=k)).date().isoformat()
            late = rnd.choice([0, 0, 0, 15, 30])
            docs.append({"emp_code": e["emp_code"], "date": day, "status": rnd.choice(["PRESENT"] * 8 + ["WFH", "LEAVE"]),
                         "punch_in": base + timedelta(days=k, hours=4), "punch_out": base + timedelta(days=k, hours=13),
                         "work_hours": 9.0, "late_minutes": late, "overtime_minutes": 0, "half_day": False, "history": []})
    m.db.attendance_logs.insert_many(docs)
    assert len(docs) == 100_000
    calls = [
        ("/attendance", {"page": 3}), ("/attendance", {"emp_code": "EMP0042", "date_from": "2026-02-01"}),
        ("/attendance", {"status": "LEAVE", "page_size": 100}),
        ("/analytics/employees/EMP0042/monthly", {"month": "2026-03"}),
        ("/analytics/departments/summary", {"month": "2026-03"}),
        ("/analytics/leaderboard/late", {"month": "2026-03"}),
        ("/analytics/departments/D3/trend", {"from": "2026-02-01", "to": "2026-04-30"}),
    ]
    for path, params in calls:
        t0 = time.time()
        r = api.get(path, params=params)
        assert r.status_code == 200, (path, r.text)
        assert time.time() - t0 < 5, (path, time.time() - t0)
    for key, params in EXPLAIN_CALLS.items():
        if key.startswith("department_trend"):
            params = {"department": "D3", "from": "2026-02-01", "to": "2026-04-30"}
        if key.startswith("employee_monthly"):
            params = {"emp_code": "EMP0042", "month": "2026-03"}
        assert_indexed(api.get(f"/admin/explain/{key.split('#')[0]}", params=params).json())
