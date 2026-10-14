# Attendance API — production submission

FastAPI + MongoDB service implementing `openapi.yaml` (rules R1–R10).
All code lives in `app/main.py` — run it with:

```bash
uvicorn app.main:app --port 8000
```

## Layout (single file, sectioned for readability)

- `1 config` — `MONGO_URI`/`MONGO_DB` (real env wins over `.env`), IST/UTC, validation regexes
- `2 helpers` — rounding (HALF_UP), ms↔datetime, strict validators
- `3 rules` — R1 attendance date, R2 late, R3 overtime, R4 work-hours, R5 half-day
- `4 serializers/models` — Mongo → API shapes (epoch-ms instants, no `_id`), request bodies
- `5 indexes/startup` — idempotent indexes via `lifespan` + `startup` event
- `6 endpoints` — health / employees / attendance / analytics / admin explain

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# set MONGO_URI / MONGO_DB (local: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

MongoDB 6.0+ required (grader uses 7.0). `run_tests.py` passes **33/33** (mongomock, no DB needed).

## Notes

- Instants are epoch-ms at the API boundary, BSON dates in Mongo; naive datetimes treated as UTC.
- Derived fields (`work_hours`, `late_minutes`, `overtime_minutes`, `half_day`) are server-computed, never client-supplied.
- Analytics read stored derived values and return both canonical `items` and legacy aliases (`departments`/`leaderboard`/`trend`).
- Submit files: `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, this `README.md`.

