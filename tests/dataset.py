"""A small, hand-checked dataset (July 2026) full of the awkward cases the rules describe."""
from datetime import datetime, timezone

UTC = timezone.utc


def emp(code, name, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": name, "email": f"{name.lower()}@x.io", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC)}


def log(code, day, status, wh=None, late=None, ot=None, half=None, history=True):
    doc = {"emp_code": code, "date": day, "status": status}
    if status in ("PRESENT", "WFH", "ON_DUTY"):
        doc["punch_in"] = datetime.fromisoformat(day + "T04:00:00+00:00")
        doc["punch_out"] = datetime.fromisoformat(day + "T13:00:00+00:00") if wh is not None else None
    else:
        doc["punch_in"] = doc["punch_out"] = None
    doc["work_hours"] = wh
    if late is not None:
        doc["late_minutes"] = late
    if ot is not None:
        doc["overtime_minutes"] = ot
    if half is not None:
        doc["half_day"] = half
    if history:
        doc["history"] = []
    return doc


EMPLOYEES = [
    emp("EMP0001", "Asha", "Engineering", "2026-01-05"),
    emp("EMP0002", "Vikram", "Engineering", "2026-07-13"),     # joins mid-month
    emp("EMP0003", "Nina", "Engineering", "2026-01-05"),       # no logs at all
    emp("EMP0004", "Rohit", "Sales", "2026-02-01"),
    emp("EMP0005", "Meera", "Sales", "2026-03-10", "22:00", "06:00"),
    emp("EMP0006", "Sam", "Sales", "2026-02-01"),
    emp("EMP0007", "Zed", "Ghost", "2026-08-01"),              # joins after July
]

LOGS = [
    log("EMP0001", "2026-07-06", "PRESENT", 9.12, 35, 40, False),
    log("EMP0001", "2026-07-07", "WFH", 3.5, half=True),
    log("EMP0001", "2026-07-08", "LEAVE"),
    log("EMP0001", "2026-07-09", "ON_DUTY", 9.0, 0, 0, False),
    log("EMP0001", "2026-07-11", "PRESENT", 8.0, 12, 35, False),   # Saturday
    # 2026-07-10 (Friday): deliberately no record
    log("EMP0001", "2026-07-13", "ABSENT"),
    log("EMP0002", "2026-07-14", "PRESENT", 8.98, 0, 0, history=False),   # legacy shape
    log("EMP0002", "2026-07-15", "PRESENT", None, 20),                     # still open
    log("EMP0004", "2026-07-06", "PRESENT", 8.0, 20, 0, False),
    log("EMP0005", "2026-07-06", "PRESENT", 8.0, 20, 0, False),
    log("EMP0006", "2026-07-06", "PRESENT", 8.0, 5, 0, False),
    log("EMP9999", "2026-07-06", "PRESENT", 8.0, 100, 0, False),           # employee does not exist
    log("EMP0001", "2026-06-30", "PRESENT", 8.0, 50, 0, False),            # previous month
]
