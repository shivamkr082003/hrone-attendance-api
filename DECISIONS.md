# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, duplicate creates → 409), `(department, emp_code)` (department filters and sorted list), `joined_on` (department summary without a department). `attendance_logs`: unique `(emp_code, date)` (one record per day, punch-in race, monthly and `$lookup` joins), `(date desc, emp_code)` (list order, month scans for the leaderboard), `(emp_code, punch_in desc)` (punch-out finds the latest open shift). I rejected `(status, date, emp_code)`: status has only five values, the date index already filters well, and every punch would pay for one more index.

2. **Punch-in race.** Both requests validate, load the employee and compute the same `(emp_code, date)`. Both call `insert_one` without reading first. The unique index lets exactly one insert succeed; the other gets `DuplicateKeyError`, which I turn into 409.

3. **Ties.** `$setWindowFields` with `$rank` gives 1, 2, 2, 4. I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even if that is more than `limit` rows, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on <= month end`), not from logs. `$lookup` gives an empty array to someone with no logs, but they still count once.

5. **At 100x.** Keep a daily per-department rollup updated on every punch and correction, so the trend and summary read tens of rows instead of scanning millions of logs.
