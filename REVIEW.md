# REVIEW.md

Review of the starter `app/main.py` (helpers and endpoints). "Starter" line references are to function names, because the
file was rewritten. All of these are fixed in the current `app/main.py`.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | No `tz_aware=True`, so PyMongo returns **naive** datetimes. Every later subtraction or comparison silently assumes a time zone. | Punch-in 04:35 UTC read back as `04:35` with no zone; mixing it with an IST-aware value raises `TypeError`. | `tz_aware=True`; all instants are UTC-aware; conversion to and from epoch ms happens only in `to_ms` / `from_ms`. |
| 2 | `compute_late_minutes` | Builds the shift start by `replace(hour=h, minute=m)` on the punch-in's own (UTC/naive) clock, not IST. | Shift 09:30, punch 10:05 IST (stored as 04:35Z): start becomes 09:30Z, the delta is negative, result 0 instead of 35. | Shift start is built in IST for the record's attendance date (`shift_window`). |
| 3 | `compute_late_minutes` | Floors to whole minutes **before** testing the grace (`minutes > 10`). R2 says strictly more than 10:00. | 09:40:01 -> floor = 10 -> `10 > 10` false -> 0; expected 10. | Compare whole seconds (`delta > 600`), then floor. |
| 4 | `compute_late_minutes` | Ignores overnight shifts and sub-second truncation (R1). | 22:00-06:00 shift, punch-in 02:00 next day belongs to the previous day's shift; 09:40:00.900 must count as 09:40:00. | `attendance_date` applies the overnight rule; `from_ms` truncates to whole seconds. |
| 5 | `compute_work_hours` | `round()` is float / banker's rounding, not half-up (R4). | 450 s = 0.125 h -> `round(0.125, 2)` = 0.12; expected 0.13. | Exact `Decimal` arithmetic with `ROUND_HALF_UP`. |
| 6 | `compute_overtime` | `end` is IST-aware but `punch_out` from PyMongo is naive: `TypeError` (a 500) on first use. | Any punch-out. | Everything aware; helper reworked. |
| 7 | `compute_overtime` | No 30-minute minimum (R3); end is always on the record's own date, so overnight shifts give negative -> 0. | Out at 18:40 on a 18:30 shift returns 10, expected 0. 22:00-06:00 shift out at 06:40 returns 0, expected 40. | Threshold of 30 min; `shift_window` moves an overnight end to the next day. |
| 8 | `create_employee` | Check-then-insert race and no unique index: two parallel creates both get 201 and a duplicate `emp_code` is stored. | 20 parallel POSTs of one code. | Unique index on `emp_code`, `insert_one`, `DuplicateKeyError` -> 409. |
| 9 | `EmployeeIn` / `create_employee` | No validation: any `emp_code`, `email`, `shift_start="99:99"`, equal start and end, `joined_on="banana"`; `created_at` is a naive `datetime.now()` returned as an ISO string, not epoch ms. | POST `{"emp_code":"x","shift_start":"99:99","joined_on":"banana"}` returns 201. | Pydantic patterns / lengths, real-date check, start != end (422). `created_at` is UTC and returned as epoch ms. |
| 10 | `list_employees` | `skip = page * page_size`: page 1 skips the first page. | GET `/employees?page=1` starts at the 21st employee. | `(page - 1) * page_size`. |
| 11 | `list_employees` | `total` counts the whole collection, ignoring `department`. | Filter on a small department; `total` is the company size. | `count_documents(filter)`. |
| 12 | `list_employees` | No sort, so pages are not stable; no bounds: `page_size=0` becomes `limit(0)` which means **no limit**, a negative value is a 500, a huge one is a memory risk. | `?page_size=0` returns everyone. | Sorted by `emp_code`; `page >= 1`, `1 <= page_size <= 100` (422 otherwise). |
| 13 | `punch_in` | Unknown employee: `emp` is `None`, then `emp["shift_start"]` -> `TypeError`, a 500. | Punch in for `EMP9999`. | 404. |
| 14 | `punch_in` | `punched_at` is parsed leniently and wrongly: `if body.punched_at` is false for 0; `fromtimestamp(ms/1000)` uses the server's local zone; pydantic accepts `"123"`, `1.5`, seconds-sized numbers. | `punched_at=1783312500` (seconds) is accepted and lands in 1970. | Strict int in `[1e11, 4.1e12]` (422 otherwise), UTC conversion, truncated to seconds. |
| 15 | `punch_in` | The date is the server-local date of the punch, not the IST date (R1), and no overnight rule. | 00:30 IST on the 7th (19:00Z on the 6th) is filed under the 6th or by server zone. | `attendance_date()` (IST, overnight aware). |
| 16 | `punch_in` | `status` is any string (`ABSENT`, `banana`). | POST with `"status":"ABSENT"` returns 201. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 17 | `punch_in` | Check-then-insert race: two parallel punch-ins both succeed. | 16 parallel identical requests give several 201s. | Unique index `(emp_code, date)`; exactly one insert wins, the rest hit `DuplicateKeyError` -> 409. |
| 18 | `punch_in` response | Leaks the internal `_id` as `id`; `punch_in` returned as a datetime string, not epoch ms. | Look at the 201 body. | `record_out()` builds the response; no ids; instants are epoch ms. |
| 19 | `list_attendance` | Loads every matching document into memory, sorts in Python, slices; `total = len(docs)`. | ~100k records: seconds per request and a lot of memory. | Server-side `find().sort().skip().limit()`, `count_documents`; indexes serve filter and sort. |
| 20 | `list_attendance` | `page=0` / negative gives negative slice bounds (wrong rows); `page_size` unbounded; dates not validated; `date_from > date_to` not rejected; sort has no `emp_code` tiebreak; `status` unvalidated; `id` leaked; legacy documents without `history` / `half_day` / `late_minutes` come back without them. | `?page=0`, `?date_from=garbage`, `?status=BOGUS`. | Page bounds, strict `YYYY-MM-DD` real-date validation, 422 on inverted ranges, sort `date desc, emp_code asc`, `Status` enum, `record_out()` applies defaults. |
| 21 | startup | No indexes at all. | `explain` shows `COLLSCAN`. | `ensure_indexes()` in the startup lifespan (idempotent, named indexes). |
| 22 | `/health` | Always returns ok; never pings MongoDB, and a database outage surfaces as 500s elsewhere. | Stop MongoDB; `/health` is still 200. | `ping`, 503 on failure; a global `PyMongoError` handler returns 503 instead of 500. |

## Looked at, decided it is not a defect

* `load_dotenv()` with its defaults does **not** override variables already in the environment, which is what the contract asks for.
* `IST = timezone(timedelta(hours=5, minutes=30))` is a correct fixed offset: India has no daylight saving time.
* `doc.pop("_id")` after `insert_one` is needed (PyMongo adds `_id` to the dict); the real problem was `doc["id"]` (#18).
* The default shift of 09:30-18:30 matches `openapi.yaml`.
* `HTTPException(409/404, "...")` produces the default `{"detail": ...}` body the contract asks for.
