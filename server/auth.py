"""
Authentication, RBAC, and Audit Trail module for Chore Robotics Fleet Platform.
"""
import os
import hmac
import hashlib
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import jwt
from fastapi import Request, HTTPException, Depends, status
from pydantic import BaseModel

# Configuration
JWT_SECRET = os.environ.get("CHORE_JWT_SECRET", "chorerobotics_super_secret_jwt_key_2026_x89a")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# --- Role & Permission Definitions ---

ROLE_PERMISSIONS: Dict[str, List[str]] = {
    "super_admin": [
        "*",  # All OEM engineering and platform permissions
    ],
    "release_engineer": [
        "fleet:read",
        "firmware:upload",
        "firmware:delete",
        "deploy:trigger",
        "deploy:rollback",
        "remoteops:reboot",
        "telemetry:read",
        "faults:read",
        "audit:read",
    ],
    "firmware_engineer": [
        "fleet:read",
        "device:manage",
        "firmware:upload",
        "deploy:trigger",
        "deploy:rollback",
        "diagnostics:selftest",
        "remoteops:reboot",
        "remoteops:dock",
        "telemetry:read",
        "faults:read",
        "audit:read",
    ],
    "diagnostics_engineer": [
        "fleet:read",
        "device:manage",
        "diagnostics:selftest",
        "remoteops:reboot",
        "remoteops:dock",
        "telemetry:read",
        "faults:read",
        "audit:read",
    ],
    "viewer": [
        "fleet:read",
        "telemetry:read",
        "faults:read",
        "audit:read",
    ],
}

ROLE_METADATA: Dict[str, Dict[str, str]] = {
    "super_admin": {
        "title": "Super Admin (OEM)",
        "description": "Full system control, global firmware channels, and platform administration",
        "badge_color": "#F7941D",
    },
    "release_engineer": {
        "title": "Release Engineer",
        "description": "Firmware package signing, multi-channel OTA uploads, and rollout control",
        "badge_color": "#8B5CF6",
    },
    "firmware_engineer": {
        "title": "Firmware Engineer",
        "description": "CAN bus module microcode, Jetson compute layers, and hardware validation",
        "badge_color": "#3B82F6",
    },
    "diagnostics_engineer": {
        "title": "Diagnostics Engineer",
        "description": "Field telemetry analysis, DTC fault codes, and CAN bus signal debugging",
        "badge_color": "#10B981",
    },
    "viewer": {
        "title": "QA / Compliance Auditor",
        "description": "Read-only access to fleet health, version distribution, and security audit logs",
        "badge_color": "#94A3B8",
    },
}

# --- Password Hashing (PBKDF2 HMAC SHA-256) ---

def hash_password(password: str, salt: Optional[str] = None) -> str:
    if not salt:
        salt = os.urandom(16).hex()
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000
    )
    return f"{salt}:{key.hex()}"

def verify_password(plain_password: str, hashed: str) -> bool:
    try:
        salt, key_hex = hashed.split(":")
        computed = hashlib.pbkdf2_hmac(
            "sha256",
            plain_password.encode("utf-8"),
            salt.encode("utf-8"),
            100_000
        )
        return hmac.compare_digest(computed.hex(), key_hex)
    except Exception:
        return False

# --- Pre-Seeded User Database ---

USERS_DB: Dict[str, Dict[str, Any]] = {
    "admin@chorerobotics.com": {
        "id": "usr_super_admin_001",
        "email": "admin@chorerobotics.com",
        "name": "Alex Vance",
        "password_hash": hash_password("admin123", "salt_super_admin_01"),
        "role": "super_admin",
        "org_id": "org_chore_oem",
        "org_name": "Chore Robotics OEM Headquarters",
        "created_at": "2026-01-01T00:00:00Z",
    },
    "release@chorerobotics.com": {
        "id": "usr_release_eng_002",
        "email": "release@chorerobotics.com",
        "name": "Sarah Chen",
        "password_hash": hash_password("release123", "salt_release_eng_02"),
        "role": "release_engineer",
        "org_id": "org_chore_oem",
        "org_name": "Chore Robotics OEM Headquarters",
        "created_at": "2026-01-15T00:00:00Z",
    },
    "firmware@chorerobotics.com": {
        "id": "usr_firmware_eng_003",
        "email": "firmware@chorerobotics.com",
        "name": "Marcus Brody",
        "password_hash": hash_password("firmware123", "salt_firmware_eng_03"),
        "role": "firmware_engineer",
        "org_id": "org_chore_oem",
        "org_name": "Chore Robotics OEM Headquarters",
        "created_at": "2026-02-01T00:00:00Z",
    },
    "diagnostics@chorerobotics.com": {
        "id": "usr_diag_eng_004",
        "email": "diagnostics@chorerobotics.com",
        "name": "Dave Miller",
        "password_hash": hash_password("diag123", "salt_diag_eng_04"),
        "role": "diagnostics_engineer",
        "org_id": "org_chore_oem",
        "org_name": "Chore Robotics OEM Headquarters",
        "created_at": "2026-02-10T00:00:00Z",
    },
    "qa@chorerobotics.com": {
        "id": "usr_qa_auditor_005",
        "email": "qa@chorerobotics.com",
        "name": "Elena Rostova",
        "password_hash": hash_password("qa123", "salt_qa_auditor_05"),
        "role": "viewer",
        "org_id": "org_chore_oem",
        "org_name": "Chore Robotics OEM Headquarters",
        "created_at": "2026-02-15T00:00:00Z",
    },
}

# --- Audit Trail Store ---

AUDIT_LOGS: List[Dict[str, Any]] = [
    {
        "audit_id": "aud_seed_001",
        "timestamp": "2026-08-25T18:00:00Z",
        "actor_email": "admin@chorerobotics.com",
        "actor_name": "Alex Vance",
        "actor_role": "super_admin",
        "action": "system:bootstrap",
        "resource_type": "platform",
        "resource_id": "global",
        "status": "SUCCESS",
        "details": "ChoreRobotics RBAC & Security Subsystem initialized",
        "ip_address": "127.0.0.1",
    }
]

def log_audit_event(
    actor: Optional[Dict[str, Any]],
    action: str,
    resource_type: str,
    resource_id: str,
    status: str = "SUCCESS",
    details: str = "",
    ip: str = "127.0.0.1"
) -> Dict[str, Any]:
    event = {
        "audit_id": f"aud_{int(datetime.now().timestamp() * 1000)}",
        "timestamp": datetime.now().isoformat(),
        "actor_email": actor.get("email", "anonymous") if actor else "anonymous/system",
        "actor_name": actor.get("name", "System") if actor else "System",
        "actor_role": actor.get("role", "unauthenticated") if actor else "unauthenticated",
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "status": status,
        "details": details,
        "ip_address": ip,
    }
    AUDIT_LOGS.insert(0, event)
    # Keep last 500 audit entries
    if len(AUDIT_LOGS) > 500:
        AUDIT_LOGS.pop()
    return event

# --- JWT Token Helpers ---

def create_access_token(user: Dict[str, Any]) -> str:
    now = datetime.utcnow()
    exp = now + timedelta(hours=JWT_EXPIRATION_HOURS)
    payload = {
        "sub": user["id"],
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "org_id": user["org_id"],
        "org_name": user["org_name"],
        "permissions": ROLE_PERMISSIONS.get(user["role"], []),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except Exception:
        return None

# --- FastAPI Authentication & Authorization Dependencies ---

class LoginRequest(BaseModel):
    email: str
    password: str

def get_current_user_optional(request: Request) -> Optional[Dict[str, Any]]:
    """Extracts user from Authorization Bearer header or access_token cookie."""
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "access_token" in request.cookies:
        token = request.cookies.get("access_token")

    if not token:
        return None

    payload = decode_access_token(token)
    if not payload:
        return None

    # Return sanitized user representation
    return {
        "id": payload.get("sub"),
        "email": payload.get("email"),
        "name": payload.get("name"),
        "role": payload.get("role"),
        "org_id": payload.get("org_id"),
        "org_name": payload.get("org_name"),
        "permissions": payload.get("permissions", []),
    }

def get_current_user(request: Request) -> Dict[str, Any]:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user

def require_permission(permission: str):
    """
    Dependency factory checking if the authenticated user has the required permission.
    Super Admins with '*' automatically pass all checks.
    """
    def permission_checker(user: Dict[str, Any] = Depends(get_current_user)):
        user_perms = user.get("permissions", [])
        if "*" in user_perms or permission in user_perms:
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access Denied: Action requires '{permission}' permission. Your role is '{user.get('role')}'.",
        )
    return permission_checker
