# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in / punch-out, manual corrections (regularization) and attendance analytics.
All application code is in [`app/main.py`](app/main.py).

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # MONGO_URI / MONGO_DB; real environment variables take priority over .env
docker run -d -p 27017:27017 mongo:7      # or use an Atlas M0 URI
uvicorn app.main:app --port 8000
```

MongoDB 6.0+ is required (`$setWindowFields`, `$lookup` with a pipeline). Indexes are created at startup, idempotently.
Interactive docs: <http://localhost:8000/docs>.

## Test

```bash
pip install -r requirements-dev.txt
python -m pytest
```

With MongoDB reachable on `MONGO_URI` everything runs against it in a throw-away database, including the analytics
tests (compared against an independent Python oracle and hand-computed numbers) and an `explain` / timing check on
100,000 records. Without MongoDB the tests fall back to mongomock, which covers CRUD and the business rules only;
the analytics tests are skipped. GitHub Actions (`.github/workflows/ci.yml`) runs the full suite on `mongo:7`.

## Files

* `REVIEW.md` - defects found in the starter and how each was fixed (Part A).
* `DECISIONS.md` - indexes, race handling, ties, headcount, scaling (Part C).

## Interpretation notes

Places where the written contract left room, and what I chose:

* **Trend range limit:** 92 days is counted inclusively (92 rows is accepted, 93 is a 422).
* **`moving_avg_7d`** averages the reported (4-decimal, half-up) `attendance_rate` values of the window.
* **Analytics** read the stored derived fields and do not recompute them from punch times. Records dated before an
  employee's `joined_on` are not excluded.
* **PATCH concurrency:** optimistic - the update only applies if the record is unchanged since it was read; the loser
  gets 409 (no automatic retry), so history entries are never lost.
* A PATCH is "a request that changes nothing" (422) when status and both punch times already equal the stored ones.
