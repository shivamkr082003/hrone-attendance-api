# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in / punch-out, manual corrections with an audit trail, and attendance
analytics built on MongoDB aggregation pipelines. All application code is in `app/main.py`.

## Run

Requires Python 3.11+ and MongoDB 6.0+ (tested on 7.0 and 8.3).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or copy .env.example to .env
uvicorn app.main:app --port 8000
```

- `MONGO_URI` and `MONGO_DB` are required. A local `.env` is read, but real environment variables take priority.
- Indexes are created at startup (idempotently). If MongoDB is not reachable at startup, `/health` returns 503 and
  creates them once it is.
- API docs: http://localhost:8000/docs

## Tests

```bash
pip install -r requirements-dev.txt
MONGO_URI=mongodb://localhost:27017 pytest -q     # uses (and empties) the database "attendance_pytest"
```

- `tests/test_api.py`: rules R1 - R5, validation, pagination, and concurrency (parallel punch-ins, punch-outs,
  employee creates and corrections).
- `tests/test_analytics.py`: compares every analytics endpoint with a plain-Python reference
  (`tests/reference.py`) on a generated dataset (`tests/datagen.py`). The dataset has ties, weekend records,
  employees with no logs, mid-month and future joiners, overnight shifts, legacy records and orphan logs. It also
  checks that each explain plan uses `IXSCAN` and never `COLLSCAN`.
- `python -m tests.datagen` loads about 100k records into `MONGO_DB` for performance checks. On that dataset every
  endpoint answers in under 50 ms locally.

## Notes and interpretations

- **Analytics.** Pipelines do all aggregation in MongoDB. Python only does calendar arithmetic (working days) and
  the final half-up rounding with `Decimal`. MongoDB's `$round` rounds half to even, so the trend rounds half-up
  in the pipeline on `Decimal128` values instead.
- **`moving_avg_7d`.** It is the mean of the reported (already rounded) daily `attendance_rate` values.
- **Trend range.** The range is limited to 92 calendar days *inclusive* (for example, 1 July - 30 September is
  allowed; 1 July - 1 October is 422).
- **PATCH.** A request is "a change" when it changes `status`, `punch_in` or `punch_out`. Explicit `null` for
  these fields is 422 (omit them instead). A concurrent correction that loses the race gets 409, and its history
  entry is never silently dropped.
- **Punch-out.** It closes the latest record with `punch_in <= punched_at`. If that record is already closed, the
  answer is 409.
- **`GET /attendance` total.** Without filters, `total` comes from `estimated_document_count` (collection
  metadata) to avoid counting 100k documents on every request.
