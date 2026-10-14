# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (the 409 guarantee and every lookup), `(department, joined_on)` for headcount, `(department, emp_code)` for the sorted list. `attendance_logs`: unique `(emp_code, date)` (one record per day, PATCH, monthly), `(date desc, emp_code, late_minutes)` (list order and the covered leaderboard scan), `(status, date, emp_code)`, `(emp_code, punch_in desc)` (punch-out lookup). Rejected: a standalone `late_minutes` index, because the leaderboard always filters by date first.
2. **Punch-in race.** Both requests build the same document and call `insert_one`. The unique `(emp_code, date)` index lets the database accept one; the other gets `DuplicateKeyError` and returns 409. There is no read before the write.
3. **Ties.** I rank everyone first with `$rank` (1, 2, 2, 4), then keep rows with `rank <= limit`. A tie at the cutoff returns all tied people, so the list can be longer than `limit`. Rows sort by minutes, then `emp_code`.
4. **Headcount.** The pipeline starts from `employees` (joined on or before month end) and `$lookup`s each person's logs. `$unwind` keeps empty results, so people with no logs still count.
5. **At 100x.** I would stop aggregating raw logs per request and keep a small per-employee, per-day or per-month rollup updated at punch time.
