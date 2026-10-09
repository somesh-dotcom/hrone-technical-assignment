# Employee Attendance & Analytics API

FastAPI and MongoDB backend service for employee attendance tracking, manual regularization with audit trails, and high-performance MongoDB aggregation pipelines for organizational analytics.

## Tech Stack
- **Python**: 3.11+
- **FastAPI**: Modern asynchronous web framework
- **MongoDB**: 6.0+ (Community or Atlas)
- **PyMongo**: Official MongoDB driver for Python

## Features & Implementation Summary
1. **Attendance Engine & Business Rules (R1 - R5)**:
   - Punch-in and punch-out with concurrency protection (atomic unique indexes and conditional updates).
   - Timezone-aware calculations in IST (UTC+05:30), overnight shift resolution across midnight, 10-minute grace period with floor calculation from shift start, overtime with minimum 30-minute threshold, and half-day detection (< 4.50 hrs) using half-up rounding.
2. **Audit Trail Regularization (Rule R1)**:
   - Natural composite key addressing `(emp_code, date)`.
   - Recomputes derived fields and atomically appends changes to `history` without lost updates under concurrency.
3. **Analytics via MongoDB Aggregations**:
   - `GET /analytics/employees/{emp_code}/monthly`: Monthly employee summary with mid-month joiner handling (Rule R7).
   - `GET /analytics/departments/summary`: Department-level summary across collections including headcount for zero-log employees (Rule R9).
   - `GET /analytics/leaderboard/late`: Competition ranking with window function (`$rank`) and tie handling.
   - `GET /analytics/departments/{department}/trend`: Daily trend with database-level gap filling (`$densify`) and 7-day moving window average (`$setWindowFields`).
4. **Performance & Query Optimization**:
   - `GET /admin/explain/{endpoint}`: Returns execution statistics proving all queries run via Index Scans (`IXSCAN`) with zero `COLLSCAN`.

## Getting Started

### Prerequisites
- Python 3.11+
- MongoDB 6.0+ running locally on port 27017 or via connection URI

### 1. Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Environment Variables
Configure environment variables (or rely on defaults):
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

### 3. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 4. Run the API Server
```bash
uvicorn app.main:app --port 8000
```
The application will create all required indexes idempotently during startup.

### 5. Running Tests
```bash
pip install pytest httpx
PYTHONPATH=. pytest -v
```

## Repository Structure
```
├── app/
│   ├── __init__.py
│   └── main.py          # Complete FastAPI application with all endpoints and helpers
├── requirements.txt     # Dependencies
├── REVIEW.md            # Part A defect review and resolution log
├── DECISIONS.md         # Part C architectural & design decisions
├── README.md            # Setup and execution guide
├── sample_data/         # Reference sample documents
└── sample_seed.py       # Seed script
```
