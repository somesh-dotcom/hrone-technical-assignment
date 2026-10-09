"""
Comprehensive test suite for Employee Attendance & Analytics API.
"""
import concurrent.futures
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app, db, IST

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    """Reset the database to the 9 sample documents before tests."""
    from sample_seed import here, opts, json_util
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_create_and_list_employees():
    # 1. Create employee
    payload = {
        "emp_code": "EMP9999",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert "created_at" in data
    assert "_id" not in data

    # 2. Duplicate create -> 409
    res_dup = client.post("/employees", json=payload)
    assert res_dup.status_code == 409

    # 3. Invalid shift (equal start and end) -> 422
    payload_bad = dict(payload, emp_code="EMP9998", shift_start="09:00", shift_end="09:00")
    res_bad = client.post("/employees", json=payload_bad)
    assert res_bad.status_code == 422

    # 4. List employees
    res_list = client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["total"] == 3  # EMP0001, EMP0002, EMP9999
    assert len(list_data["items"]) == 3
    assert list_data["items"][0]["emp_code"] == "EMP0001"


def test_punch_in_and_out_standard():
    # Punch in at 09:40:01 (10s past grace -> 10m late)
    dt_in = datetime(2026, 7, 20, 9, 40, 1, tzinfo=IST)
    ts_in = int(dt_in.timestamp() * 1000)

    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in, "status": "PRESENT"})
    assert res_in.status_code == 201
    data_in = res_in.json()
    assert data_in["emp_code"] == "EMP0001"
    assert data_in["date"] == "2026-07-20"
    assert data_in["late_minutes"] == 10
    assert data_in["punch_out"] is None
    assert data_in["work_hours"] is None

    # Duplicate punch in for same day -> 409
    res_in_dup = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
    assert res_in_dup.status_code == 409

    # Punch out at 19:15:00 (45m overtime -> 45)
    dt_out = datetime(2026, 7, 20, 19, 15, 0, tzinfo=IST)
    ts_out = int(dt_out.timestamp() * 1000)

    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
    assert res_out.status_code == 200
    data_out = res_out.json()
    assert data_out["overtime_minutes"] == 45
    assert data_out["half_day"] is False
    # (19:15:00 - 09:40:01) = 9h 34m 59s = 34499s / 3600 = 9.583055... -> 9.58
    assert data_out["work_hours"] == 9.58

    # Duplicate punch out -> 409
    res_out_dup = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})
    assert res_out_dup.status_code == 409


def test_punch_in_and_out_overnight():
    # EMP0005 has shift 22:00 - 06:00
    # Punch in on 2026-07-21 at 05:45 IST (belongs to 2026-07-20 shift per R1)
    dt_in = datetime(2026, 7, 21, 5, 45, 0, tzinfo=IST)
    ts_in = int(dt_in.timestamp() * 1000)

    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ts_in, "status": "PRESENT"})
    assert res_in.status_code == 201
    data_in = res_in.json()
    assert data_in["date"] == "2026-07-20"

    # Punch out at 06:40:00 IST on 2026-07-21 (40 min overtime)
    dt_out = datetime(2026, 7, 21, 6, 40, 0, tzinfo=IST)
    ts_out = int(dt_out.timestamp() * 1000)

    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ts_out})
    assert res_out.status_code == 200
    data_out = res_out.json()
    assert data_out["overtime_minutes"] == 40
    assert data_out["half_day"] is True  # worked 0.92 hrs < 4.5


def test_list_attendance():
    res = client.get("/attendance?date_from=2026-07-06&date_to=2026-07-08")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 6
    assert "items" in data

    # Validation error if date_from > date_to -> 422
    res_bad = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res_bad.status_code == 422


def test_regularize_attendance():
    # Regularize EMP0004 on 2026-07-08 (currently ABSENT) to PRESENT with punch times
    dt_in = datetime(2026, 7, 8, 9, 30, 0, tzinfo=IST)
    dt_out = datetime(2026, 7, 8, 18, 30, 0, tzinfo=IST)
    ts_in = int(dt_in.timestamp() * 1000)
    ts_out = int(dt_out.timestamp() * 1000)

    patch_payload = {
        "status": "PRESENT",
        "punch_in": ts_in,
        "punch_out": ts_out,
        "reason": "Forgot ID card at home",
        "regularized_by": "manager.john",
    }
    res = client.patch("/attendance/EMP0004/2026-07-08", json=patch_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "PRESENT"
    assert data["work_hours"] == 9.0
    assert data["late_minutes"] == 0
    assert len(data["history"]) == 1
    assert data["history"][0]["by"] == "manager.john"
    assert data["history"][0]["reason"] == "Forgot ID card at home"
    assert "status" in data["history"][0]["changes"]
    assert data["history"][0]["changes"]["status"]["from"] == "ABSENT"
    assert data["history"][0]["changes"]["status"]["to"] == "PRESENT"

    # Regularize again with nothing changed -> 422
    res_no_change = client.patch("/attendance/EMP0004/2026-07-08", json=patch_payload)
    assert res_no_change.status_code == 422


def test_analytics_employee_monthly():
    # EMP0001 in 2026-07 (joined 2026-01-05 -> full month)
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23  # July 2026 has 23 weekdays
    assert data["present_days"] == 2.0
    assert data["late_count"] == 0

    # EMP0002 joined mid-month on 2026-07-13
    res2 = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["working_days"] == 15  # Mon-Fri from July 13 to July 31 = 15 days


def test_analytics_department_summary():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = {item["department"]: item for item in data["items"]}
    assert "Engineering" in items
    assert items["Engineering"]["headcount"] == 2
    assert items["Engineering"]["present_days"] == 3.0
    assert items["Sales"]["headcount"] == 2
    assert items["Support"]["headcount"] == 2


def test_analytics_late_leaderboard():
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert len(data["items"]) >= 1
    top = data["items"][0]
    assert top["emp_code"] == "EMP0003"
    assert top["rank"] == 1
    assert top["total_late_minutes"] == 35


def test_analytics_department_trend():
    res = client.get("/analytics/departments/Sales/trend?from=2026-07-06&to=2026-07-12")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Sales"
    assert len(data["items"]) == 7  # Exactly 7 days
    d0 = data["items"][0]
    assert d0["date"] == "2026-07-06"
    assert d0["is_working_day"] is True
    assert d0["headcount"] == 2
    assert d0["present_count"] == 1.0
    assert d0["late_count"] == 1
    assert d0["attendance_rate"] == 0.5
    assert d0["moving_avg_7d"] == 0.5


def test_admin_explain():
    endpoints = [
        ("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31", "attendance_logs"),
        ("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07", "attendance_logs"),
        ("/admin/explain/department_summary?month=2026-07", "employees"),
        ("/admin/explain/late_leaderboard?month=2026-07&limit=10", "attendance_logs"),
        ("/admin/explain/department_trend?department=Sales&from=2026-07-06&to=2026-07-12", "employees"),
    ]
    for url, coll in endpoints:
        res = client.get(url)
        assert res.status_code == 200
        data = res.json()
        assert data["collection"] == coll
        assert "explain" in data
        explain_str = str(data["explain"])
        assert "IXSCAN" in explain_str
        assert "COLLSCAN" not in explain_str


def test_concurrency_punch_in():
    dt = datetime(2026, 7, 25, 9, 30, 0, tzinfo=IST)
    ts = int(dt.timestamp() * 1000)

    def do_punch():
        return client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch) for _ in range(5)]
        results = [f.result() for f in futures]

    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1
    assert statuses.count(409) == 4


def test_concurrency_punch_out():
    # Setup a punch-in
    dt_in = datetime(2026, 7, 26, 9, 30, 0, tzinfo=IST)
    ts_in = int(dt_in.timestamp() * 1000)
    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
    assert res_in.status_code == 201

    dt_out = datetime(2026, 7, 26, 18, 30, 0, tzinfo=IST)
    ts_out = int(dt_out.timestamp() * 1000)

    def do_punch_out():
        return client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(5)]
        results = [f.result() for f in futures]

    statuses = [r.status_code for r in results]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 4


def test_concurrency_regularize():
    # Test concurrent regularization on EMP0001 2026-07-06
    def do_reg(i):
        return client.patch(
            "/attendance/EMP0001/2026-07-06",
            json={
                "status": "WFH" if i % 2 == 0 else "ON_DUTY",
                "reason": f"Regularize reason from thread {i}",
                "regularized_by": f"admin_{i}",
            },
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(do_reg, i) for i in range(4)]
        results = [f.result() for f in futures]

    statuses = [r.status_code for r in results]
    assert 200 in statuses
    # At least one succeeds (200), and conflicting ones get 409 or sequential 200
    for s in statuses:
        assert s in (200, 409, 422)


def test_late_leaderboard_ties():
    # Seed 3 employees with same late minutes
    # EMP0001: 30 min late
    # EMP0002: 30 min late
    # EMP0003: 40 min late
    # EMP0004: 20 min late
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-08-01", "status": "PRESENT", "late_minutes": 30, "overtime_minutes": 0},
        {"emp_code": "EMP0002", "date": "2026-08-01", "status": "PRESENT", "late_minutes": 30, "overtime_minutes": 0},
        {"emp_code": "EMP0003", "date": "2026-08-01", "status": "PRESENT", "late_minutes": 40, "overtime_minutes": 0},
        {"emp_code": "EMP0004", "date": "2026-08-01", "status": "PRESENT", "late_minutes": 20, "overtime_minutes": 0},
    ])

    # With limit=2:
    # Rank 1: EMP0003 (40 min)
    # Rank 2: EMP0001 (30 min)
    # Rank 2: EMP0002 (30 min) -> tied rank 2 <= limit 2, so both included!
    # Rank 4: EMP0004 (20 min) -> rank 4 > 2, excluded!
    res = client.get("/analytics/leaderboard/late?month=2026-08&limit=2")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP0003"
    assert items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMP0001"
    assert items[1]["rank"] == 2
    assert items[2]["emp_code"] == "EMP0002"
    assert items[2]["rank"] == 2


def test_weekend_records_and_mid_month_joiners():
    # Weekend record (2026-07-04 is Saturday)
    db.attendance_logs.insert_one({
        "emp_code": "EMP0002",
        "date": "2026-07-04",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 4, 4, 30),
        "punch_out": datetime(2026, 7, 4, 14, 0),
        "work_hours": 9.5,
        "late_minutes": 30,
        "overtime_minutes": 60,
        "half_day": False,
    })

    res = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    # Joined on 2026-07-13 -> working_days = 15
    assert data["working_days"] == 15
    # Weekend record does NOT add to present_days (R7), but adds to late and overtime
    assert data["total_late_minutes"] >= 30
    assert data["total_overtime_minutes"] >= 60


def test_input_validation_boundary():
    # 1. Seconds-looking epoch (must be rejected with 422)
    res_sec = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500})
    assert res_sec.status_code == 422

    # 2. Punch-out > 24 hours after punch-in -> 422
    dt_in = datetime(2026, 7, 28, 9, 30, 0, tzinfo=IST)
    ts_in = int(dt_in.timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})

    dt_out_late = dt_in + timedelta(hours=25)
    ts_out_late = int(dt_out_late.timestamp() * 1000)
    res_out_late = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out_late})
    assert res_out_late.status_code == 422

    # 3. Punch-out <= punch-in -> 422
    res_out_before = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_in})
    assert res_out_before.status_code == 422

