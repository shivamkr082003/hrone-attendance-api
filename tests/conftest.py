"""Tests run against a real MongoDB (6.0+). They use their own database and empty it before each test module.

    MONGO_URI=mongodb://localhost:27017 pytest -q
"""
import os

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = os.environ.get("TEST_MONGO_DB", "attendance_pytest")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


@pytest.fixture(scope="session")
def app_client():
    main.employees.drop()
    main.attendance.drop()
    with TestClient(main.app) as test_client:  # runs the lifespan, which creates the indexes
        yield test_client


@pytest.fixture(scope="module")
def client(app_client):
    main.employees.delete_many({})
    main.attendance.delete_many({})
    return app_client
