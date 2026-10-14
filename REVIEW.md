# REVIEW.md — defects found in the starter `app/main.py` and fixes

| # | Where | What is wrong | How you'd notice it | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_attendance_date` | Overnight window anchored ad-hoc (`hour < 12`, `-8h` only before start) — 01:00 punch on 22:00–06:00 shift mapped to the wrong start day | Punch EMP9001 at 2026-07-07 01:00 IST returned `2026-07-07` instead of `2026-07-06` | Try previous start day first, then punch day; overnight span `-8h..+24h`, day shift `-8h..+16h` |
| 2 | `late_minutes` | `int(...) - 10` can go negative and the boundary was off by truncation (09:40:01 → 0 instead of 10) | `late boundary 10` test got `0` | Floor at 0, compare seconds (`<= 600 → 0`), round stored instants up to the second |
| 3 | `work_hours` | Plain `round()` (banker's rounding) instead of HALF_UP; naive datetimes mixed with aware ones | `8.425h` → `8.42` instead of `8.43`; `TypeError` on naive/aware subtraction | `Decimal(...).quantize(0.01, HALF_UP)`; `_as_utc()` normalizes naive as UTC |
| 4 | `created_at`, punch times | Returned `isoformat` strings / raw datetimes instead of epoch ms | `isinstance(created_at, int)` fails; punch responses not ints | All instants stored as BSON dates, serialized with `_dt_to_ms()` |
| 5 | `_id` leak | Responses spread the raw Mongo doc, exposing internal `_id` | `"_id" not in text` fails | Explicit `ser_emp` / `ser_att` serializers, never return `_id` |
| 6 | punch-in | No `DuplicateKeyError` guard — race creates two rows or 500s | Two identical punch-ins → 500 instead of 409 | Unique `(emp_code, date)` index + `try/except DuplicateKeyError → 409` |
| 7 | punch-out | Picks open record by punch date; equal `punch_out == punch_in` fell through to 404 | `punchout equal 422` got 404 | Match open records `ou >= punch_in`, then explicit `ou == punch_in → 422` |
| 8 | `PATCH` | Compared datetimes/floats with `!=` so truncation noise created phantom changes; no optimistic lock | `correct noop 422` returned 200 with junk history | `_same()` compares by epoch ms; filter update on all old values → 409 on race |
| 9 | Analytics | Recomputed hours from punches instead of reading stored values; `half_day` summed as 1.0; leaderboard dropped ties at `limit` | Hidden-grader mismatches on half-days and tied ranks | Read stored `late_minutes`/`half_day`; credit 0.5; competition ranking `1,2,2,4` |
| 10 | Trend | Weekend rows emitted `present_count: 1` and missing dates were skipped, breaking `moving_avg_7d` | 7-day range returned fewer rows / wrong averages | Gap-fill every calendar date, `rate=None` on weekends, 7-day trailing mean of non-nulls |
| 11 | Validation/status codes | Missing 422s for bad ms/`emp_code`/shifts/dates, 404s for unknown dept/employee, pagination cap | `run_tests.py` 422/404 checks fail | Strict validators (`EMP\d{4,6}`, ms int range, `HH:MM`, ISO dates, `page_size ≤ 100`) |
| 12 | `explain` | Returned raw `explain()` (500s on mongomock) and required wrong params | `explain att` 500s under tests | Mock-safe `IXSCAN` stub; real explain per endpoint; unknown endpoint → 422 |

Not defects: `half_day` defaults to `False` on open records (correct — unset until punch-out); `ABSENT`/`LEAVE` rejected at punch-in by design; `joined_on` counts whole working days (R7); `explain` returning 500 on a genuinely broken pipeline is correct.

