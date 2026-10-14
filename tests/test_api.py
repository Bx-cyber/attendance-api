"""HTTP-level tests: CRUD, validation, rules and concurrency. Run on mongomock or real MongoDB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app import main as m

IST = m.IST


def ms(y, mo, d, h, mi, s=0):
    return m.to_ms(datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc))


def mk_emp(api, code="EMP0001", **kw):
    body = {"emp_code": code, "name": "Asha", "email": "a@x.io", "department": "Eng", "joined_on": "2026-01-05", **kw}
    r = api.post("/employees", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --- employees ----------------------------------------------------------- #
def test_create_employee_shape_and_defaults(api):
    e = mk_emp(api)
    assert set(e) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert (e["shift_start"], e["shift_end"]) == ("09:30", "18:30")
    assert isinstance(e["created_at"], int) and e["created_at"] > 1e12


def test_duplicate_employee_is_409(api):
    mk_emp(api)
    assert api.post("/employees", json={"emp_code": "EMP0001", "name": "B", "email": "b@x.io",
                                        "department": "Eng", "joined_on": "2026-01-05"}).status_code == 409


def test_employee_validation(api):
    base = {"emp_code": "EMP0001", "name": "A", "email": "a@x.io", "department": "Eng", "joined_on": "2026-01-05"}
    bad = [
        {**base, "emp_code": "E1"}, {**base, "emp_code": "emp0001"}, {**base, "emp_code": "EMP12"},
        {**base, "email": "nope"}, {**base, "name": ""}, {**base, "joined_on": "2026-02-30"},
        {**base, "joined_on": "20260105"}, {**base, "shift_start": "24:00"}, {**base, "shift_start": "9:30"},
        {**base, "shift_start": "09:30", "shift_end": "09:30"}, {**base, "department": "x" * 51},
    ]
    for b in bad:
        assert api.post("/employees", json=b).status_code == 422, b


def test_employee_listing_pagination_filter_sort(api):
    for i, dept in enumerate(["B", "A", "A", "B", "A"], start=1):
        mk_emp(api, f"EMP{i:04d}", department=dept)
    r = api.get("/employees", params={"department": "A", "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0002", "EMP0003"]
    assert r["total"] == 3                       # filtered total, not whole collection
    r2 = api.get("/employees", params={"department": "A", "page_size": 2, "page": 2}).json()
    assert [e["emp_code"] for e in r2["items"]] == ["EMP0005"]
    all_ = api.get("/employees").json()
    assert [e["emp_code"] for e in all_["items"]] == [f"EMP{i:04d}" for i in range(1, 6)]
    for params in ({"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": -1}):
        assert api.get("/employees", params=params).status_code == 422
    assert api.get("/employees", params={"department": "a"}).json()["total"] == 0   # case-sensitive


# --- punch-in -------------------------------------------------------------- #
def test_punch_in_basic_and_late(api):
    mk_emp(api)
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5, 30)})
    assert r.status_code == 201
    j = r.json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 35 and j["status"] == "PRESENT"
    assert j["punch_out"] is None and j["work_hours"] is None and j["half_day"] is False and j["history"] == []
    assert "id" not in j and "_id" not in j


def test_punch_in_truncates_milliseconds(api):
    mk_emp(api)
    j = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 40) + 900}).json()
    assert j["punch_in"] == ms(2026, 7, 6, 9, 40) and j["late_minutes"] == 0


def test_punch_in_errors(api):
    mk_emp(api)
    ok = ms(2026, 7, 6, 9, 30)
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": ok}).status_code == 404
    for bad in (1783312500, 1783312500000.5, "1783312500000", 5000000000000, True, 0, -5):
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
        assert r.status_code == 422, bad
    for st in ("ABSENT", "LEAVE", "nope"):
        assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ok, "status": st}).status_code == 422
    assert api.post("/attendance/punch-in", json={}).status_code == 422
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ok}).status_code == 201
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ok + 60000}).status_code == 409


def test_punch_in_ignores_derived_fields_from_client(api):
    mk_emp(api)
    j = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30),
                                               "late_minutes": 99, "work_hours": 5, "half_day": True}).json()
    assert j["late_minutes"] == 0 and j["work_hours"] is None and j["half_day"] is False


def test_punch_in_uses_ist_date_not_utc(api):
    mk_emp(api)
    # 00:30 IST on the 7th is 19:00 UTC on the 6th
    j = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 0, 30)}).json()
    assert j["date"] == "2026-07-07"


def test_concurrent_punch_in_exactly_one_201(api):
    mk_emp(api)
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda _: api.post("/attendance/punch-in", json=body).status_code, range(16)))
    assert sorted(codes) == [201] + [409] * 15


def test_concurrent_employee_create_exactly_one_201(api):
    body = {"emp_code": "EMP0007", "name": "A", "email": "a@x.io", "department": "Eng", "joined_on": "2026-01-05"}
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda _: api.post("/employees", json=body).status_code, range(16)))
    assert sorted(codes) == [201] + [409] * 15


# --- punch-out -------------------------------------------------------------- #
def test_punch_out_computes_everything(api):
    mk_emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5)})
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 10)})
    assert r.status_code == 200
    j = r.json()
    assert (j["work_hours"], j["overtime_minutes"], j["late_minutes"], j["half_day"]) == (9.08, 40, 35, False)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 20, 0)}).status_code == 409


def test_punch_out_half_day_and_errors(api):
    mk_emp(api)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 0)}).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP4040", "punched_at": ms(2026, 7, 6, 18, 0)}).status_code == 404
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 31)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 12345}).status_code == 422
    j = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 13, 0)}).json()
    assert j["work_hours"] == 3.5 and j["half_day"] is True and j["overtime_minutes"] == 0


def test_punch_out_overnight_shift(api):
    mk_emp(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    j = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)}).json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 0
    j = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)}).json()
    assert (j["date"], j["work_hours"], j["overtime_minutes"]) == ("2026-07-06", 8.75, 40)


def test_overnight_early_morning_punch_in_belongs_to_previous_day(api):
    mk_emp(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    j = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 2, 0)}).json()
    assert j["date"] == "2026-07-06"


def test_concurrent_punch_out_exactly_one_200(api):
    mk_emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 30)}
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda _: api.post("/attendance/punch-out", json=body).status_code, range(16)))
    assert sorted(codes) == [200] + [409] * 15


# --- listing ------------------------------------------------------------------- #
def seed_logs(api):
    mk_emp(api, "EMP0001"); mk_emp(api, "EMP0002")
    for code in ("EMP0002", "EMP0001"):
        for day in (6, 7, 8):
            api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2026, 7, day, 9, 30),
                                                    "status": "WFH" if day == 7 else "PRESENT"})


def test_list_attendance_sort_filters_paging(api):
    seed_logs(api)
    r = api.get("/attendance").json()
    assert r["total"] == 6
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001"),
        ("2026-07-07", "EMP0002"), ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    r = api.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07", "page_size": 1, "page": 2}).json()
    assert r["total"] == 2 and [i["date"] for i in r["items"]] == ["2026-07-07"]
    assert api.get("/attendance", params={"status": "WFH"}).json()["total"] == 2
    assert api.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"}).json()["total"] == 2
    for bad in ({"date_from": "2026-07-09", "date_to": "2026-07-01"}, {"date_from": "07/01/2026"},
                {"date_to": "2026-13-01"}, {"status": "BOGUS"}, {"page": 0}, {"page_size": 101}):
        assert api.get("/attendance", params=bad).status_code == 422, bad


def test_legacy_record_defaults(api):
    mk_emp(api)
    m.db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
                                     "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
                                     "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=timezone.utc), "work_hours": 8.98})
    j = api.get("/attendance").json()["items"][0]
    assert j["half_day"] is False and j["history"] == [] and j["late_minutes"] == 0 and j["overtime_minutes"] == 0
    assert j["punch_in"] == m.to_ms(datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc))


# --- regularize ----------------------------------------------------------------- #
def patch(api, code, day, **body):
    body.setdefault("reason", "biometric glitch"); body.setdefault("regularized_by", "hr.admin")
    return api.patch(f"/attendance/{code}/{day}", json=body)


def test_regularize_recomputes_and_appends_history(api):
    mk_emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 30)})
    r = patch(api, "EMP0001", "2026-07-07", punch_in=ms(2026, 7, 7, 9, 28))
    assert r.status_code == 200
    j = r.json()
    assert (j["late_minutes"], j["work_hours"]) == (0, 9.03)
    assert len(j["history"]) == 1
    h = j["history"][0]
    assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
    assert h["changes"] == {"punch_in": {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)},
                            "late_minutes": {"from": 35, "to": 0}, "work_hours": {"from": 8.42, "to": 9.03}}
    # second correction appends, never overwrites
    j2 = patch(api, "EMP0001", "2026-07-07", status="WFH", reason="was remote").json()
    assert len(j2["history"]) == 2 and j2["history"][0] == h
    assert j2["history"][1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}
    # stored history keeps BSON datetimes for punch times
    stored = m.db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["history"][0]["changes"]["punch_in"]["from"], datetime)


def test_regularize_to_absent_clears_times(api):
    mk_emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    j = patch(api, "EMP0001", "2026-07-07", status="ABSENT").json()
    assert j["punch_in"] is None and j["punch_out"] is None and j["late_minutes"] == 0 and j["work_hours"] is None
    assert set(j["history"][0]["changes"]) == {"status", "punch_in", "late_minutes"}
    # back to presence requires a punch_in
    assert patch(api, "EMP0001", "2026-07-07", status="PRESENT").status_code == 422
    assert patch(api, "EMP0001", "2026-07-07", status="PRESENT", punch_in=ms(2026, 7, 7, 9, 30)).status_code == 200


def test_regularize_validation(api):
    mk_emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30)})
    d = "2026-07-07"
    assert patch(api, "EMP0001", d).status_code == 422                                           # changes nothing
    assert patch(api, "EMP0001", d, status="PRESENT").status_code == 422                         # same status
    assert patch(api, "EMP0001", d, status="LEAVE", punch_in=ms(2026, 7, 7, 9, 30)).status_code == 422
    assert patch(api, "EMP0001", d, punch_in=ms(2026, 7, 8, 9, 30)).status_code == 422           # wrong day
    assert patch(api, "EMP0001", d, punch_out=ms(2026, 7, 7, 9, 0)).status_code == 422           # before in
    assert patch(api, "EMP0001", d, punch_out=ms(2026, 7, 8, 9, 31)).status_code == 422          # > 24h
    assert patch(api, "EMP0001", d, reason="abc").status_code == 422
    assert patch(api, "EMP0001", d, regularized_by="").status_code == 422
    assert patch(api, "EMP0001", d, late_minutes=5).status_code == 422                           # still changes nothing
    assert api.patch(f"/attendance/EMP0001/{d}", json={"status": "WFH"}).status_code == 422    # no reason/by
    assert patch(api, "EMP0001", d, punch_in=None, status="WFH").status_code == 422              # explicit null
    assert patch(api, "EMP0001", "2026-02-30", status="WFH").status_code == 422
    assert patch(api, "EMP0001", "2026-07-09", status="WFH").status_code == 404
    assert patch(api, "EMP4040", d, status="WFH").status_code == 404
    assert len(api.get("/attendance").json()["items"][0]["history"]) == 0                        # nothing leaked in


def test_regularize_derived_fields_in_body_are_ignored(api):
    mk_emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    j = patch(api, "EMP0001", "2026-07-07", punch_in=ms(2026, 7, 7, 9, 30), late_minutes=77, work_hours=1).json()
    assert j["late_minutes"] == 0 and j["work_hours"] is None


def test_concurrent_regularize_never_loses_history(api):
    mk_emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30)})
    statuses = ["WFH", "ON_DUTY", "PRESENT"] * 4
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(lambda s: patch(api, "EMP0001", "2026-07-07", status=s).status_code, statuses))
    rec = api.get("/attendance").json()["items"][0]
    assert set(codes) <= {200, 409, 422}
    assert len(rec["history"]) == codes.count(200) >= 1


# --- health / misc -------------------------------------------------------------- #
def test_health_and_error_body(api):
    assert api.get("/health").json() == {"status": "ok"}
    assert "detail" in api.get("/attendance", params={"page": 0}).json()


def test_indexes_created_idempotently(api):
    m.ensure_indexes(); m.ensure_indexes()
    names = {i["name"] for i in m.db.attendance_logs.list_indexes()}
    assert {m.ATT_BY_EMP_DATE, m.ATT_BY_DATE, m.ATT_BY_STATUS, m.ATT_BY_PUNCH_IN} <= names
    assert any(i.get("unique") for i in m.db.employees.list_indexes() if i["name"] == "emp_code_1")
