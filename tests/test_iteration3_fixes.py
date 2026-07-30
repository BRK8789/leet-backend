"""Iteration 3 targeted regression tests.

Covers ONLY the 4 items requested by main agent:
  1. Helper text on StudentDashboard.jsx (static file inspection)
  2. CSV importer normalizes leetcode_username
  3. Startup backfill rewrites legacy leetcode_username values
  4. Regression: admin login + normalize unit variants + sync numeric stats
"""
import os
import sys
import io
import time
import uuid
import subprocess

import pytest
import requests
from dotenv import load_dotenv
from pymongo import MongoClient

# Enable env
load_dotenv("/app/frontend/.env")
load_dotenv("/app/backend/.env")

sys.path.insert(0, "/app/backend")
from leetcode import normalize_leetcode_username  # noqa: E402

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
ADMIN_EMAIL = "admin@college.edu"
ADMIN_PASSWORD = "Admin@12345"


# ---------- Fixtures ----------
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="module")
def mongo():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


# ============================================================
# GAP #1 — Helper text on StudentDashboard.jsx
# ============================================================
def test_student_dashboard_has_helper_text():
    """Assert the helper text exists in the source file with required phrasing."""
    with open("/app/frontend/src/pages/StudentDashboard.jsx", "r") as f:
        src = f.read()
    assert "Enter just the username" in src, "helper text 'Enter just the username' missing"
    assert "leetcode.com/u/nSKWHoKvyX/" in src, "example URL missing"
    assert "nSKWHoKvyX" in src, "example username missing"
    # Assert it's above the input and inside its own helper div
    assert 'text-xs text-zinc-500 mb-3' in src, "helper div styling missing"


# ============================================================
# GAP #2 — CSV importer normalizes leetcode_username
# ============================================================
def test_csv_import_normalizes_leetcode_username(admin_session, mongo):
    """POST /api/students/import/csv with `u/testuser1` should persist `testuser1`."""
    unique = uuid.uuid4().hex[:8]
    email = f"testcsv_{unique}@example.com"
    roll = f"TESTCSV{unique}"

    csv_body = (
        "roll_number,name,email,leetcode_username,year,semester\n"
        f"{roll},TEST CSV Student,{email},u/testuser1,1,1\n"
    )
    r = admin_session.post(
        f"{API}/students/import/csv",
        data=csv_body.encode("utf-8"),
        headers={"Content-Type": "text/csv"},
    )
    assert r.status_code == 200, f"csv import failed: {r.status_code} {r.text}"
    body = r.json()
    assert body.get("created", 0) >= 1, f"expected created>=1 got {body}"

    # Verify directly in Mongo (also verify via API for completeness)
    stored = mongo.users.find_one({"email": email})
    assert stored is not None, f"imported student not found in Mongo. import response: {body}"
    assert stored.get("leetcode_username") == "testuser1", (
        f"expected 'testuser1' got {stored.get('leetcode_username')!r}"
    )

    # Also verify through API surface
    r2 = admin_session.get(f"{API}/students", params={"q": roll})
    assert r2.status_code == 200
    payload = r2.json()
    students = payload.get("items", payload) if isinstance(payload, dict) else payload
    match = next((s for s in students if s.get("email") == email), None)
    assert match is not None, f"imported student not found in GET /students"
    assert match.get("leetcode_username") == "testuser1", (
        f"expected 'testuser1' got {match.get('leetcode_username')!r}"
    )

    # Cleanup
    mongo.users.delete_one({"email": email})


# ============================================================
# STARTUP BACKFILL — normalizes legacy values on restart
# ============================================================
def test_startup_backfill_normalizes_legacy_values(mongo):
    """Manually insert a student with `u/manualtest`, restart backend, verify normalized."""
    # Create a minimally-valid student doc directly in Mongo with legacy value
    unique = uuid.uuid4().hex[:8]
    email = f"TESTBACKFILL_{unique}@example.com"
    legacy_username = f"u/manualtest_{unique}"

    doc = {
        "email": email,
        "password_hash": "irrelevant",
        "name": "TEST Backfill Student",
        "role": "student",
        "roll_number": f"TESTBF{unique}",
        "year": 1,
        "semester": 1,
        "leetcode_username": legacy_username,
        "leetcode_stats": None,
        "created_at": "1970-01-01T00:00:00Z",
    }
    res = mongo.users.insert_one(doc)
    inserted_id = res.inserted_id

    try:
        # Confirm legacy value stored raw
        d = mongo.users.find_one({"_id": inserted_id})
        assert d["leetcode_username"] == legacy_username

        # Restart backend to trigger startup backfill
        subprocess.run(
            ["sudo", "supervisorctl", "restart", "backend"],
            check=True, capture_output=True, timeout=30,
        )

        # Wait for backend to come back up
        deadline = time.time() + 40
        ok = False
        while time.time() < deadline:
            try:
                r = requests.get(f"{API}/", timeout=3)
                if r.status_code < 500:
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(1)
        assert ok, "backend did not come back up after restart"

        # Small extra pause so startup_event finishes (indexes + backfill)
        time.sleep(2)

        # Verify value was rewritten
        d2 = mongo.users.find_one({"_id": inserted_id})
        assert d2["leetcode_username"] == f"manualtest_{unique}", (
            f"backfill did not normalize: {d2['leetcode_username']!r}"
        )
    finally:
        mongo.users.delete_one({"_id": inserted_id})


def test_startup_backfill_log_line_present():
    """Verify the backend log contains the backfill log line."""
    log_paths = [
        "/var/log/supervisor/backend.out.log",
        "/var/log/supervisor/backend.err.log",
    ]
    found = False
    for p in log_paths:
        if not os.path.exists(p):
            continue
        with open(p, "r", errors="ignore") as f:
            content = f.read()[-200_000:]  # tail 200KB
        if "Backfill: normalized" in content and "legacy leetcode_username" in content:
            found = True
            break
    assert found, "Startup backfill log line not observed in backend logs"


# ============================================================
# REGRESSION #1 — admin login still works
# ============================================================
def test_admin_login_works():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200
    data = r.json()
    assert data.get("user", {}).get("email") == ADMIN_EMAIL
    assert data.get("user", {}).get("role") == "admin"


# ============================================================
# REGRESSION #2 — normalize_leetcode_username still handles all variants
# ============================================================
@pytest.mark.parametrize("raw,expected", [
    ("u/x", "x"),
    ("@x", "x"),
    ("https://leetcode.com/u/x/", "x"),
    ("leetcode.com/x", "x"),
    ("x", "x"),
])
def test_normalize_variants_regression(raw, expected):
    assert normalize_leetcode_username(raw) == expected


# ============================================================
# REGRESSION #3 — POST /api/sync/student/{id} returns numeric stats
# ============================================================
def test_sync_student_with_neetcode_returns_numeric_stats(admin_session, mongo):
    """Insert a student with leetcode_username='neetcode', run single sync, expect numeric stats."""
    unique = uuid.uuid4().hex[:8]
    email = f"TESTSYNC_{unique}@example.com"

    # Insert directly to bypass department_id/branch_id/section_id requirement
    doc = {
        "email": email,
        "password_hash": "irrelevant",
        "name": "TEST Sync Student",
        "role": "student",
        "roll_number": f"TESTSYNC{unique}",
        "year": 1,
        "semester": 1,
        "leetcode_username": "neetcode",
        "leetcode_stats": None,
        "created_at": "1970-01-01T00:00:00Z",
    }
    res = mongo.users.insert_one(doc)
    sid = str(res.inserted_id)

    try:
        # Trigger sync
        rs = admin_session.post(f"{API}/sync/student/{sid}")
        assert rs.status_code == 200, f"sync failed: {rs.status_code} {rs.text}"

        # Fetch back and validate stats
        rg = admin_session.get(f"{API}/students/{sid}")
        assert rg.status_code == 200
        s = rg.json()
        stats = s.get("leetcode_stats") or {}
        total = stats.get("total_solved") or 0
        assert isinstance(total, int) and total > 0, f"expected numeric total_solved > 0, got {stats!r}"
    finally:
        mongo.users.delete_one({"_id": res.inserted_id})
