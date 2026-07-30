"""End-to-end backend tests for LeetCode Student Progress Tracker.

Covers:
- Auth (login / me / logout / role protection)
- Departments / Branches / Sections CRUD
- Faculty create + faculty login
- Students create + list + role scoping
- LeetCode single-student sync + bulk sync + status polling
- Leaderboard, analytics, reports (csv/xlsx/pdf)
- Settings
"""
import os
import time
import uuid
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/frontend/.env")

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@college.edu"
ADMIN_PASSWORD = "Admin@12345"

# ------------------------ Shared state to chain tests ------------------------
STATE = {}


# ------------------------ Fixtures ------------------------
@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    data = r.json()
    assert data["user"]["role"] == "admin"
    STATE["admin_token"] = data["access_token"]
    return s


@pytest.fixture(scope="session")
def anon_session():
    return requests.Session()


# ------------------------ Auth ------------------------
class TestAuth:
    def test_login_success(self, admin_session):
        r = admin_session.get(f"{API}/auth/me", timeout=10)
        assert r.status_code == 200
        assert r.json()["email"] == ADMIN_EMAIL

    def test_me_unauthenticated(self, anon_session):
        r = anon_session.get(f"{API}/auth/me", timeout=10)
        assert r.status_code == 401

    def test_login_wrong_password(self, anon_session):
        r = anon_session.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "bad"}, timeout=10)
        assert r.status_code == 401


# ------------------------ Departments ------------------------
class TestDepartments:
    def test_create_department(self, admin_session):
        code = f"TEST{uuid.uuid4().hex[:4].upper()}"
        r = admin_session.post(f"{API}/departments", json={"name": "TEST Dept", "code": code}, timeout=10)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["code"] == code
        assert "id" in data
        STATE["department_id"] = data["id"]
        STATE["department_code"] = code

    def test_list_departments(self, admin_session):
        r = admin_session.get(f"{API}/departments", timeout=10)
        assert r.status_code == 200
        assert any(d["id"] == STATE["department_id"] for d in r.json())

    def test_duplicate_department_code(self, admin_session):
        r = admin_session.post(f"{API}/departments", json={"name": "dup", "code": STATE["department_code"]}, timeout=10)
        assert r.status_code == 400


# ------------------------ Branches ------------------------
class TestBranches:
    def test_create_branch(self, admin_session):
        r = admin_session.post(f"{API}/branches", json={
            "name": "TEST Branch", "code": "TB1", "department_id": STATE["department_id"]
        }, timeout=10)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["department_id"] == STATE["department_id"]
        STATE["branch_id"] = data["id"]

    def test_list_branches(self, admin_session):
        r = admin_session.get(f"{API}/branches?department_id={STATE['department_id']}", timeout=10)
        assert r.status_code == 200
        assert any(b["id"] == STATE["branch_id"] for b in r.json())


# ------------------------ Sections ------------------------
class TestSections:
    def test_create_section(self, admin_session):
        r = admin_session.post(f"{API}/sections", json={
            "name": "A", "branch_id": STATE["branch_id"], "year": 2, "semester": 3
        }, timeout=10)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["branch_id"] == STATE["branch_id"]
        STATE["section_id"] = data["id"]

    def test_list_sections(self, admin_session):
        r = admin_session.get(f"{API}/sections?branch_id={STATE['branch_id']}", timeout=10)
        assert r.status_code == 200
        assert len(r.json()) >= 1


# ------------------------ Faculty ------------------------
class TestFaculty:
    def test_create_faculty(self, admin_session):
        email = f"test_faculty_{uuid.uuid4().hex[:6]}@college.edu"
        r = admin_session.post(f"{API}/faculty", json={
            "name": "TEST Faculty",
            "email": email,
            "password": "Faculty@123",
            "department_id": STATE["department_id"],
            "assigned_section_ids": [STATE["section_id"]],
        }, timeout=10)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["role"] == "faculty"
        STATE["faculty_id"] = data["id"]
        STATE["faculty_email"] = email

    def test_faculty_login(self):
        s = requests.Session()
        r = s.post(f"{API}/auth/login", json={"email": STATE["faculty_email"], "password": "Faculty@123"}, timeout=10)
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "faculty"
        STATE["faculty_session"] = s

    def test_faculty_cannot_create_department(self):
        s = STATE["faculty_session"]
        r = s.post(f"{API}/departments", json={"name": "Nope", "code": "NOPE1"}, timeout=10)
        assert r.status_code == 403


# ------------------------ Students ------------------------
class TestStudents:
    def test_create_student_with_leetcode_username(self, admin_session):
        email = f"test_student_{uuid.uuid4().hex[:6]}@college.edu"
        roll = f"TEST{uuid.uuid4().hex[:6].upper()}"
        r = admin_session.post(f"{API}/students", json={
            "roll_number": roll,
            "name": "TEST Student",
            "email": email,
            "password": "Student@123",
            "department_id": STATE["department_id"],
            "branch_id": STATE["branch_id"],
            "section_id": STATE["section_id"],
            "year": 2,
            "semester": 3,
            "faculty_id": STATE["faculty_id"],
            "leetcode_username": "neetcode",
        }, timeout=10)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["role"] == "student"
        assert data["roll_number"] == roll
        STATE["student_id"] = data["id"]
        STATE["student_email"] = email

    def test_list_students_as_admin(self, admin_session):
        r = admin_session.get(f"{API}/students", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert "items" in data and "total" in data
        assert any(s["id"] == STATE["student_id"] for s in data["items"])

    def test_list_students_as_faculty_shows_assigned(self):
        s = STATE["faculty_session"]
        r = s.get(f"{API}/students", timeout=10)
        assert r.status_code == 200
        # Faculty should see only assigned student(s)
        data = r.json()
        for st in data["items"]:
            assert st.get("faculty_id") == STATE["faculty_id"]

    def test_student_login_and_role_scoping(self):
        s = requests.Session()
        r = s.post(f"{API}/auth/login", json={"email": STATE["student_email"], "password": "Student@123"}, timeout=10)
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "student"

        # Student can NOT create departments
        r2 = s.post(f"{API}/departments", json={"name": "no", "code": "NOX"}, timeout=10)
        assert r2.status_code == 403

        # Student listing only shows self
        r3 = s.get(f"{API}/students", timeout=10)
        assert r3.status_code == 200
        items = r3.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == STATE["student_id"]


# ------------------------ LeetCode sync ------------------------
class TestLeetCodeSync:
    def test_single_student_sync(self, admin_session):
        r = admin_session.post(f"{API}/sync/student/{STATE['student_id']}", timeout=30)
        # LeetCode API may be blocked from environment => tolerate 502/404
        if r.status_code == 502:
            pytest.skip(f"LeetCode API unreachable from env: {r.text}")
        assert r.status_code == 200, r.text
        stats = r.json()["stats"]
        assert isinstance(stats.get("total_solved"), int)
        assert stats["total_solved"] >= 0
        STATE["leetcode_worked"] = stats["total_solved"] > 0

    def test_bulk_sync_and_status(self, admin_session):
        r = admin_session.post(f"{API}/sync/leetcode", timeout=10)
        assert r.status_code == 200
        sid = r.json()["sync_id"]
        # Poll status up to 25s
        for _ in range(25):
            time.sleep(1)
            st = admin_session.get(f"{API}/sync/status/{sid}", timeout=10)
            assert st.status_code == 200
            if st.json().get("status") == "completed":
                break
        final = admin_session.get(f"{API}/sync/status/{sid}", timeout=10).json()
        assert final["status"] in ("completed", "running")
        # Success + failed should sum to total when completed
        if final["status"] == "completed":
            assert final["success"] + final["failed"] == final["total"]


# ------------------------ Leaderboard / analytics ------------------------
class TestAnalytics:
    def test_leaderboard(self, admin_session):
        r = admin_session.get(f"{API}/leaderboard", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        # Verify sorted desc by total_solved
        totals = [row["total_solved"] for row in data]
        assert totals == sorted(totals, reverse=True)

    def test_analytics_summary(self, admin_session):
        r = admin_session.get(f"{API}/analytics/summary", timeout=10)
        assert r.status_code == 200
        data = r.json()
        for k in ["total_students", "active_students", "inactive_students", "totals", "avg_per_student", "top_performers"]:
            assert k in data

    def test_analytics_by_department(self, admin_session):
        r = admin_session.get(f"{API}/analytics/by-department", timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_analytics_by_year(self, admin_session):
        r = admin_session.get(f"{API}/analytics/by-year", timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ------------------------ Reports ------------------------
class TestReports:
    def test_report_csv(self, admin_session):
        r = admin_session.get(f"{API}/reports/students?format=csv", timeout=10)
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")
        assert b"Roll No" in r.content
        assert len(r.content) > 0

    def test_report_xlsx(self, admin_session):
        r = admin_session.get(f"{API}/reports/students?format=xlsx", timeout=10)
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        assert "spreadsheetml" in ct or "xlsx" in ct
        # xlsx magic starts with PK zip
        assert r.content[:2] == b"PK"

    def test_report_pdf(self, admin_session):
        r = admin_session.get(f"{API}/reports/students?format=pdf", timeout=15)
        assert r.status_code == 200
        assert "application/pdf" in r.headers.get("content-type", "")
        assert r.content[:4] == b"%PDF"


# ------------------------ Settings ------------------------
class TestSettings:
    def test_get_settings(self, admin_session):
        r = admin_session.get(f"{API}/settings", timeout=10)
        assert r.status_code == 200
        assert "college_name" in r.json()

    def test_update_settings(self, admin_session):
        r = admin_session.put(f"{API}/settings", json={"college_name": "TEST College"}, timeout=10)
        assert r.status_code == 200
        assert r.json()["college_name"] == "TEST College"


# ------------------------ Cleanup (final) ------------------------
class TestZCleanup:
    """Runs alphabetically last."""
    def test_cleanup_created_data(self, admin_session):
        # Delete student, faculty, section, branch, department
        if STATE.get("student_id"):
            admin_session.delete(f"{API}/students/{STATE['student_id']}", timeout=10)
        if STATE.get("faculty_id"):
            admin_session.delete(f"{API}/faculty/{STATE['faculty_id']}", timeout=10)
        if STATE.get("section_id"):
            admin_session.delete(f"{API}/sections/{STATE['section_id']}", timeout=10)
        if STATE.get("branch_id"):
            admin_session.delete(f"{API}/branches/{STATE['branch_id']}", timeout=10)
        if STATE.get("department_id"):
            admin_session.delete(f"{API}/departments/{STATE['department_id']}", timeout=10)
        assert True
