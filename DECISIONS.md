# DECISIONS.md

1. **Indexes.** `uniq_emp_code` on `employees.emp_code` enforces the "unique, never changes" business key and serves every employee lookup plus the POST duplicate guard. `idx_emp_department` serves the `GET /employees?department=` filter and the summary headcount scan. `uniq_emp_date` on `attendance_logs(emp_code, date)` is the record identity: it makes the punch-in race a single atomic insert and speeds monthly reads. `idx_att_date` serves month-range scans, leaderboard grouping, and trend buckets. `idx_att_status` serves the attendance-list status filter and the `explain(attendance_list)` IXSCAN. I considered a compound `(date, status)` index and rejected it: status has only five values so selectivity is poor, and the single-field indexes already cover the queried shapes.

2. **Punch-in race.** Both requests compute the same IST attendance date, then both attempt `insert_one` on the same `(emp_code, date)` key. MongoDB serializes the writes on the unique index: the first insert wins, the second raises `DuplicateKeyError`, which the handler converts to `409`. No read-then-write check exists, so no interleaving can create duplicates.

3. **Ties.** The leaderboard uses competition ranking (`1,2,2,4`): rows are sorted by total late minutes desc, then `emp_code` asc, and equal totals share one rank while the next rank skips. Every row whose rank is `<= limit` is returned, so a tie at the cutoff returns all tied employees rather than cutting the tie arbitrarily.

4. **Headcount.** The summary iterates departments from the `employees` collection, not from logs, so a department with zero logs still emits a row with its full headcount. Present credit is summed per stored log (`1.0`, or `0.5` when `half_day` is set), and employees without logs simply contribute `0.0`.

5. **One thing you would change** if this had to serve 100x the data: pre-aggregate per `(department, date)` and per `(emp_code, month)` counters on every write, so the analytics endpoints become single-document reads instead of month-wide scans and group-bys.

