"""Pydantic models for the LeetCode Tracker."""
from datetime import datetime, timezone
from typing import Optional, List, Any
from pydantic import BaseModel, EmailStr, Field


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- Auth ----------
class LoginIn(BaseModel):
    email: EmailStr
    password: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    role: str
    department_id: Optional[str] = None
    branch_id: Optional[str] = None
    section_id: Optional[str] = None
    roll_number: Optional[str] = None
    mobile: Optional[str] = None
    profile_photo: Optional[str] = None
    leetcode_username: Optional[str] = None


# ---------- Department / Branch / Section ----------
class DepartmentIn(BaseModel):
    name: str
    code: str


class BranchIn(BaseModel):
    name: str
    code: str
    department_id: str


class SectionIn(BaseModel):
    name: str
    branch_id: str
    year: int
    semester: int


# ---------- Faculty ----------
class FacultyIn(BaseModel):
    name: str
    email: EmailStr
    password: str = "Faculty@123"
    mobile: Optional[str] = None
    department_id: str
    assigned_section_ids: List[str] = []


class FacultyUpdate(BaseModel):
    name: Optional[str] = None
    mobile: Optional[str] = None
    department_id: Optional[str] = None
    assigned_section_ids: Optional[List[str]] = None


# ---------- Student ----------
class StudentIn(BaseModel):
    roll_number: str
    name: str
    email: EmailStr
    password: str = "Student@123"
    mobile: Optional[str] = None
    department_id: str
    branch_id: str
    section_id: str
    year: int
    semester: int
    faculty_id: Optional[str] = None
    leetcode_username: Optional[str] = None
    profile_photo: Optional[str] = None


class StudentUpdate(BaseModel):
    name: Optional[str] = None
    mobile: Optional[str] = None
    department_id: Optional[str] = None
    branch_id: Optional[str] = None
    section_id: Optional[str] = None
    year: Optional[int] = None
    semester: Optional[int] = None
    faculty_id: Optional[str] = None
    leetcode_username: Optional[str] = None
    profile_photo: Optional[str] = None


class LeetCodeUsernameIn(BaseModel):
    leetcode_username: str


class SettingsIn(BaseModel):
    college_name: Optional[str] = None
    notification_thresholds: Optional[dict] = None
