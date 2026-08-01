"""JWT + bcrypt authentication utilities."""
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt
import jwt
from fastapi import HTTPException, Request, Depends
from bson import ObjectId

JWT_ALGORITHM = "HS256"


def get_jwt_secret() -> str:
    return os.environ["JWT_SECRET"]


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=8),
        "type": "access",
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
        "type": "refresh",
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])


def user_to_public(user: dict) -> dict:
    return {
        "id": str(user["_id"]),
        "email": user.get("email"),
        "name": user.get("name"),
        "role": user.get("role"),
        "department_id": user.get("department_id"),
        "branch_id": user.get("branch_id"),
        "section_id": user.get("section_id"),
        "roll_number": user.get("roll_number"),
        "mobile": user.get("mobile"),
        "profile_photo": user.get("profile_photo"),
        "leetcode_username": user.get("leetcode_username"),
    }


async def get_current_user_from_request(request: Request, required: bool = True) -> dict:
    """Extract token from cookie or Authorization header, verify against DB."""
    from server import db  # local import to avoid circular

    guest_user = {
        "_id": ObjectId("000000000000000000000000"),
        "role": "admin",
        "name": "Public Guest",
        "email": "guest@public.com",
    }

    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        if not required:
            return guest_user
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            if not required:
                return guest_user
            raise HTTPException(status_code=401, detail="Invalid token type")
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
        if not user:
            if not required:
                return guest_user
            raise HTTPException(status_code=401, detail="User not found")
        return user
    except jwt.ExpiredSignatureError:
        if not required:
            return guest_user
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        if not required:
            return guest_user
        raise HTTPException(status_code=401, detail="Invalid token")


async def require_role(request: Request, allowed_roles: list) -> dict:
    user = await get_current_user_from_request(request)
    if user.get("role") not in allowed_roles:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def require_admin(request: Request):
    async def _dep():
        return await require_role(request, ["admin"])
    return _dep()
