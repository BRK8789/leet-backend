"""LeetCode Student Progress Tracking System - FastAPI backend."""
from dotenv import load_dotenv
from pathlib import Path
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import os
import io
import csv
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, Query
from fastapi.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from openpyxl import Workbook
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

from auth import (
    hash_password, verify_password, create_access_token, create_refresh_token,
    get_current_user_from_request, user_to_public,
)
from leetcode import fetch_leetcode_stats, normalize_leetcode_username
from models import (
    LoginIn, ChangePasswordIn, DepartmentIn, BranchIn, SectionIn,
    FacultyIn, FacultyUpdate, StudentIn, StudentUpdate, LeetCodeUsernameIn,
    SettingsIn,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- Mongo ----------
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI(title="LeetCode Student Progress Tracker")
api = APIRouter(prefix="/api")

cors_origins_env = os.environ.get("CORS_ORIGINS", "")
raw_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip() and o.strip() != "*"]

known_origins = [
    "https://leet-frontend.pages.dev",
    "https://leet-frontend.drbrk8789.workers.dev",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
]
for ko in known_origins:
    if ko not in raw_origins:
        raw_origins.append(ko)

app.add_middleware(
    CORSMiddleware,
    allow_origins=raw_origins,
    allow_origin_regex=r"https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Helpers ----------
def oid(v) -> ObjectId:
    try:
        return ObjectId(v)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")


def sid(doc: dict) -> dict:
    """Serialize a MongoDB doc: convert ObjectId to string, add 'id' key."""
    if not doc:
        return doc
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out["id"] = str(v)
        elif isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_current_user(request: Request) -> dict:
    return await get_current_user_from_request(request)


async def require_admin(request: Request) -> dict:
    user = await get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def require_admin_or_faculty(request: Request) -> dict:
    user = await get_current_user(request)
    if user.get("role") not in ("admin", "faculty"):
        raise HTTPException(status_code=403, detail="Admin/Faculty access required")
    return user


# ---------- Startup ----------
@app.on_event("startup")
async def startup_event():
    # Indexes
    await db.users.create_index("email", unique=True)
    await db.users.create_index("role")
    await db.users.create_index("roll_number", sparse=True)
    await db.departments.create_index("code", unique=True)
    await db.branches.create_index([("code", 1), ("department_id", 1)])
    await db.sync_logs.create_index("started_at")

    # Seed admin
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@college.edu")
    admin_password = os.environ.get("ADMIN_PASSWORD", "Admin@12345")
    existing = await db.users.find_one({"email": admin_email})
    if not existing:
        await db.users.insert_one({
            "email": admin_email,
            "password_hash": hash_password(admin_password),
            "name": "System Administrator",
            "role": "admin",
            "created_at": now_iso(),
        })
        logger.info(f"Admin user seeded: {admin_email}")
    else:
        # Update password if changed
        if not verify_password(admin_password, existing.get("password_hash", "")):
            await db.users.update_one(
                {"_id": existing["_id"]},
                {"$set": {"password_hash": hash_password(admin_password)}},
            )

    # Seed default settings
    settings = await db.settings.find_one({"_id": "global"})
    if not settings:
        await db.settings.insert_one({
            "_id": "global",
            "college_name": "Engineering College",
            "notification_thresholds": {"problem_milestone": [100, 500], "inactive_days": 14},
        })

    # One-time backfill: normalize any legacy leetcode_username values that
    # contain 'u/', '@' or a full leetcode.com URL.
    legacy = await db.users.find(
        {"role": "student", "leetcode_username": {"$regex": r"^(u/|@|https?://|leetcode\.com/)", "$options": "i"}},
        {"_id": 1, "leetcode_username": 1},
    ).to_list(10000)
    for u in legacy:
        cleaned = normalize_leetcode_username(u.get("leetcode_username") or "")
        if cleaned != u.get("leetcode_username"):
            await db.users.update_one({"_id": u["_id"]}, {"$set": {"leetcode_username": cleaned}})
    if legacy:
        logger.info(f"Backfill: normalized {len(legacy)} legacy leetcode_username value(s)")


# ---------- Auth Routes ----------
@api.post("/auth/login")
async def login(body: LoginIn, response: Response):
    email = body.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    access_token = create_access_token(str(user["_id"]), email, user["role"])
    refresh_token = create_refresh_token(str(user["_id"]))
    response.set_cookie("access_token", access_token, httponly=True, secure=False,
                        samesite="lax", max_age=8 * 3600, path="/")
    response.set_cookie("refresh_token", refresh_token, httponly=True, secure=False,
                        samesite="lax", max_age=7 * 86400, path="/")
    return {"user": user_to_public(user), "access_token": access_token}


@api.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return {"ok": True}


@api.get("/auth/me")
async def me(request: Request):
    user = await get_current_user(request)
    return user_to_public(user)


@api.post("/auth/change-password")
async def change_password(body: ChangePasswordIn, request: Request):
    user = await get_current_user(request)
    if not verify_password(body.old_password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Old password incorrect")
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password_hash": hash_password(body.new_password)}},
    )
    return {"ok": True}


# ---------- Departments ----------
@api.get("/departments")
async def list_departments(request: Request):
    await get_current_user(request)
    docs = await db.departments.find().to_list(500)
    return [sid(d) for d in docs]


@api.post("/departments")
async def create_department(body: DepartmentIn, request: Request):
    await require_admin(request)
    if await db.departments.find_one({"code": body.code}):
        raise HTTPException(status_code=400, detail="Department code already exists")
    res = await db.departments.insert_one({"name": body.name, "code": body.code, "created_at": now_iso()})
    return sid(await db.departments.find_one({"_id": res.inserted_id}))


@api.put("/departments/{dep_id}")
async def update_department(dep_id: str, body: DepartmentIn, request: Request):
    await require_admin(request)
    await db.departments.update_one({"_id": oid(dep_id)}, {"$set": {"name": body.name, "code": body.code}})
    return sid(await db.departments.find_one({"_id": oid(dep_id)}))


@api.delete("/departments/{dep_id}")
async def delete_department(dep_id: str, request: Request):
    await require_admin(request)
    await db.departments.delete_one({"_id": oid(dep_id)})
    return {"ok": True}


# ---------- Branches ----------
@api.get("/branches")
async def list_branches(request: Request, department_id: Optional[str] = None):
    await get_current_user(request)
    q = {}
    if department_id:
        q["department_id"] = department_id
    docs = await db.branches.find(q).to_list(1000)
    return [sid(d) for d in docs]


@api.post("/branches")
async def create_branch(body: BranchIn, request: Request):
    await require_admin(request)
    res = await db.branches.insert_one({
        "name": body.name, "code": body.code,
        "department_id": body.department_id, "created_at": now_iso(),
    })
    return sid(await db.branches.find_one({"_id": res.inserted_id}))


@api.put("/branches/{bid}")
async def update_branch(bid: str, body: BranchIn, request: Request):
    await require_admin(request)
    await db.branches.update_one({"_id": oid(bid)}, {"$set": body.model_dump()})
    return sid(await db.branches.find_one({"_id": oid(bid)}))


@api.delete("/branches/{bid}")
async def delete_branch(bid: str, request: Request):
    await require_admin(request)
    await db.branches.delete_one({"_id": oid(bid)})
    return {"ok": True}


# ---------- Sections ----------
@api.get("/sections")
async def list_sections(request: Request, branch_id: Optional[str] = None):
    await get_current_user(request)
    q = {}
    if branch_id:
        q["branch_id"] = branch_id
    docs = await db.sections.find(q).to_list(1000)
    return [sid(d) for d in docs]


@api.post("/sections")
async def create_section(body: SectionIn, request: Request):
    await require_admin(request)
    res = await db.sections.insert_one({**body.model_dump(), "created_at": now_iso()})
    return sid(await db.sections.find_one({"_id": res.inserted_id}))


@api.put("/sections/{sec_id}")
async def update_section(sec_id: str, body: SectionIn, request: Request):
    await require_admin(request)
    await db.sections.update_one({"_id": oid(sec_id)}, {"$set": body.model_dump()})
    return sid(await db.sections.find_one({"_id": oid(sec_id)}))


@api.delete("/sections/{sec_id}")
async def delete_section(sec_id: str, request: Request):
    await require_admin(request)
    await db.sections.delete_one({"_id": oid(sec_id)})
    return {"ok": True}


# ---------- Faculty ----------
@api.get("/faculty")
async def list_faculty(request: Request):
    await require_admin_or_faculty(request)
    docs = await db.users.find({"role": "faculty"}).to_list(1000)
    return [{**user_to_public(d),
             "assigned_section_ids": d.get("assigned_section_ids", [])} for d in docs]


@api.post("/faculty")
async def create_faculty(body: FacultyIn, request: Request):
    await require_admin(request)
    email = body.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already exists")
    doc = {
        "email": email,
        "password_hash": hash_password(body.password),
        "name": body.name,
        "role": "faculty",
        "mobile": body.mobile,
        "department_id": body.department_id,
        "assigned_section_ids": body.assigned_section_ids,
        "created_at": now_iso(),
    }
    res = await db.users.insert_one(doc)
    return {**user_to_public(await db.users.find_one({"_id": res.inserted_id})),
            "assigned_section_ids": body.assigned_section_ids}


@api.put("/faculty/{fid}")
async def update_faculty(fid: str, body: FacultyUpdate, request: Request):
    await require_admin(request)
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    if upd:
        await db.users.update_one({"_id": oid(fid), "role": "faculty"}, {"$set": upd})
    d = await db.users.find_one({"_id": oid(fid)})
    return {**user_to_public(d), "assigned_section_ids": d.get("assigned_section_ids", [])}


@api.delete("/faculty/{fid}")
async def delete_faculty(fid: str, request: Request):
    await require_admin(request)
    await db.users.delete_one({"_id": oid(fid), "role": "faculty"})
    return {"ok": True}


# ---------- Students ----------
def _student_dict(d: dict) -> dict:
    return {
        **user_to_public(d),
        "year": d.get("year"),
        "semester": d.get("semester"),
        "faculty_id": d.get("faculty_id"),
        "leetcode_stats": d.get("leetcode_stats"),
        "last_synced_at": d.get("last_synced_at"),
    }


@api.get("/students")
async def list_students(
    request: Request,
    department_id: Optional[str] = None,
    branch_id: Optional[str] = None,
    section_id: Optional[str] = None,
    faculty_id: Optional[str] = None,
    q: Optional[str] = None,
    skip: int = 0,
    limit: int = 200,
):
    user = await get_current_user(request)
    query: dict = {"role": "student"}
    if user["role"] == "faculty":
        query["faculty_id"] = str(user["_id"])
    elif user["role"] == "student":
        query["_id"] = user["_id"]
    if department_id:
        query["department_id"] = department_id
    if branch_id:
        query["branch_id"] = branch_id
    if section_id:
        query["section_id"] = section_id
    if faculty_id:
        query["faculty_id"] = faculty_id
    if q:
        query["$or"] = [
            {"name": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
            {"roll_number": {"$regex": q, "$options": "i"}},
            {"leetcode_username": {"$regex": q, "$options": "i"}},
        ]
    total = await db.users.count_documents(query)
    docs = await db.users.find(query).skip(skip).limit(limit).to_list(limit)
    return {"total": total, "items": [_student_dict(d) for d in docs]}


@api.post("/students")
async def create_student(body: StudentIn, request: Request):
    await require_admin(request)
    email = body.email.lower().strip()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already exists")
    if await db.users.find_one({"roll_number": body.roll_number, "role": "student"}):
        raise HTTPException(status_code=400, detail="Roll number already exists")
    doc = {
        "email": email,
        "password_hash": hash_password(body.password),
        "name": body.name,
        "role": "student",
        "roll_number": body.roll_number,
        "mobile": body.mobile,
        "department_id": body.department_id,
        "branch_id": body.branch_id,
        "section_id": body.section_id,
        "year": body.year,
        "semester": body.semester,
        "faculty_id": body.faculty_id,
        "leetcode_username": normalize_leetcode_username(body.leetcode_username or ""),
        "profile_photo": body.profile_photo,
        "leetcode_stats": None,
        "created_at": now_iso(),
    }
    res = await db.users.insert_one(doc)
    return _student_dict(await db.users.find_one({"_id": res.inserted_id}))


@api.get("/students/{sid_}")
async def get_student(sid_: str, request: Request):
    user = await get_current_user(request)
    d = await db.users.find_one({"_id": oid(sid_), "role": "student"})
    if not d:
        raise HTTPException(status_code=404, detail="Student not found")
    if user["role"] == "faculty" and d.get("faculty_id") != str(user["_id"]):
        raise HTTPException(status_code=403, detail="Not your student")
    if user["role"] == "student" and str(user["_id"]) != sid_:
        raise HTTPException(status_code=403, detail="Forbidden")
    return _student_dict(d)


@api.put("/students/{sid_}")
async def update_student(sid_: str, body: StudentUpdate, request: Request):
    await require_admin(request)
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    if "leetcode_username" in upd:
        upd["leetcode_username"] = normalize_leetcode_username(upd["leetcode_username"] or "")
    if upd:
        await db.users.update_one({"_id": oid(sid_), "role": "student"}, {"$set": upd})
    return _student_dict(await db.users.find_one({"_id": oid(sid_)}))


@api.delete("/students/{sid_}")
async def delete_student(sid_: str, request: Request):
    await require_admin(request)
    await db.users.delete_one({"_id": oid(sid_), "role": "student"})
    return {"ok": True}


@api.post("/students/me/leetcode-username")
async def update_own_leetcode(body: LeetCodeUsernameIn, request: Request):
    user = await get_current_user(request)
    if user["role"] != "student":
        raise HTTPException(status_code=403, detail="Only students can update this")
    cleaned = normalize_leetcode_username(body.leetcode_username)
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"leetcode_username": cleaned}},
    )
    return {"ok": True, "leetcode_username": cleaned}


# ---------- CSV Import / Export ----------
@api.get("/students/export/csv")
async def export_students_csv(request: Request):
    await require_admin_or_faculty(request)
    user = await get_current_user(request)
    q = {"role": "student"}
    if user["role"] == "faculty":
        q["faculty_id"] = str(user["_id"])
    docs = await db.users.find(q).to_list(5000)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Roll Number", "Name", "Email", "Mobile", "Year", "Semester",
                     "LeetCode Username", "Easy", "Medium", "Hard", "Total Solved",
                     "Contest Rating", "Last Synced"])
    for d in docs:
        s = d.get("leetcode_stats") or {}
        writer.writerow([
            d.get("roll_number", ""), d.get("name", ""), d.get("email", ""),
            d.get("mobile", ""), d.get("year", ""), d.get("semester", ""),
            d.get("leetcode_username", ""),
            s.get("easy", 0), s.get("medium", 0), s.get("hard", 0),
            s.get("total_solved", 0), s.get("contest_rating") or "",
            d.get("last_synced_at", ""),
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=students.csv"},
    )


@api.get("/students/import/template")
async def import_template(request: Request):
    """Return a ready-to-fill CSV template with headers and one example row."""
    await require_admin(request)
    headers = [
        "roll_number", "name", "email", "password", "mobile",
        "department_code", "branch_code", "section_name",
        "year", "semester", "leetcode_username",
    ]
    example = [
        "22CSE001", "Ravi Kumar", "ravi@college.edu", "Student@123", "9876543210",
        "CSE", "CSE-A", "A", "1", "1", "nSKWHoKvyX",
    ]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    writer.writerow(example)
    out.seek(0)
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=students_template.csv"},
    )


@api.post("/students/import/csv")
async def import_students_csv(request: Request):
    await require_admin(request)
    body = await request.body()
    text = body.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))

    # Pre-fetch dept/branch/section for code lookup
    deps = {d["code"].upper(): str(d["_id"]) for d in await db.departments.find().to_list(2000)}
    branches = {b["code"].upper(): str(b["_id"]) for b in await db.branches.find().to_list(2000)}
    sections_by_name = {}
    for s in await db.sections.find().to_list(5000):
        sections_by_name.setdefault(s["name"].upper(), []).append(s)

    def get_row(row, *keys):
        for k in keys:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return str(v).strip()
        return None

    created = 0
    failed = []
    for row in reader:
        try:
            roll = get_row(row, "roll_number", "Roll Number", "roll no", "Roll No")
            name = get_row(row, "name", "Name")
            email = (get_row(row, "email", "Email") or "").lower()
            if not roll or not email or not name:
                failed.append({"row": row, "reason": "Missing required field (roll_number/name/email)"})
                continue
            if await db.users.find_one({"email": email}):
                failed.append({"row": row, "reason": "Email exists"})
                continue

            # Resolve department/branch/section — accept either code+name or raw id
            dep_id = get_row(row, "department_id")
            dep_code = (get_row(row, "department_code", "Department", "department") or "").upper()
            if not dep_id and dep_code:
                dep_id = deps.get(dep_code)

            br_id = get_row(row, "branch_id")
            br_code = (get_row(row, "branch_code", "Branch", "branch") or "").upper()
            if not br_id and br_code:
                br_id = branches.get(br_code)

            sec_id = get_row(row, "section_id")
            sec_name = (get_row(row, "section_name", "Section", "section") or "").upper()
            if not sec_id and sec_name:
                # Prefer a section that belongs to the resolved branch
                candidates = sections_by_name.get(sec_name) or []
                match = next((s for s in candidates if br_id and str(s.get("branch_id")) == br_id), None) or (candidates[0] if candidates else None)
                if match:
                    sec_id = str(match["_id"])

            doc = {
                "email": email,
                "password_hash": hash_password(get_row(row, "password") or "Student@123"),
                "name": name,
                "role": "student",
                "roll_number": roll,
                "mobile": get_row(row, "mobile", "Mobile"),
                "department_id": dep_id,
                "branch_id": br_id,
                "section_id": sec_id,
                "year": int(get_row(row, "year", "Year") or 1),
                "semester": int(get_row(row, "semester", "Semester") or 1),
                "leetcode_username": normalize_leetcode_username(get_row(row, "leetcode_username", "LeetCode Username", "leetcode") or ""),
                "leetcode_stats": None,
                "created_at": now_iso(),
            }
            await db.users.insert_one(doc)
            created += 1
        except Exception as e:
            failed.append({"row": row, "reason": str(e)})
    return {"created": created, "failed": failed}


# ---------- LeetCode Sync ----------
@api.post("/sync/leetcode")
async def sync_leetcode(request: Request):
    """Start a manual LeetCode sync for all students. Runs in background."""
    await require_admin(request)
    # Create a sync log
    log_doc = {
        "started_at": now_iso(),
        "completed_at": None,
        "status": "running",
        "total": 0,
        "success": 0,
        "failed": 0,
        "logs": [],
    }
    res = await db.sync_logs.insert_one(log_doc)
    log_id = res.inserted_id
    asyncio.create_task(_run_sync(log_id))
    return {"sync_id": str(log_id)}


async def _run_sync(log_id):
    students = await db.users.find(
        {"role": "student", "leetcode_username": {"$nin": [None, ""]}}
    ).to_list(10000)
    total = len(students)
    success = 0
    failed = 0
    logs: List[str] = []
    await db.sync_logs.update_one({"_id": log_id}, {"$set": {"total": total}})
    for s in students:
        uname = s.get("leetcode_username")
        try:
            stats = await fetch_leetcode_stats(uname)
            if stats is None:
                failed += 1
                logs.append(f"[FAIL] {uname}: user not found")
            elif stats.get("__error__"):
                failed += 1
                logs.append(f"[ERROR] {uname}: {stats['__error__']}")
            else:
                await db.users.update_one(
                    {"_id": s["_id"]},
                    {"$set": {"leetcode_stats": stats, "last_synced_at": now_iso()}},
                )
                success += 1
                logs.append(f"[OK] {uname}: {stats.get('total_solved', 0)} solved")
        except Exception as e:
            failed += 1
            logs.append(f"[EXC] {uname}: {e}")
        # Update log periodically
        await db.sync_logs.update_one(
            {"_id": log_id},
            {"$set": {"success": success, "failed": failed, "logs": logs[-200:]}},
        )
        await asyncio.sleep(0.5)  # be gentle with LeetCode API
    await db.sync_logs.update_one(
        {"_id": log_id},
        {"$set": {"status": "completed", "completed_at": now_iso(),
                  "success": success, "failed": failed, "logs": logs[-200:]}},
    )


@api.get("/sync/status/{sync_id}")
async def sync_status(sync_id: str, request: Request):
    await require_admin(request)
    doc = await db.sync_logs.find_one({"_id": oid(sync_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Sync not found")
    return sid(doc)


@api.get("/sync/logs")
async def sync_logs(request: Request, limit: int = 20):
    await require_admin(request)
    docs = await db.sync_logs.find().sort("started_at", -1).limit(limit).to_list(limit)
    return [sid(d) for d in docs]


@api.post("/sync/student/{sid_}")
async def sync_single_student(sid_: str, request: Request):
    """Sync a single student's LeetCode data on demand."""
    user = await get_current_user(request)
    d = await db.users.find_one({"_id": oid(sid_), "role": "student"})
    if not d:
        raise HTTPException(status_code=404, detail="Student not found")
    if user["role"] == "student" and str(user["_id"]) != sid_:
        raise HTTPException(status_code=403, detail="Forbidden")
    uname = d.get("leetcode_username")
    if not uname:
        raise HTTPException(status_code=400, detail="No LeetCode username set")
    stats = await fetch_leetcode_stats(uname)
    if stats is None:
        raise HTTPException(status_code=404, detail="LeetCode user not found")
    if stats.get("__error__"):
        raise HTTPException(status_code=502, detail=stats["__error__"])
    await db.users.update_one(
        {"_id": d["_id"]},
        {"$set": {"leetcode_stats": stats, "last_synced_at": now_iso()}},
    )
    return {"stats": stats}


# ---------- Dashboard Analytics ----------
@api.get("/analytics/summary")
async def analytics_summary(request: Request):
    user = await get_current_user(request)
    q = {"role": "student"}
    if user["role"] == "faculty":
        q["faculty_id"] = str(user["_id"])

    total = await db.users.count_documents(q)
    with_stats = await db.users.count_documents({**q, "leetcode_stats": {"$ne": None}})
    inactive = total - with_stats

    # Aggregate totals
    pipeline = [
        {"$match": {**q, "leetcode_stats": {"$ne": None}}},
        {"$group": {
            "_id": None,
            "easy": {"$sum": "$leetcode_stats.easy"},
            "medium": {"$sum": "$leetcode_stats.medium"},
            "hard": {"$sum": "$leetcode_stats.hard"},
            "total_solved": {"$sum": "$leetcode_stats.total_solved"},
        }}
    ]
    agg = await db.users.aggregate(pipeline).to_list(1)
    totals = agg[0] if agg else {"easy": 0, "medium": 0, "hard": 0, "total_solved": 0}
    totals.pop("_id", None)

    avg_per_student = round(totals["total_solved"] / with_stats, 1) if with_stats else 0

    # Top performers
    top = await db.users.find(
        {**q, "leetcode_stats.total_solved": {"$gt": 0}}
    ).sort("leetcode_stats.total_solved", -1).limit(5).to_list(5)

    return {
        "total_students": total,
        "active_students": with_stats,
        "inactive_students": inactive,
        "totals": totals,
        "avg_per_student": avg_per_student,
        "top_performers": [
            {
                "id": str(t["_id"]), "name": t.get("name"),
                "roll_number": t.get("roll_number"),
                "leetcode_username": t.get("leetcode_username"),
                "total_solved": (t.get("leetcode_stats") or {}).get("total_solved", 0),
                "easy": (t.get("leetcode_stats") or {}).get("easy", 0),
                "medium": (t.get("leetcode_stats") or {}).get("medium", 0),
                "hard": (t.get("leetcode_stats") or {}).get("hard", 0),
            } for t in top
        ],
    }


@api.get("/analytics/by-department")
async def by_department(request: Request):
    await require_admin_or_faculty(request)
    pipeline = [
        {"$match": {"role": "student", "leetcode_stats": {"$ne": None}}},
        {"$group": {
            "_id": "$department_id",
            "students": {"$sum": 1},
            "easy": {"$sum": "$leetcode_stats.easy"},
            "medium": {"$sum": "$leetcode_stats.medium"},
            "hard": {"$sum": "$leetcode_stats.hard"},
            "total_solved": {"$sum": "$leetcode_stats.total_solved"},
        }}
    ]
    rows = await db.users.aggregate(pipeline).to_list(500)
    departments = {str(d["_id"]): d for d in await db.departments.find().to_list(500)}
    out = []
    for r in rows:
        dep = departments.get(r.get("_id") or "", {})
        out.append({
            "department_id": r.get("_id"),
            "department_name": dep.get("name", "Unassigned"),
            "students": r["students"],
            "easy": r["easy"], "medium": r["medium"], "hard": r["hard"],
            "total_solved": r["total_solved"],
        })
    return out


@api.get("/analytics/by-year")
async def by_year(request: Request):
    await require_admin_or_faculty(request)
    pipeline = [
        {"$match": {"role": "student", "leetcode_stats": {"$ne": None}}},
        {"$group": {
            "_id": "$year",
            "students": {"$sum": 1},
            "total_solved": {"$sum": "$leetcode_stats.total_solved"},
        }},
        {"$sort": {"_id": 1}},
    ]
    rows = await db.users.aggregate(pipeline).to_list(20)
    return [{"year": r.get("_id"), "students": r["students"], "total_solved": r["total_solved"]} for r in rows]


# ---------- Leaderboard ----------
@api.get("/leaderboard")
async def leaderboard(
    request: Request,
    scope: str = "college",
    department_id: Optional[str] = None,
    section_id: Optional[str] = None,
    faculty_id: Optional[str] = None,
    limit: int = 100,
):
    user = await get_current_user(request)
    q = {"role": "student", "leetcode_stats": {"$ne": None}}
    if scope == "department" and department_id:
        q["department_id"] = department_id
    elif scope == "section" and section_id:
        q["section_id"] = section_id
    elif scope == "faculty" and faculty_id:
        q["faculty_id"] = faculty_id
    elif user["role"] == "faculty" and scope == "my":
        q["faculty_id"] = str(user["_id"])

    docs = await db.users.find(q).sort("leetcode_stats.total_solved", -1).limit(limit).to_list(limit)
    out = []
    for i, d in enumerate(docs):
        s = d.get("leetcode_stats") or {}
        out.append({
            "rank": i + 1,
            "id": str(d["_id"]),
            "name": d.get("name"),
            "roll_number": d.get("roll_number"),
            "leetcode_username": d.get("leetcode_username"),
            "department_id": d.get("department_id"),
            "section_id": d.get("section_id"),
            "total_solved": s.get("total_solved", 0),
            "easy": s.get("easy", 0),
            "medium": s.get("medium", 0),
            "hard": s.get("hard", 0),
            "contest_rating": s.get("contest_rating"),
            "avatar": s.get("avatar"),
        })
    return out


# ---------- Reports ----------
@api.get("/reports/students")
async def report_students(
    request: Request,
    format: str = "csv",
    section_id: Optional[str] = None,
    department_id: Optional[str] = None,
):
    await require_admin_or_faculty(request)
    user = await get_current_user(request)
    q = {"role": "student"}
    if user["role"] == "faculty":
        q["faculty_id"] = str(user["_id"])
    if section_id:
        q["section_id"] = section_id
    if department_id:
        q["department_id"] = department_id
    docs = await db.users.find(q).to_list(5000)
    rows = []
    for d in docs:
        s = d.get("leetcode_stats") or {}
        rows.append([
            d.get("roll_number", ""), d.get("name", ""), d.get("email", ""),
            d.get("leetcode_username", ""), s.get("easy", 0), s.get("medium", 0),
            s.get("hard", 0), s.get("total_solved", 0), s.get("contest_rating") or "",
        ])
    headers = ["Roll No", "Name", "Email", "LeetCode", "Easy", "Medium", "Hard", "Total", "Contest Rating"]

    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(rows)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=student_report.csv"},
        )
    elif format == "xlsx":
        wb = Workbook()
        ws = wb.active
        ws.title = "Student Report"
        ws.append(headers)
        for r in rows:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=student_report.xlsx"},
        )
    elif format == "pdf":
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter)
        styles = getSampleStyleSheet()
        elements = [Paragraph("Student LeetCode Progress Report", styles["Title"])]
        data = [headers] + rows
        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#18181b")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
        ]))
        elements.append(table)
        doc.build(elements)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/pdf",
            headers={"Content-Disposition": "attachment; filename=student_report.pdf"},
        )
    raise HTTPException(status_code=400, detail="Unsupported format")


# ---------- Settings ----------
@api.get("/settings")
async def get_settings(request: Request):
    await get_current_user(request)
    doc = await db.settings.find_one({"_id": "global"}) or {}
    doc.pop("_id", None)
    return doc


@api.put("/settings")
async def update_settings(body: SettingsIn, request: Request):
    await require_admin(request)
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    if upd:
        await db.settings.update_one({"_id": "global"}, {"$set": upd}, upsert=True)
    doc = await db.settings.find_one({"_id": "global"}) or {}
    doc.pop("_id", None)
    return doc


@api.get("/")
async def root():
    return {"status": "ok", "service": "LeetCode Tracker", "version": "v2_cors_fix"}


# Include the router
app.include_router(api)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
