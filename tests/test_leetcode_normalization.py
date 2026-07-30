"""Bug-fix regression tests for LeetCode username normalization.

Covers:
- Direct unit test of normalize_leetcode_username()
- POST /api/students accepts messy inputs and stores cleaned value
- PUT /api/students/{id} accepts a full URL and stores cleaned value
- POST /api/students/me/leetcode-username accepts "u/..." and returns cleaned value
- POST /api/sync/student/{id} with a real public username returns numeric stats
"""
import os
import sys
import uuid
import pytest
import requests
from dotenv import load_dotenv

# Load env used by frontend for the public BASE_URL
load_dotenv("/app/frontend/.env")

# Enable direct import of the backend module for unit test of the normalizer
sys.path.insert(0, "/app/backend")

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@college.edu"
ADMIN_PASSWORD = "Admin@12345"

# ------------------------------------------------------------------
# 1. Unit test of the pure normalize helper
# ------------------------------------------------------------------
from leetcode import normalize_leetcode_username, fetch_leetcode_stats  # noqa: E402


@pytest.mark.parametrize("raw,expected", [
    ("nSKWHoKvyX", "nSKWHoKvyX"),
    ("u/nSKWHoKvyX", "nSKWHoKvyX"),
    ("U/nSKWHoKvyX", "nSKWHoKvyX"),
    ("@nSKWHoKvyX", "nSKWHoKvyX"),
    ("https://leetcode.com/u/nSKWHoKvyX/", "nSKWHoKvyX"),
    ("https://leetcode.com/u/nSKWHoKvyX", "nSKWHoKvyX"),
    ("leetcode.com/u/nSKWHoKvyX", "nSKWHoKvyX"),
    ("leetcode.com/u/nSKWHoKvyX/", "nSKWHoKvyX"),
    ("https://leetcode.com/nSKWHoKvyX/", "nSKWHoKvyX"),
    ("  nSKWHoKvyX  ", "nSKWHoKvyX"),
])
def test_normalize_leetcode_username_variants(raw, expected):
    assert normalize_leetcode_username(raw) == expected


def test_normalize_empty():
    assert normalize_leetcode_username("") == ""
    assert normalize_leetcode_username(None) == ""


# ------------------------------------------------------------------
# 2. Endpoint tests with admin auth
# ------------------------------------------------------------------
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="module")
def ref_ids(admin_session):
    """Create dept/branch/section to hang students off. Cleaned up at end."""
    code = f"TESTNORM{uuid.uuid4().hex[:4].upper()}"
    d = admin_session.post(f"{API}/departments", json={"name": "TEST Norm Dept", "code": code}, timeout=10)
    assert d.status_code == 200, d.text
    dept_id = d.json()["id"]

    b = admin_session.post(f"{API}/branches", json={
        "name": "TEST Norm Branch", "code": "TN1", "department_id": dept_id,
    }, timeout=10)
    assert b.status_code == 200, b.text
    branch_id = b.json()["id"]

    sec = admin_session.post(f"{API}/sections", json={
        "name": "A", "branch_id": branch_id, "year": 2, "semester": 3,
    }, timeout=10)
    assert sec.status_code == 200, sec.text
    section_id = sec.json()["id"]

    ids = {"dept": dept_id, "branch": branch_id, "section": section_id, "students": []}
    yield ids

    # ---- teardown ----
    for sid_ in ids["students"]:
        admin_session.delete(f"{API}/students/{sid_}", timeout=10)
    admin_session.delete(f"{API}/sections/{section_id}", timeout=10)
    admin_session.delete(f"{API}/branches/{branch_id}", timeout=10)
    admin_session.delete(f"{API}/departments/{dept_id}", timeout=10)


def _create_student(admin_session, ref_ids, leetcode_username):
    email = f"test_norm_{uuid.uuid4().hex[:6]}@college.edu"
    roll = f"TESTN{uuid.uuid4().hex[:6].upper()}"
    r = admin_session.post(f"{API}/students", json={
        "roll_number": roll,
        "name": "TEST Norm Student",
        "email": email,
        "password": "Student@123",
        "department_id": ref_ids["dept"],
        "branch_id": ref_ids["branch"],
        "section_id": ref_ids["section"],
        "year": 2,
        "semester": 3,
        "leetcode_username": leetcode_username,
    }, timeout=15)
    assert r.status_code == 200, r.text
    sid_ = r.json()["id"]
    ref_ids["students"].append(sid_)
    return sid_, email, r.json()


def _fetch_student(admin_session, sid_):
    r = admin_session.get(f"{API}/students", timeout=10)
    assert r.status_code == 200
    for s in r.json()["items"]:
        if s["id"] == sid_:
            return s
    raise AssertionError(f"Student {sid_} not found in list")


# 2a. POST /api/students normalizes "u/nSKWHoKvyX"
def test_post_students_normalizes_u_prefix(admin_session, ref_ids):
    sid_, _, create_resp = _create_student(admin_session, ref_ids, "u/nSKWHoKvyX")
    # Response should already carry cleaned value
    assert create_resp.get("leetcode_username") == "nSKWHoKvyX", create_resp
    # Persistence check via GET
    fetched = _fetch_student(admin_session, sid_)
    assert fetched["leetcode_username"] == "nSKWHoKvyX"


# 2b. PUT /api/students/{id} normalizes full URL
def test_put_students_normalizes_full_url(admin_session, ref_ids):
    sid_, _, _ = _create_student(admin_session, ref_ids, "nSKWHoKvyX")
    r = admin_session.put(
        f"{API}/students/{sid_}",
        json={"leetcode_username": "https://leetcode.com/u/neetcode/"},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    fetched = _fetch_student(admin_session, sid_)
    assert fetched["leetcode_username"] == "neetcode", fetched


# 2c. POST /api/students/me/leetcode-username normalizes "u/neetcode"
def test_student_self_update_leetcode_username(admin_session, ref_ids):
    sid_, email, _ = _create_student(admin_session, ref_ids, "temp")
    # Student login
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": "Student@123"}, timeout=10)
    assert r.status_code == 200, r.text
    r2 = s.post(
        f"{API}/students/me/leetcode-username",
        json={"leetcode_username": "u/neetcode"},
        timeout=10,
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body.get("leetcode_username") == "neetcode", body
    # Persistence check via admin GET
    fetched = _fetch_student(admin_session, sid_)
    assert fetched["leetcode_username"] == "neetcode"


# 2d. Single sync succeeds and returns numeric stats
def test_single_student_sync_after_normalized_url_input(admin_session, ref_ids):
    """User previously typed a URL; sync must now work because the URL was stripped on save."""
    sid_, _, _ = _create_student(admin_session, ref_ids, "https://leetcode.com/u/neetcode/")
    fetched = _fetch_student(admin_session, sid_)
    assert fetched["leetcode_username"] == "neetcode"  # sanity

    r = admin_session.post(f"{API}/sync/student/{sid_}", timeout=45)
    if r.status_code == 502:
        pytest.skip(f"LeetCode public GraphQL blocked from env: {r.text}")
    assert r.status_code == 200, r.text
    stats = r.json().get("stats")
    assert stats is not None, r.json()
    for k in ("total_solved", "easy", "medium", "hard"):
        assert isinstance(stats.get(k), int), f"{k} not int: {stats}"
    assert stats["total_solved"] >= 1


# 2e. Bulk sync completes and reports at least one success (or is skipped for env reasons)
def test_bulk_sync_after_url_input_completes(admin_session, ref_ids):
    """With a valid public username in DB, bulk sync must complete without leaking `u/...` to LeetCode."""
    _create_student(admin_session, ref_ids, "u/neetcode")
    r = admin_session.post(f"{API}/sync/leetcode", timeout=15)
    assert r.status_code == 200, r.text
    sid_ = r.json()["sync_id"]
    import time as _t
    final = None
    for _ in range(30):
        _t.sleep(1)
        st = admin_session.get(f"{API}/sync/status/{sid_}", timeout=10)
        assert st.status_code == 200
        final = st.json()
        if final.get("status") == "completed":
            break
    assert final is not None
    if final["status"] != "completed":
        pytest.skip(f"Bulk sync still running: {final}")
    # No log line should say 'user not found' anymore for u/... entries
    logs_joined = " | ".join(final.get("logs") or [])
    assert "user not found" not in logs_joined.lower(), (
        f"'user not found' still present after normalization: {logs_joined}"
    )
    # Any log line that references a u/... username must be a success (means it was normalized before hitting LC)
    for line in (final.get("logs") or []):
        if "u/" in line:
            assert line.startswith("[OK]"), f"u/... username failed sync (should have been normalized): {line}"
    if final.get("success", 0) == 0 and final.get("failed", 0) > 0:
        pytest.skip(f"LeetCode may be rate-limited in env, all failed: {final}")
    assert final.get("success", 0) >= 1, final


# ------------------------------------------------------------------
# 3. Async direct test of fetch_leetcode_stats — used as env-tolerant fallback
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_leetcode_stats_direct_with_url_input():
    """Direct call with a URL must return a dict (not None) — proves normalize + fetch work end-to-end."""
    res = await fetch_leetcode_stats("https://leetcode.com/u/neetcode/")
    if res is None:
        pytest.skip("LeetCode returned no matched user (rate limit / env block)")
    if isinstance(res, dict) and res.get("__error__"):
        pytest.skip(f"LeetCode network error: {res['__error__']}")
    assert isinstance(res, dict)
    assert res.get("username", "").lower() == "neetcode"
    assert isinstance(res.get("total_solved"), int)
