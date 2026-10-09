# DECISIONS.md

1. **Indexes.**
   - `employees`: `{"emp_code": 1}` (unique) guarantees employee uniqueness; `{"department": 1, "joined_on": 1}` and `{"joined_on": 1}` back department summary and trend aggregations.
   - `attendance_logs`: `{"emp_code": 1, "date": 1}` (unique) prevents duplicate day records and powers lookups; `{"date": -1, "emp_code": 1}` powers paginated attendance listings; `{"emp_code": 1, "punch_in": -1}` enables indexed punch-out lookups; `{"date": 1, "late_minutes": 1}` optimizes leaderboard filtering.
   - *Rejected*: An isolated single-field index on `status` was rejected due to low cardinality; compound indexing with `date` is far more selective.

2. **Punch-in race.**
   Both requests compute the attendance date and issue `insert_one`. MongoDB's unique compound index on `(emp_code, date)` guarantees atomic insertion for exactly one request (201 Created). The concurrent request encounters a `DuplicateKeyError`, caught by the handler and returned as 409 Conflict.

3. **Ties.**
   The pipeline ranks via `$setWindowFields` using `$rank` (standard competition ranking: 1, 2, 2, 4). The `$match: {"rank": {"$lte": limit}}` stage filters after ranking. Tied employees at rank $\le$ limit are both included, sorted by `total_late_minutes` desc, then `emp_code` asc.

4. **Headcount.**
   The aggregation begins on `employees` with `joined_on <= end_of_period` and performs a `$lookup` on `attendance_logs` with `preserveNullAndEmptyArrays: true`. Employees without attendance logs remain present in the grouped set and are counted toward headcount.

5. **One thing you would change for 100x the data.**
   Pre-aggregate daily and monthly metrics incrementally using Change Streams or materialized rollups, avoiding repetitive aggregations over millions of raw log documents.
