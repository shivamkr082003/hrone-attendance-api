"""Behaviour of the CRUD endpoints, business rules R1 - R5, validation and concurrency."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from app.main import IST


def ms(ist: str) -> int:
    """'2026-07-06 09:40:01' (IST) -> epoch milliseconds."""
    return int(datetime.fromisoformat(ist).replace(tzinfo=IST).timestamp() * 1000)


def employee(code, **overrides):
    return {"emp_code": code, "name": "Test Person", "email": "t@example.com", "department": "QA",
            "joined_on": "2026-01-05", **overrides}


@pytest.fixture(scope="module", autouse=True)
def people(client):
    assert client.post("/employees", json=employee("EMP0001")).status_code == 201
    assert client.post("/employees", json=employee("EMP0002")).status_code == 201
    assert client.post("/employees", json=employee("EMP0005", shift_start="22:00", shift_end="06:00")).status_code == 201


def punch_in(client, code, when, **extra):
    return client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(when), **extra})


def punch_out(client, code, when):
    return client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(when)})


def patch(client, code, day, **body):
    return client.patch(f"/attendance/{code}/{day}",
                        json={"reason": "biometric glitch", "regularized_by": "hr.admin", **body})


# ----------------------------------------------------------------------------- system and employees
def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_employee_response_shape_and_duplicate(client):
    response = client.post("/employees", json=employee("EMP0100"))
    body = response.json()
    assert response.status_code == 201 and isinstance(body["created_at"], int)
    assert set(body) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on",
                         "created_at"}
    assert (body["shift_start"], body["shift_end"]) == ("09:30", "18:30")
    assert client.post("/employees", json=employee("EMP0100")).status_code == 409


@pytest.mark.parametrize("overrides", [
    {"emp_code": "E1"}, {"email": "not-an-email"}, {"joined_on": "2026-02-30"}, {"joined_on": "20260105"},
    {"shift_start": "24:00"}, {"shift_start": "09:00", "shift_end": "09:00"}, {"department": ""},
])
def test_employee_validation(client, overrides):
    assert client.post("/employees", json=employee("EMP0200", **overrides)).status_code == 422


def test_concurrent_employee_creates(client):
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda _: client.post("/employees", json=employee("EMP0300")).status_code, range(8)))
    assert sorted(codes) == [201] + [409] * 7


def test_employee_pagination_and_filter(client):
    for i in range(5):
        client.post("/employees", json=employee(f"EMP05{i:02d}", department="Pager"))
    first = client.get("/employees?department=Pager&page=1&page_size=2").json()
    third = client.get("/employees?department=Pager&page=3&page_size=2").json()
    assert [e["emp_code"] for e in first["items"]] == ["EMP0500", "EMP0501"]
    assert [e["emp_code"] for e in third["items"]] == ["EMP0504"]
    assert first["total"] == 5
    for bad in ("page=0", "page_size=0", "page_size=101"):
        assert client.get(f"/employees?{bad}").status_code == 422


# ----------------------------------------------------------------------------- punch-in, R1 and R2
@pytest.mark.parametrize("when, late", [
    ("2026-08-03 09:40:00", 0), ("2026-08-04 09:40:01", 10), ("2026-08-05 10:15:59", 45),
    ("2026-08-06 09:40:00.900", 0), ("2026-08-07 09:00:00", 0),
])
def test_late_minutes(client, when, late):
    response = punch_in(client, "EMP0002", when)
    assert response.status_code == 201
    assert response.json()["late_minutes"] == late
    assert response.json()["punch_in"] % 1000 == 0  # truncated to whole seconds (R1)


def test_punch_in_record_shape(client):
    body = punch_in(client, "EMP0001", "2026-07-06 09:28:00", status="WFH").json()
    assert body == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "WFH", "punch_in": ms("2026-07-06 09:28:00"),
                    "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                    "half_day": False, "history": []}


def test_ist_date_not_utc_date(client):
    # 02:00 IST on 10 Aug is still 9 Aug in UTC; the attendance date is the IST date.
    assert punch_in(client, "EMP0001", "2026-08-10 02:00:00").json()["date"] == "2026-08-10"


def test_overnight_punch_in_belongs_to_previous_day(client):
    body = punch_in(client, "EMP0005", "2026-07-07 02:00:00").json()
    assert (body["date"], body["late_minutes"]) == ("2026-07-06", 240)


@pytest.mark.parametrize("punched_at", [1783570201, 1783570201000.0, "1783570201000", True, 4102444800001])
def test_bad_timestamps(client, punched_at):
    response = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punched_at})
    assert response.status_code == 422


def test_punch_in_errors(client):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    assert punch_in(client, "EMP0001", "2026-07-20 09:00:00", status="LEAVE").status_code == 422
    assert punch_in(client, "EMP0001", "2026-07-21 09:00:00").status_code == 201
    assert punch_in(client, "EMP0001", "2026-07-21 11:00:00").status_code == 409


def test_concurrent_punch_ins(client):
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: punch_in(client, "EMP0001", "2026-07-22 09:00:00").status_code, range(10)))
    assert sorted(codes) == [201] + [409] * 9


# ----------------------------------------------------------------------------- punch-out, R3 - R5
def test_punch_out_computes_derived_fields(client):
    punch_in(client, "EMP0002", "2026-07-06 10:05:00")
    body = punch_out(client, "EMP0002", "2026-07-06 19:10:00").json()
    assert (body["work_hours"], body["late_minutes"], body["overtime_minutes"], body["half_day"]) == (9.08, 35, 40, False)


@pytest.mark.parametrize("day, out, overtime", [("2026-09-01", "18:59:59", 29), ("2026-09-02", "19:00:00", 30)])
def test_overtime_threshold(client, day, out, overtime):
    punch_in(client, "EMP0002", f"{day} 09:30:00")
    expected = overtime if overtime >= 30 else 0
    assert punch_out(client, "EMP0002", f"{day} {out}").json()["overtime_minutes"] == expected


def test_work_hours_half_up_and_half_day(client):
    punch_in(client, "EMP0002", "2026-09-03 09:30:00")
    body = punch_out(client, "EMP0002", "2026-09-03 12:10:30").json()  # 9630 s = 2.675 h
    assert (body["work_hours"], body["half_day"]) == (2.68, True)
    punch_in(client, "EMP0002", "2026-09-04 09:30:00")
    body = punch_out(client, "EMP0002", "2026-09-04 13:59:42").json()  # 4.495 h rounds to 4.50 -> not a half day
    assert (body["work_hours"], body["half_day"]) == (4.5, False)


def test_overnight_punch_out(client):
    punch_in(client, "EMP0005", "2026-07-08 21:55:00")
    body = punch_out(client, "EMP0005", "2026-07-09 06:40:00").json()
    assert (body["date"], body["work_hours"], body["overtime_minutes"]) == ("2026-07-08", 8.75, 40)


def test_punch_out_errors(client):
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    assert punch_out(client, "EMP0002", "2020-01-01 10:00:00").status_code == 404
    punch_in(client, "EMP0002", "2026-09-07 09:30:00")
    assert punch_out(client, "EMP0002", "2026-09-07 09:30:00").status_code == 422
    assert punch_out(client, "EMP0002", "2026-09-08 09:30:01").status_code == 422
    assert punch_out(client, "EMP0002", "2026-09-07 18:30:00").status_code == 200
    assert punch_out(client, "EMP0002", "2026-09-07 18:45:00").status_code == 409


def test_concurrent_punch_outs(client):
    punch_in(client, "EMP0002", "2026-09-09 09:30:00")
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: punch_out(client, "EMP0002", "2026-09-09 18:30:00").status_code, range(10)))
    assert sorted(codes) == [200] + [409] * 9


# ----------------------------------------------------------------------------- regularization
def test_regularize_recomputes_and_appends_history(client):
    punch_in(client, "EMP0001", "2026-07-07 10:05:00")
    punch_out(client, "EMP0001", "2026-07-07 18:30:00")
    body = patch(client, "EMP0001", "2026-07-07", punch_in=ms("2026-07-07 09:28:00")).json()
    assert (body["late_minutes"], body["work_hours"]) == (0, 9.03)
    [entry] = body["history"]
    assert (entry["by"], entry["reason"]) == ("hr.admin", "biometric glitch")
    assert entry["changes"] == {
        "punch_in": {"from": ms("2026-07-07 10:05:00"), "to": ms("2026-07-07 09:28:00")},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }
    body = patch(client, "EMP0001", "2026-07-07", status="LEAVE").json()
    assert (body["punch_in"], body["punch_out"], body["work_hours"], len(body["history"])) == (None, None, None, 2)
    assert body["history"][0] == entry  # never edited


@pytest.mark.parametrize("body, code", [
    ({"punch_in": "same"}, 422),                                     # changes nothing
    ({"status": "ABSENT", "punch_in": ms("2026-07-08 09:00:00")}, 422),
    ({"punch_in": ms("2026-07-09 09:00:00")}, 422),                  # leaves the attendance date
    ({"punch_out": ms("2026-07-08 08:00:00")}, 422),                 # before punch_in
    ({"punch_out": None}, 422),
    ({"reason": "x"}, 422),
])
def test_regularize_validation(client, body, code):
    if not client.get("/attendance?emp_code=EMP0001&date_from=2026-07-08&date_to=2026-07-08").json()["total"]:
        punch_in(client, "EMP0001", "2026-07-08 09:00:00")
    if body.get("punch_in") == "same":
        body = {"punch_in": ms("2026-07-08 09:00:00")}
    assert patch(client, "EMP0001", "2026-07-08", **body).status_code == code


def test_regularize_not_found_and_presence_needs_punch_in(client):
    assert patch(client, "EMP9999", "2026-07-08", status="LEAVE").status_code == 404
    assert patch(client, "EMP0001", "2025-01-01", status="LEAVE").status_code == 404
    assert patch(client, "EMP0001", "2026-13-01", status="LEAVE").status_code == 422
    punch_in(client, "EMP0001", "2026-07-23 09:00:00")
    patch(client, "EMP0001", "2026-07-23", status="ABSENT")
    assert patch(client, "EMP0001", "2026-07-23", status="PRESENT").status_code == 422
    restored = patch(client, "EMP0001", "2026-07-23", status="PRESENT", punch_in=ms("2026-07-23 09:45:00"))
    assert restored.status_code == 200 and restored.json()["late_minutes"] == 15


def test_concurrent_regularizations_do_not_lose_history(client):
    punch_in(client, "EMP0001", "2026-07-24 09:00:00")
    statuses = ["WFH", "ON_DUTY", "PRESENT"] * 3
    with ThreadPoolExecutor(9) as pool:
        codes = list(pool.map(lambda s: patch(client, "EMP0001", "2026-07-24", status=s).status_code, statuses))
    record = client.get("/attendance?emp_code=EMP0001&date_from=2026-07-24&date_to=2026-07-24").json()["items"][0]
    assert set(codes) <= {200, 409, 422} and codes.count(200) >= 1
    assert len(record["history"]) == codes.count(200)


# ----------------------------------------------------------------------------- list
def test_attendance_list(client):
    page = client.get("/attendance?date_from=2026-07-06&date_to=2026-07-06").json()
    assert [(r["date"], r["emp_code"]) for r in page["items"]] == [("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002"),
                                                                 ("2026-07-06", "EMP0005")]
    assert page["total"] == 3
    mine = client.get("/attendance?emp_code=EMP0002&page_size=100").json()["items"]
    assert [r["date"] for r in mine] == sorted((r["date"] for r in mine), reverse=True)
    for bad in ("date_from=2026-07-10&date_to=2026-07-01", "status=FOO", "date_from=2026-7-1", "page_size=101"):
        assert client.get(f"/attendance?{bad}").status_code == 422


def test_legacy_record_defaults(client):
    from app.main import attendance
    attendance.insert_one({"emp_code": "EMP0001", "date": "2026-06-01", "status": "LEAVE"})
    record = client.get("/attendance?emp_code=EMP0001&date_from=2026-06-01&date_to=2026-06-01").json()["items"][0]
    assert record == {"emp_code": "EMP0001", "date": "2026-06-01", "status": "LEAVE", "punch_in": None,
                      "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                      "half_day": False, "history": []}
