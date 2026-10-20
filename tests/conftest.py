"""Pytest configuration: isolate the test database and add the project root to sys.path."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Use a throwaway database for the test session (set BEFORE backend is imported).
TEST_DB = ROOT / "tests" / "test_flood.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["FLOOD_DB_PATH"] = str(TEST_DB)

import pytest


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as c:
        yield c
