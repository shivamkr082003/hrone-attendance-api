"""Every analytics endpoint against the plain-Python reference (tests/reference.py) on a generated dataset with
ties, weekend records, employees without logs, mid-month and future joiners, overnight shifts and orphan logs."""
import json

import pytest

from app import main
from tests import datagen, reference

MONTHS = ["2025-11", "2026-02", "2026-07", "2026-10", "2027-01"]


@pytest.fixture(scope="module")
def data(client):
    emps, logs = datagen.build(seed=11, employee_count=140)
    main.employees.insert_many([dict(e) for e in emps])
    main.attendance.insert_many([dict(l) for l in logs])
    return emps, logs


def departments(emps):
    return sorted({e["department"] for e in emps})


@pytest.mark.parametrize("month", MONTHS)
def test_employee_monthly(client, data, month):
    emps, logs = data
    for emp in emps[::3]:
        got = client.get(f"/analytics/employees/{emp['emp_code']}/monthly?month={month}").json()
        assert got == reference.monthly(emp, month, logs)


@pytest.mark.parametrize("month", MONTHS)
def test_department_summary(client, data, month):
    emps, logs = data
    assert client.get(f"/analytics/departments/summary?month={month}").json() == reference.summary(month, emps, logs)
    for dept in departments(emps):
        got = client.get(f"/analytics/departments/summary?month={month}&department={dept}").json()
        assert got == reference.summary(month, emps, logs, dept)


@pytest.mark.parametrize("month", MONTHS)
def test_late_leaderboard(client, data, month):
    emps, logs = data
    for limit in (1, 2, 5, 10, 50):
        got = client.get(f"/analytics/leaderboard/late?month={month}&limit={limit}").json()
        assert got == reference.leaderboard(month, emps, logs, limit)
    for dept in departments(emps):
        got = client.get(f"/analytics/leaderboard/late?month={month}&department={dept}&limit=3").json()
        assert got == reference.leaderboard(month, emps, logs, 3, dept)


def test_leaderboard_has_ties_and_ignores_unknown_employees(client, data):
    items = client.get("/analytics/leaderboard/late?month=2026-07&limit=50").json()["items"]
    ranks = [row["rank"] for row in items]
    assert len(ranks) != len(set(ranks)), "dataset should contain ties"
    assert all(row["emp_code"] != "EMP9999" for row in items)


@pytest.mark.parametrize("start, end", [("2026-06-15", "2026-09-14"), ("2025-11-01", "2025-11-30"),
                                        ("2025-10-01", "2025-11-05"), ("2026-10-31", "2026-10-31")])
def test_department_trend(client, data, start, end):
    emps, logs = data
    for dept in departments(emps):
        got = client.get(f"/analytics/departments/{dept}/trend?from={start}&to={end}").json()
        assert got == reference.trend(dept, start, end, emps, logs)


def test_analytics_validation(client, data):
    assert client.get("/analytics/employees/EMP9999/monthly?month=2026-07").status_code == 404
    assert client.get("/analytics/employees/EMP0001/monthly?month=2026-13").status_code == 422
    assert client.get("/analytics/departments/summary").status_code == 422
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=0").status_code == 422
    assert client.get("/analytics/departments/Nobody/trend?from=2026-07-01&to=2026-07-02").status_code == 404
    assert client.get("/analytics/departments/Sales/trend?from=2026-07-02&to=2026-07-01").status_code == 422
    assert client.get("/analytics/departments/Sales/trend?from=2026-07-01&to=2026-10-01").status_code == 422
    assert client.get("/analytics/departments/Sales/trend?from=2026-07-01&to=2026-09-30").status_code == 200


@pytest.mark.parametrize("endpoint, params", [
    ("attendance_list", ""), ("attendance_list", "emp_code=EMP0010&date_from=2026-07-01&date_to=2026-07-31"),
    ("attendance_list", "status=LEAVE"), ("employee_monthly", "emp_code=EMP0010&month=2026-07"),
    ("department_summary", "month=2026-07"), ("department_summary", "month=2026-07&department=Sales"),
    ("late_leaderboard", "month=2026-07&limit=5"), ("department_trend", "department=Sales&from=2026-07-01&to=2026-07-31"),
])
def test_explain_uses_indexes(client, data, endpoint, params):
    response = client.get(f"/admin/explain/{endpoint}?{params}")
    assert response.status_code == 200
    body = response.json()
    plan = json.dumps(body["explain"])
    assert body["endpoint"] == endpoint and "IXSCAN" in plan and "COLLSCAN" not in plan


def test_explain_requires_parameters(client, data):
    assert client.get("/admin/explain/employee_monthly?month=2026-07").status_code == 422
    assert client.get("/admin/explain/department_trend?department=Sales").status_code == 422
    assert client.get("/admin/explain/unknown").status_code == 422
