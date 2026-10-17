from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect, HTTPException, Request, Response, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
import sys
import os
import json
import glob
import asyncio
import uuid
from typing import Optional, Literal
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from auth import (
        USERS_DB,
        ROLE_PERMISSIONS,
        ROLE_METADATA,
        AUDIT_LOGS,
        LoginRequest,
        create_access_token,
        get_current_user,
        get_current_user_optional,
        require_permission,
        log_audit_event,
        verify_password,
    )
except ImportError:
    from server.auth import (
        USERS_DB,
        ROLE_PERMISSIONS,
        ROLE_METADATA,
        AUDIT_LOGS,
        LoginRequest,
        create_access_token,
        get_current_user,
        get_current_user_optional,
        require_permission,
        log_audit_event,
        verify_password,
    )

from deployments import deployment_preview, valid_version
from storage import atomic_write_json, store_release

app = FastAPI(title="Chore Robotics OTA Server")

STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.abspath(os.environ.get("CHORE_DATA_DIR", BASE_DIR))
UPDATE_DIR = os.path.join(DATA_DIR, "updates")
JETSON_UPDATE_DIR = os.path.join(UPDATE_DIR, "jetson")
DB_FILE = os.path.join(DATA_DIR, "fleet_db.json")
ONLINE_THRESHOLD_SECONDS = 20  # robot/attachment considered offline if no check-in within this window

os.makedirs(UPDATE_DIR, exist_ok=True)
os.makedirs(JETSON_UPDATE_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/updates", StaticFiles(directory=UPDATE_DIR), name="updates")

# --- Persistence ---

# Baseline attachment firmware versions, seeded on first boot (no real files needed).
_ATTACHMENT_VERSION_SEED = [
    ("SP", "1.1.0"),
    ("LM", "2.4.0"),
    ("LM", "2.4.1"),
    ("SR", "1.0.2"),
    ("LC", "3.0.0"),
]

def default_attachment_versions() -> list[dict]:
    now = datetime.now().isoformat()
    return [
        {"type_code": tc, "version": v, "filename": None, "size": None, "uploaded_at": now}
        for tc, v in _ATTACHMENT_VERSION_SEED
    ]

def load_state() -> dict:
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {}
    if "robots" not in data and "attachments" not in data:
        # Migrate old flat shape: the whole file used to be {robot_id: {...}, ...}.
        data = {"robots": data}
    data.setdefault("robots", {})
    data.setdefault("attachments", {})
    data.setdefault("attachment_versions", default_attachment_versions())
    data.setdefault("attachment_targets", {})
    data.setdefault("jetson_target_version", "")
    data.setdefault("current_target_version", "")
    data.setdefault("commands", [])
    data.setdefault("diagnostic_policy", {
        "telemetry_interval": "Standard (60s)",
        "can_log_level": "Errors & Faults Only",
        "crash_dump_policy": "Automatic Upload on Panic"
    })
    # Migrate robots persisted before the jetson module firmware channel existed.
    for r in data["robots"].values():
        r.setdefault("jetson_version", "1.0.0")
        r.setdefault("jetson_pending_version", None)
        r.setdefault("jetson_update_status", "idle")
    return data

def save_fleet():
    atomic_write_json(DB_FILE, {
        "robots": robots,
        "attachments": attachments,
        "attachment_versions": attachment_versions,
        "attachment_targets": attachment_targets,
        "jetson_target_version": jetson_target_version,
        "current_target_version": current_target_version,
        "commands": commands[-50:],
        "diagnostic_policy": diagnostic_policy,
    })

_state = load_state()
robots: dict[str, dict] = _state["robots"]
attachments: dict[str, dict] = _state["attachments"]
attachment_versions: list[dict] = _state["attachment_versions"]
attachment_targets: dict[str, str] = _state["attachment_targets"]
jetson_target_version: str = _state["jetson_target_version"]
commands: list[dict] = _state.get("commands", [])
diagnostic_policy: dict = _state.get("diagnostic_policy", {
    "telemetry_interval": "Standard (60s)",
    "can_log_level": "Errors & Faults Only",
    "crash_dump_policy": "Automatic Upload on Panic"
})
current_target_version = _state["current_target_version"]
active_connections: list[WebSocket] = []

# --- Models ---

COMPATIBILITY_RULES = {
    "SP": {
        "type": "Snow Plower",
        "supported_hw_revs": ["A", "B"],
        "min_fw": "1.1.0",
        "description": "High-torque winter plowing blade (Rev A & B base chassis)"
    },
    "LM": {
        "type": "Lawn Mower",
        "supported_hw_revs": ["A", "B", "C"],
        "min_fw": "2.4.0",
        "description": "Dual rotary precision mower attachment (Universal)"
    },
    "SR": {
        "type": "Snow Remover",
        "supported_hw_revs": ["B", "C"],
        "min_fw": "1.0.0",
        "description": "Heavy-duty rotary snow blower (Requires Rev B/C high-voltage bus)"
    },
    "LC": {
        "type": "Leaf Collector",
        "supported_hw_revs": ["A", "B", "C"],
        "min_fw": "3.0.0",
        "description": "High-vacuum impeller and bagger attachment (Universal)"
    },
}

class RegisterRequest(BaseModel):
    robot_id: str
    name: str
    model: str = "Unknown"
    version: str
    serial: Optional[str] = None
    hw_rev: Optional[str] = "A"
    carrier_rev: Optional[str] = "1.2"
    jetson_serial: Optional[str] = None
    jetson_version: Optional[str] = None
    lifecycle: Optional[str] = "active"
    tags: Optional[list[str]] = None
    location: Optional[str] = "Main Depot"
    notes: Optional[str] = ""

class AckRequest(BaseModel):
    robot_id: str
    status: str  # "triggered" | "idle"
    version: Optional[str] = None

class DeployRequest(BaseModel):
    version: str
    robot_ids: Optional[list[str]] = None  # None = all known robots

class JetsonAckRequest(BaseModel):
    robot_id: str
    status: str  # "triggered" | "idle"
    version: Optional[str] = None

class JetsonDeployRequest(BaseModel):
    version: str
    robot_ids: Optional[list[str]] = None  # None = all known robots

class AttachmentRegisterRequest(BaseModel):
    attachment_id: str
    type: str
    type_code: str
    version: str
    attached_to: Optional[str] = None
    lifecycle: Optional[str] = "active"
    notes: Optional[str] = ""

class AttachmentAckRequest(BaseModel):
    attachment_id: str
    status: str  # "triggered" | "idle"
    version: Optional[str] = None

class AttachmentDeployRequest(BaseModel):
    version: str
    type_code: str
    attachment_ids: Optional[list[str]] = None  # None = all attachments of that type

class DeploymentPreviewRequest(BaseModel):
    channel: Literal["robot", "jetson", "attachment"]
    version: str
    device_ids: Optional[list[str]] = None
    type_code: Optional[str] = None

class DemoStageRequest(BaseModel):
    stage: int
    title: str
    section: str
    caption: str

class RobotUpdateRequest(BaseModel):
    name: Optional[str] = None
    lifecycle: Optional[str] = None  # active | provisioning | maintenance | decommissioned
    location: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[list[str]] = None

class RobotTagRequest(BaseModel):
    tag: Optional[str] = None
    tags: Optional[list[str]] = None

class AttachmentPairRequest(BaseModel):
    robot_id: str
    force: bool = False

class AttachmentUpdateRequest(BaseModel):
    lifecycle: Optional[str] = None  # active | maintenance | decommissioned | inventory
    notes: Optional[str] = None

class DecommissionRequest(BaseModel):
    reason: Optional[str] = "Operator decommissioning"

class CommandRequest(BaseModel):
    command: str  # "selftest" | "reboot" | "can_query" | "fetch_logs" | "blink_led" | "dock" | "partition_reset"
    parameters: Optional[dict] = None

class CommandAckRequest(BaseModel):
    robot_id: str
    status: str  # "delivering" | "executing" | "completed" | "failed"
    progress: Optional[int] = 0
    result: Optional[dict] = None
    logs: Optional[list[str]] = None

class DiagnosticPolicyRequest(BaseModel):
    telemetry_interval: Optional[str] = "Standard (60s)"
    can_log_level: Optional[str] = "Errors & Faults Only"
    crash_dump_policy: Optional[str] = "Automatic Upload on Panic"

# --- Helpers ---

async def broadcast(data: dict):
    msg = json.dumps(data)
    for ws in list(active_connections):
        try:
            await ws.send_text(msg)
        except Exception:
            if ws in active_connections:
                active_connections.remove(ws)

def scan_uploaded_versions() -> list[dict]:
    versions = []
    for path in glob.glob(os.path.join(UPDATE_DIR, "update_*.tar.gz")):
        fname = os.path.basename(path)
        version = fname[len("update_"):-len(".tar.gz")]
        versions.append({
            "version": version,
            "filename": fname,
            "size": os.path.getsize(path),
            "uploaded_at": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(),
        })
    versions.sort(key=lambda v: v["version"], reverse=True)
    return versions

def scan_jetson_versions() -> list[dict]:
    versions = []
    for path in glob.glob(os.path.join(JETSON_UPDATE_DIR, "jetson_*.tar.gz")):
        fname = os.path.basename(path)
        version = fname[len("jetson_"):-len(".tar.gz")]
        versions.append({
            "version": version,
            "filename": fname,
            "size": os.path.getsize(path),
            "uploaded_at": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(),
        })
    versions.sort(key=lambda v: v["version"], reverse=True)
    return versions

def device_is_online(device: dict) -> bool:
    try:
        seen = datetime.fromisoformat(device["last_seen"])
        age = (datetime.now(seen.tzinfo) - seen).total_seconds()
        return 0 <= age < ONLINE_THRESHOLD_SECONDS
    except (KeyError, TypeError, ValueError):
        return False


def robot_view(robot_id: str, r: dict) -> dict:
    """Compute derived fields (online/offline) at read time with complete device registry defaults."""
    is_online = device_is_online(r)
    out = dict(r)
    out["robot_id"] = robot_id
    out["status"] = "online" if is_online else "offline"
    out["lifecycle"] = r.get("lifecycle", "active")
    out["hw_rev"] = r.get("hw_rev", "A")
    out["carrier_rev"] = r.get("carrier_rev", "1.2")
    out["jetson_serial"] = r.get("jetson_serial", f"NV-JET-2026-{robot_id[-5:]}" if len(robot_id) >= 5 else "NV-JET-2026-00001")
    out["tags"] = r.get("tags", [])
    out["location"] = r.get("location", "Main Depot")
    out["notes"] = r.get("notes", "")
    if "manufactured_date" not in r:
        try:
            first_seen_dt = datetime.fromisoformat(r.get("first_seen", datetime.now().isoformat()))
            out["manufactured_date"] = (first_seen_dt - timedelta(days=45)).strftime("%Y-%m-%d")
        except Exception:
            out["manufactured_date"] = "2026-01-15"
    return out

def attachment_view(attachment_id: str, a: dict) -> dict:
    """Compute derived fields at read time with complete device registry defaults."""
    out = dict(a)
    out["attachment_id"] = attachment_id
    out["serial"] = a.get("serial", attachment_id)
    out["lifecycle"] = a.get("lifecycle", "active" if a.get("attached_to") else "inventory")
    out["notes"] = a.get("notes", "")
    out["paired_history"] = a.get("paired_history", [])
    if a.get("attached_to") is None:
        out["status"] = "inventory"
    else:
        is_online = device_is_online(a)
        out["status"] = "online" if is_online else "offline"
    return out

def robots_snapshot() -> dict:
    return {rid: robot_view(rid, r) for rid, r in robots.items()}

def attachments_snapshot() -> dict:
    return {aid: attachment_view(aid, a) for aid, a in attachments.items()}

def fleet_snapshot() -> dict:
    return {"robots": robots_snapshot(), "attachments": attachments_snapshot()}

async def periodic_broadcast():
    """Robots/attachments that stop polling (crash, killed, network drop) never
    call an endpoint again, so nothing would otherwise trigger a re-broadcast —
    the dashboard would show them as online until some unrelated event fired.
    This loop keeps the online/offline status live regardless."""
    while True:
        await asyncio.sleep(5)
        if active_connections and (robots or attachments):
            await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(periodic_broadcast())

# --- Authentication & RBAC Routes ---

@app.get("/login", response_class=HTMLResponse)
def login_page():
    with open(os.path.join(STATIC_DIR, "login.html"), "r") as f:
        return f.read()

@app.post("/api/auth/login")
def auth_login(req: LoginRequest, response: Response, request: Request):
    user = USERS_DB.get(req.email.lower())
    if not user or not verify_password(req.password, user["password_hash"]):
        log_audit_event(
            None,
            action="auth:login_failed",
            resource_type="user",
            resource_id=req.email,
            status="FAILED",
            details="Invalid email or password attempt",
            ip=request.client.host if request.client else "127.0.0.1"
        )
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(user)
    # Set HTTP-only secure cookie for web browser dashboard navigation
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        max_age=86400,
        samesite="lax",
        path="/"
    )

    log_audit_event(
        user,
        action="auth:login_success",
        resource_type="user",
        resource_id=user["id"],
        status="SUCCESS",
        details=f"User authenticated with role '{user['role']}'",
        ip=request.client.host if request.client else "127.0.0.1"
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "role": user["role"],
            "role_title": ROLE_METADATA.get(user["role"], {}).get("title", user["role"]),
            "role_color": ROLE_METADATA.get(user["role"], {}).get("badge_color", "#F7941D"),
            "org_id": user["org_id"],
            "org_name": user["org_name"],
            "permissions": ROLE_PERMISSIONS.get(user["role"], []),
        }
    }

@app.get("/api/auth/me")
def auth_me(request: Request):
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "role": user["role"],
            "role_title": ROLE_METADATA.get(user["role"], {}).get("title", user["role"]),
            "role_color": ROLE_METADATA.get(user["role"], {}).get("badge_color", "#F7941D"),
            "org_id": user["org_id"],
            "org_name": user["org_name"],
            "permissions": user["permissions"],
        }
    }

@app.post("/api/auth/logout")
def auth_logout(response: Response, request: Request):
    user = get_current_user_optional(request)
    if user:
        log_audit_event(
            user,
            action="auth:logout",
            resource_type="user",
            resource_id=user["id"],
            status="SUCCESS",
            details="User logged out",
            ip=request.client.host if request.client else "127.0.0.1"
        )
    response.delete_cookie(key="access_token", path="/")
    return {"status": "logged_out"}

@app.get("/api/audit/logs")
def get_audit_logs(limit: int = 50):
    return {"logs": AUDIT_LOGS[:limit]}

# --- Dashboard routes ---

@app.get("/", response_class=HTMLResponse)
def read_root():
    with open(os.path.join(STATIC_DIR, "index.html"), "r") as f:
        return f.read()

@app.get("/api/fleet")
def get_fleet():
    return fleet_snapshot()

@app.get("/api/versions")
def get_versions():
    return {"versions": scan_uploaded_versions(), "target": current_target_version}

@app.get("/api/jetson/versions")
def get_jetson_versions():
    return {"versions": scan_jetson_versions(), "target": jetson_target_version}


async def publish_release(file: UploadFile, path: str):
    try:
        return await run_in_threadpool(store_release, file.file, path)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="This version already exists. Use a new version number.")
    except OverflowError as error:
        raise HTTPException(status_code=413, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    finally:
        await file.close()

@app.post("/upload", dependencies=[Depends(require_permission("firmware:upload"))])
async def upload_update(file: UploadFile = File(...), version: str = "", request: Request = None):
    user = get_current_user_optional(request) if request else None
    if user and "firmware:upload" not in user.get("permissions", []) and "*" not in user.get("permissions", []):
        log_audit_event(user, "firmware:upload", "firmware", version, "DENIED", "Unauthorized upload attempt", request.client.host if request.client else "127.0.0.1")
        raise HTTPException(status_code=403, detail="Access Denied: You do not have permission to upload firmware.")

    if not valid_version(version):
        raise HTTPException(status_code=422, detail="Enter a valid release version, such as 1.3.0")
    file_path = os.path.join(UPDATE_DIR, f"update_{version}.tar.gz")
    metadata = await publish_release(file, file_path)
    await broadcast({"type": "versions_update", "versions": scan_uploaded_versions()})

    log_audit_event(
        user,
        action="firmware:upload",
        resource_type="firmware",
        resource_id=f"v{version}",
        status="SUCCESS",
        details=f"Uploaded robot firmware package update_{version}.tar.gz",
        ip=request.client.host if request.client else "127.0.0.1"
    )
    return {"status": "uploaded", "version": version, **metadata}

@app.post("/upload/jetson", dependencies=[Depends(require_permission("firmware:upload"))])
async def upload_jetson_update(file: UploadFile = File(...), version: str = "", request: Request = None):
    user = get_current_user_optional(request) if request else None
    if user and "firmware:upload" not in user.get("permissions", []) and "*" not in user.get("permissions", []):
        log_audit_event(user, "firmware:upload", "jetson_firmware", version, "DENIED", "Unauthorized Jetson upload attempt", request.client.host if request.client else "127.0.0.1")
        raise HTTPException(status_code=403, detail="Access Denied: You do not have permission to upload Jetson module firmware.")

    if not valid_version(version):
        raise HTTPException(status_code=422, detail="Enter a valid release version, such as 1.3.0")
    file_path = os.path.join(JETSON_UPDATE_DIR, f"jetson_{version}.tar.gz")
    metadata = await publish_release(file, file_path)
    await broadcast({"type": "jetson_versions_update", "versions": scan_jetson_versions()})

    log_audit_event(
        user,
        action="firmware:upload",
        resource_type="jetson_firmware",
        resource_id=f"jetson-v{version}",
        status="SUCCESS",
        details=f"Uploaded Jetson compute module firmware jetson_{version}.tar.gz",
        ip=request.client.host if request.client else "127.0.0.1"
    )
    return {"status": "uploaded", "version": version, **metadata}

@app.post("/api/clear", dependencies=[Depends(require_permission("system:manage"))])
async def clear_database(request: Request = None):
    user = get_current_user_optional(request) if request else None
    # Super Admin check for destructive database wipe
    if user and user.get("role") != "super_admin":
        log_audit_event(user, "system:clear_database", "database", "fleet_db", "DENIED", f"User with role '{user.get('role')}' denied from clearing database", request.client.host if request.client else "127.0.0.1")
        raise HTTPException(status_code=403, detail="Access Denied: Only Super Admins can clear the database.")

    global robots, attachments, current_target_version, attachment_targets, jetson_target_version, commands
    robots = {}
    attachments = {}
    current_target_version = ""
    attachment_targets = {}
    jetson_target_version = ""
    commands = []
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    await broadcast({"type": "target_update", "target_version": current_target_version})
    await broadcast({"type": "attachment_target_update", "targets": dict(attachment_targets)})
    await broadcast({"type": "jetson_target_update", "target_version": jetson_target_version})
    await broadcast({"type": "toast", "message": "Database cleared — waiting for robots to connect", "kind": "info"})

    log_audit_event(
        user,
        action="system:clear_database",
        resource_type="database",
        resource_id="fleet_db",
        status="SUCCESS",
        details="Entire fleet and attachment registry wiped by operator",
        ip=request.client.host if request.client else "127.0.0.1"
    )
    return {"status": "cleared"}

def preview_deployment(channel, version, device_ids, type_code=None):
    devices = attachments_snapshot() if channel == "attachment" else robots_snapshot()
    releases = (attachment_versions if channel == "attachment" else
                scan_jetson_versions() if channel == "jetson" else scan_uploaded_versions())
    return deployment_preview(channel, version, device_ids, devices, releases, type_code,
                              hosts=robots_snapshot() if channel == "attachment" else None)


def validated_targets(channel, version, device_ids, type_code=None):
    preview = preview_deployment(channel, version, device_ids, type_code)
    if not preview["can_deploy"]:
        raise HTTPException(status_code=409, detail={
            "message": "Deployment was not queued. Resolve the readiness checks and try again.",
            "preview": preview,
        })
    return preview["targets"]


@app.post("/api/deploy/preview", dependencies=[Depends(require_permission("deploy:trigger"))])
def get_deployment_preview(req: DeploymentPreviewRequest):
    return preview_deployment(req.channel, req.version, req.device_ids, req.type_code)


@app.post("/deploy", dependencies=[Depends(require_permission("deploy:trigger"))])
async def deploy_update(req: DeployRequest, request: Request = None):
    user = get_current_user_optional(request) if request else None
    if user and "deploy:trigger" not in user.get("permissions", []) and "*" not in user.get("permissions", []):
        log_audit_event(user, "deploy:trigger", "firmware", req.version, "DENIED", f"User with role '{user.get('role')}' lacks deploy:trigger permission", request.client.host if request.client else "127.0.0.1")
        raise HTTPException(status_code=403, detail="Access Denied: You do not have permission to trigger firmware rollouts.")

    global current_target_version
    ids = validated_targets("robot", req.version, req.robot_ids)
    current_target_version = req.version
    targets = []
    for rid in ids:
        if rid not in robots:
            continue
        robots[rid]["pending_version"] = req.version
        robots[rid]["update_status"] = "pending"
        targets.append(rid)
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    await broadcast({"type": "target_update", "target_version": current_target_version})
    await broadcast({"type": "toast", "message": f"Update v{req.version} queued for {len(targets)} robot(s)", "kind": "info"})

    log_audit_event(
        user,
        action="deploy:trigger",
        resource_type="robot_firmware",
        resource_id=f"v{req.version}",
        status="SUCCESS",
        details=f"Queued firmware v{req.version} for {len(targets)} robot(s)",
        ip=request.client.host if request.client else "127.0.0.1"
    )
    return {"status": "queued", "targets": targets}

@app.post("/deploy/attachment", dependencies=[Depends(require_permission("deploy:trigger"))])
async def deploy_attachment_update(req: AttachmentDeployRequest, request: Request = None):
    user = get_current_user_optional(request) if request else None
    if user and "deploy:trigger" not in user.get("permissions", []) and "*" not in user.get("permissions", []):
        log_audit_event(user, "deploy:trigger", "attachment_firmware", f"{req.type_code}-v{req.version}", "DENIED", "Unauthorized attachment deploy attempt", request.client.host if request.client else "127.0.0.1")
        raise HTTPException(status_code=403, detail="Access Denied: You do not have permission to trigger attachment firmware updates.")

    candidate_ids = validated_targets("attachment", req.version, req.attachment_ids, req.type_code)
    attachment_targets[req.type_code] = req.version

    targets = []
    for aid in candidate_ids:
        a = attachments.get(aid)
        if not a or a["type_code"] != req.type_code:
            continue
        a["pending_version"] = req.version
        a["update_status"] = "pending"
        targets.append(aid)
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    await broadcast({"type": "attachment_target_update", "targets": dict(attachment_targets)})
    await broadcast({"type": "toast", "message": f"Update v{req.version} queued for {len(targets)} {req.type_code} attachment(s)", "kind": "info"})

    log_audit_event(
        user,
        action="deploy:trigger",
        resource_type="attachment_firmware",
        resource_id=f"{req.type_code}-v{req.version}",
        status="SUCCESS",
        details=f"Queued attachment firmware v{req.version} for {len(targets)} {req.type_code} units",
        ip=request.client.host if request.client else "127.0.0.1"
    )
    return {"status": "queued", "targets": targets}

@app.post("/deploy/jetson", dependencies=[Depends(require_permission("deploy:trigger"))])
async def deploy_jetson_update(req: JetsonDeployRequest, request: Request = None):
    user = get_current_user_optional(request) if request else None
    if user and "deploy:trigger" not in user.get("permissions", []) and "*" not in user.get("permissions", []):
        log_audit_event(user, "deploy:trigger", "jetson_firmware", f"jetson-v{req.version}", "DENIED", "Unauthorized Jetson deploy attempt", request.client.host if request.client else "127.0.0.1")
        raise HTTPException(status_code=403, detail="Access Denied: You do not have permission to trigger Jetson module updates.")

    global jetson_target_version
    ids = validated_targets("jetson", req.version, req.robot_ids)
    jetson_target_version = req.version
    targets = []
    for rid in ids:
        if rid not in robots:
            continue
        robots[rid]["jetson_pending_version"] = req.version
        robots[rid]["jetson_update_status"] = "pending"
        targets.append(rid)
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    await broadcast({"type": "jetson_target_update", "target_version": jetson_target_version})
    await broadcast({"type": "toast", "message": f"Jetson module update v{req.version} queued for {len(targets)} robot(s)", "kind": "info"})

    log_audit_event(
        user,
        action="deploy:trigger",
        resource_type="jetson_firmware",
        resource_id=f"jetson-v{req.version}",
        status="SUCCESS",
        details=f"Queued Jetson compute firmware v{req.version} for {len(targets)} robot(s)",
        ip=request.client.host if request.client else "127.0.0.1"
    )
    return {"status": "queued", "targets": targets}

@app.post("/api/demo/stage")
async def demo_stage(req: DemoStageRequest):
    await broadcast({
        "type": "demo_stage",
        "stage": req.stage,
        "title": req.title,
        "section": req.section,
        "caption": req.caption,
    })
    return {"status": "ok"}

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    active_connections.append(ws)
    await ws.send_text(json.dumps({
        "type": "init",
        "fleet": fleet_snapshot(),
        "versions": scan_uploaded_versions(),
        "attachment_versions": attachment_versions,
        "target_version": current_target_version,
        "attachment_targets": attachment_targets,
        "jetson_versions": scan_jetson_versions(),
        "jetson_target_version": jetson_target_version,
    }))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in active_connections:
            active_connections.remove(ws)

# --- Robot-facing routes ---

# --- Robot-facing routes ---

@app.post("/api/robot/register")
async def robot_register(req: RegisterRequest):
    now = datetime.now().isoformat()
    existing = robots.get(req.robot_id)
    default_tags = ["region:us-west", "cohort:pilot-2026", f"hw-rev:{req.hw_rev or 'a'}".lower()]
    robots[req.robot_id] = {
        "name": req.name,
        "model": req.model,
        "serial": req.serial or req.robot_id,
        "hw_rev": req.hw_rev or "A",
        "carrier_rev": req.carrier_rev or (existing.get("carrier_rev", "1.2") if existing else "1.2"),
        "jetson_serial": req.jetson_serial or (existing.get("jetson_serial") if existing else f"NV-JET-2026-{req.robot_id[-5:]}"),
        "current_version": req.version,
        "pending_version": existing.get("pending_version") if existing else None,
        "update_status": existing.get("update_status", "idle") if existing else "idle",
        "jetson_version": req.jetson_version or (existing.get("jetson_version", "1.0.0") if existing else "1.0.0"),
        "jetson_pending_version": existing.get("jetson_pending_version") if existing else None,
        "jetson_update_status": existing.get("jetson_update_status", "idle") if existing else "idle",
        "lifecycle": req.lifecycle or (existing.get("lifecycle", "active") if existing else "active"),
        "tags": req.tags if req.tags is not None else (existing.get("tags", default_tags) if existing else default_tags),
        "location": req.location or (existing.get("location", "Main Depot") if existing else "Main Depot"),
        "notes": req.notes if req.notes is not None else (existing.get("notes", "") if existing else ""),
        "first_seen": existing.get("first_seen", now) if existing else now,
        "last_seen": now,
    }
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    return {"status": "registered", "robot_id": req.robot_id}

@app.get("/api/robot/check")
async def robot_check(robot_id: str, version: str):
    if robot_id not in robots:
        raise HTTPException(status_code=404, detail="not_registered")
    r = robots[robot_id]
    was_offline = not device_is_online(r)
    r["last_seen"] = datetime.now().isoformat()
    r["current_version"] = version
    pending = r.get("pending_version")
    update_available = bool(pending) and pending != version and r.get("update_status") != "triggered"

    # Check for pending diagnostic/remote commands
    pending_cmds = []
    for c in commands:
        if c.get("robot_id") == robot_id and c.get("status") == "pending":
            c["status"] = "delivering"
            c["delivered_at"] = datetime.now().isoformat()
            pending_cmds.append({
                "command_id": c["command_id"],
                "command": c["command"],
                "parameters": c.get("parameters", {}),
            })
            await broadcast({"type": "command_update", "command": c})

    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    if was_offline:
        await broadcast({"type": "toast", "message": f"{r['name']} back online", "kind": "success"})
    return {
        "update_available": update_available,
        "target_version": pending if update_available else None,
        "pending_commands": pending_cmds,
        "diagnostic_policy": diagnostic_policy,
    }

@app.post("/api/robot/ack")
async def robot_ack(req: AckRequest):
    if req.robot_id not in robots:
        raise HTTPException(status_code=404, detail="not_registered")
    r = robots[req.robot_id]
    r["update_status"] = req.status
    r["last_seen"] = datetime.now().isoformat()
    if req.version:
        r["current_version"] = req.version
    if req.status == "idle":
        r["pending_version"] = None
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    return {"status": "ok"}

@app.get("/api/robot/jetson/check")
async def robot_jetson_check(robot_id: str, version: str):
    """Jetson compute-module firmware channel, independent from the robot's
    main firmware version but scoped to the same robot serial (not a
    separately-inventoried device like an attachment)."""
    if robot_id not in robots:
        raise HTTPException(status_code=404, detail="not_registered")
    r = robots[robot_id]
    r["last_seen"] = datetime.now().isoformat()
    r["jetson_version"] = version
    pending = r.get("jetson_pending_version")
    update_available = bool(pending) and pending != version and r.get("jetson_update_status") != "triggered"
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    return {
        "update_available": update_available,
        "target_version": pending if update_available else None,
    }

@app.post("/api/robot/jetson/ack")
async def robot_jetson_ack(req: JetsonAckRequest):
    if req.robot_id not in robots:
        raise HTTPException(status_code=404, detail="not_registered")
    r = robots[req.robot_id]
    r["jetson_update_status"] = req.status
    r["last_seen"] = datetime.now().isoformat()
    if req.version:
        r["jetson_version"] = req.version
    if req.status == "idle":
        r["jetson_pending_version"] = None
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    return {"status": "ok"}

# --- Attachment-facing routes ---

@app.post("/api/attachment/register")
async def attachment_register(req: AttachmentRegisterRequest):
    now = datetime.now().isoformat()
    existing = attachments.get(req.attachment_id)
    paired_history = existing.get("paired_history", []) if existing else []
    if req.attached_to and (not paired_history or paired_history[-1].get("robot_id") != req.attached_to):
        paired_history.append({
            "robot_id": req.attached_to,
            "paired_at": now,
            "unpaired_at": None,
        })
    attachments[req.attachment_id] = {
        "type": req.type,
        "type_code": req.type_code,
        "serial": req.attachment_id,
        "attached_to": req.attached_to,
        "current_version": req.version,
        "pending_version": existing.get("pending_version") if existing else None,
        "update_status": existing.get("update_status", "idle") if existing else "idle",
        "lifecycle": req.lifecycle or (existing.get("lifecycle", "active" if req.attached_to else "inventory") if existing else ("active" if req.attached_to else "inventory")),
        "notes": req.notes or (existing.get("notes", "") if existing else ""),
        "paired_history": paired_history,
        "first_seen": existing.get("first_seen", now) if existing else now,
        "last_seen": now,
    }
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    return {"status": "registered", "attachment_id": req.attachment_id}

@app.get("/api/attachment/check")
async def attachment_check(attachment_id: str, version: str):
    if attachment_id not in attachments:
        raise HTTPException(status_code=404, detail="not_registered")
    a = attachments[attachment_id]
    was_offline = (
        a.get("attached_to") is not None
        and (datetime.now() - datetime.fromisoformat(a["last_seen"])) >= timedelta(seconds=ONLINE_THRESHOLD_SECONDS)
    )
    a["last_seen"] = datetime.now().isoformat()
    a["current_version"] = version
    pending = a.get("pending_version")
    update_available = bool(pending) and pending != version and a.get("update_status") != "triggered"
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    if was_offline:
        await broadcast({"type": "toast", "message": f"{a['type']} {attachment_id} back online", "kind": "success"})
    return {
        "update_available": update_available,
        "target_version": pending if update_available else None,
    }

@app.post("/api/attachment/ack")
async def attachment_ack(req: AttachmentAckRequest):
    if req.attachment_id not in attachments:
        raise HTTPException(status_code=404, detail="not_registered")
    a = attachments[req.attachment_id]
    a["update_status"] = req.status
    a["last_seen"] = datetime.now().isoformat()
    if req.version:
        a["current_version"] = req.version
    if req.status == "idle":
        a["pending_version"] = None
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    return {"status": "ok"}

# --- Device & Attachment Registry Routes ---

@app.get("/api/devices")
def get_devices(
    q: Optional[str] = None,
    lifecycle: Optional[str] = None,
    hw_rev: Optional[str] = None,
    tag: Optional[str] = None,
    model: Optional[str] = None,
    attachment_type: Optional[str] = None,
):
    """Multi-field search and query endpoint for base robots and attachments."""
    robots_list = list(robots_snapshot().values())
    attachments_list = list(attachments_snapshot().values())

    all_tags = set()
    for r in robots_list:
        for t in r.get("tags", []):
            all_tags.add(t)

    filtered_robots = []
    for r in robots_list:
        if lifecycle and r.get("lifecycle", "active").lower() != lifecycle.lower():
            continue
        if hw_rev and r.get("hw_rev", "A").upper() != hw_rev.upper():
            continue
        if tag and tag not in r.get("tags", []):
            continue
        if model and model.lower() not in r.get("model", "").lower():
            continue
        if q:
            query = q.lower()
            match = (
                query in r.get("name", "").lower()
                or query in r.get("serial", "").lower()
                or query in r.get("robot_id", "").lower()
                or query in r.get("location", "").lower()
                or any(query in t.lower() for t in r.get("tags", []))
            )
            if not match:
                continue
        filtered_robots.append(r)

    filtered_attachments = []
    for a in attachments_list:
        if lifecycle and a.get("lifecycle", "active").lower() != lifecycle.lower():
            continue
        if attachment_type and a.get("type_code", "").upper() != attachment_type.upper() and a.get("type", "").lower() != attachment_type.lower():
            continue
        if q:
            query = q.lower()
            match = (
                query in a.get("serial", "").lower()
                or query in a.get("type", "").lower()
                or query in (a.get("attached_to") or "").lower()
            )
            if not match:
                continue
        filtered_attachments.append(a)

    kpi = {
        "total_robots": len(robots_list),
        "active_robots": sum(1 for r in robots_list if r.get("lifecycle", "active") == "active"),
        "maintenance_robots": sum(1 for r in robots_list if r.get("lifecycle") == "maintenance"),
        "decommissioned_robots": sum(1 for r in robots_list if r.get("lifecycle") == "decommissioned"),
        "total_attachments": len(attachments_list),
        "attached_attachments": sum(1 for a in attachments_list if a.get("attached_to")),
        "inventory_attachments": sum(1 for a in attachments_list if not a.get("attached_to")),
    }

    return {
        "robots": filtered_robots,
        "attachments": filtered_attachments,
        "tags": sorted(list(all_tags)),
        "kpi": kpi,
    }

@app.patch("/api/robot/{robot_id}")
async def update_robot(robot_id: str, req: RobotUpdateRequest, request: Request = None):
    user = get_current_user_optional(request) if request else None
    if user and "fleet:manage" not in user.get("permissions", []) and "*" not in user.get("permissions", []):
        log_audit_event(user, "device:update", "robot", robot_id, "DENIED", "Lacks fleet:manage permission", request.client.host if request.client else "127.0.0.1")
        raise HTTPException(status_code=403, detail="Access Denied: You do not have permission to update devices.")

    if robot_id not in robots:
        raise HTTPException(status_code=404, detail="Robot not found")

    r = robots[robot_id]
    changes = []
    if req.name is not None:
        changes.append(f"name: '{r.get('name')}' -> '{req.name}'")
        r["name"] = req.name
    if req.lifecycle is not None:
        changes.append(f"lifecycle: '{r.get('lifecycle')}' -> '{req.lifecycle}'")
        r["lifecycle"] = req.lifecycle
    if req.location is not None:
        changes.append(f"location: '{r.get('location')}' -> '{req.location}'")
        r["location"] = req.location
    if req.notes is not None:
        r["notes"] = req.notes
    if req.tags is not None:
        changes.append(f"tags: {req.tags}")
        r["tags"] = list(set(req.tags))

    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})

    log_audit_event(
        user,
        action="device:update",
        resource_type="robot",
        resource_id=robot_id,
        status="SUCCESS",
        details="; ".join(changes) if changes else "Updated metadata",
        ip=request.client.host if request.client else "127.0.0.1"
    )
    return {"status": "ok", "robot": robot_view(robot_id, r)}

@app.post("/api/robot/{robot_id}/tags")
async def add_robot_tag(robot_id: str, req: RobotTagRequest, request: Request = None):
    user = get_current_user_optional(request) if request else None
    if robot_id not in robots:
        raise HTTPException(status_code=404, detail="Robot not found")

    r = robots[robot_id]
    if "tags" not in r or not isinstance(r["tags"], list):
        r["tags"] = []

    to_add = []
    if req.tag:
        to_add.append(req.tag.strip())
    if req.tags:
        to_add.extend([t.strip() for t in req.tags if t.strip()])

    added = []
    for t in to_add:
        if t and t not in r["tags"]:
            r["tags"].append(t)
            added.append(t)

    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    log_audit_event(user, "device:tag_add", "robot", robot_id, "SUCCESS", f"Added tags: {added}", request.client.host if request.client else "127.0.0.1")
    return {"status": "ok", "tags": r["tags"]}

@app.delete("/api/robot/{robot_id}/tags/{tag}")
async def remove_robot_tag(robot_id: str, tag: str, request: Request = None):
    user = get_current_user_optional(request) if request else None
    if robot_id not in robots:
        raise HTTPException(status_code=404, detail="Robot not found")

    r = robots[robot_id]
    if "tags" in r and tag in r["tags"]:
        r["tags"].remove(tag)
        save_fleet()
        await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
        log_audit_event(user, "device:tag_remove", "robot", robot_id, "SUCCESS", f"Removed tag: {tag}", request.client.host if request.client else "127.0.0.1")
    return {"status": "ok", "tags": r.get("tags", [])}

@app.post("/api/attachment/{attachment_id}/pair")
async def pair_attachment(attachment_id: str, req: AttachmentPairRequest, request: Request = None):
    user = get_current_user_optional(request) if request else None
    if attachment_id not in attachments:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if req.robot_id not in robots:
        raise HTTPException(status_code=404, detail="Target robot not found")

    a = attachments[attachment_id]
    r = robots[req.robot_id]

    if r.get("lifecycle") == "decommissioned":
        raise HTTPException(status_code=400, detail="Cannot pair attachment to a decommissioned robot")

    # Dynamic hardware compatibility validation
    rule = COMPATIBILITY_RULES.get(a.get("type_code"))
    robot_hw = r.get("hw_rev", "A").upper()
    if rule and robot_hw not in rule["supported_hw_revs"] and not req.force:
        detail_msg = f"Incompatible Hardware: {a.get('type')} requires base chassis Rev {', '.join(rule['supported_hw_revs'])}, but {r.get('name')} is Rev {robot_hw}."
        log_audit_event(user, "device:pair", "attachment", attachment_id, "FAILED", f"Blocked pairing to {req.robot_id}: {detail_msg}", request.client.host if request.client else "127.0.0.1")
        raise HTTPException(status_code=400, detail=detail_msg)

    # Automatically unpair any existing tool attached to this robot
    for other_id, other_att in attachments.items():
        if other_id != attachment_id and other_att.get("attached_to") == req.robot_id:
            other_att["attached_to"] = None
            if "paired_history" in other_att and other_att["paired_history"]:
                if other_att["paired_history"][-1].get("unpaired_at") is None:
                    other_att["paired_history"][-1]["unpaired_at"] = datetime.now().isoformat()

    now = datetime.now().isoformat()
    a["attached_to"] = req.robot_id
    a["last_seen"] = now
    a["lifecycle"] = "active"
    if "paired_history" not in a or not isinstance(a["paired_history"], list):
        a["paired_history"] = []
    a["paired_history"].append({
        "robot_id": req.robot_id,
        "paired_at": now,
        "unpaired_at": None,
    })

    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    await broadcast({"type": "toast", "message": f"Paired {a.get('type')} ({attachment_id}) to {r.get('name')}", "kind": "success"})
    log_audit_event(user, "device:pair", "attachment", attachment_id, "SUCCESS", f"Paired to robot {req.robot_id} ({r.get('name')})", request.client.host if request.client else "127.0.0.1")
    return {"status": "ok", "attachment": attachment_view(attachment_id, a)}

@app.post("/api/attachment/{attachment_id}/unpair")
async def unpair_attachment(attachment_id: str, request: Request = None):
    user = get_current_user_optional(request) if request else None
    if attachment_id not in attachments:
        raise HTTPException(status_code=404, detail="Attachment not found")

    a = attachments[attachment_id]
    prev_robot = a.get("attached_to")
    a["attached_to"] = None
    a["lifecycle"] = "inventory"

    if "paired_history" in a and a["paired_history"]:
        if a["paired_history"][-1].get("unpaired_at") is None:
            a["paired_history"][-1]["unpaired_at"] = datetime.now().isoformat()

    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    await broadcast({"type": "toast", "message": f"Unpaired {a.get('type')} ({attachment_id}) into inventory", "kind": "info"})
    log_audit_event(user, "device:unpair", "attachment", attachment_id, "SUCCESS", f"Unpaired from robot {prev_robot} to inventory", request.client.host if request.client else "127.0.0.1")
    return {"status": "ok", "attachment": attachment_view(attachment_id, a)}

@app.patch("/api/attachment/{attachment_id}")
async def update_attachment(attachment_id: str, req: AttachmentUpdateRequest, request: Request = None):
    user = get_current_user_optional(request) if request else None
    if user and "fleet:manage" not in user.get("permissions", []) and "*" not in user.get("permissions", []):
        log_audit_event(user, "device:update", "attachment", attachment_id, "DENIED", "Lacks fleet:manage permission", request.client.host if request.client else "127.0.0.1")
        raise HTTPException(status_code=403, detail="Access Denied: You do not have permission to update devices.")

    if attachment_id not in attachments:
        raise HTTPException(status_code=404, detail="Attachment not found")

    a = attachments[attachment_id]
    if req.lifecycle is not None:
        a["lifecycle"] = req.lifecycle
    if req.notes is not None:
        a["notes"] = req.notes

    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    log_audit_event(user, "device:update", "attachment", attachment_id, "SUCCESS", f"Updated attachment metadata", request.client.host if request.client else "127.0.0.1")
    return {"status": "ok", "attachment": attachment_view(attachment_id, a)}

@app.get("/api/devices/compatibility")
def get_compatibility():
    matrix = []
    for code, info in COMPATIBILITY_RULES.items():
        matrix.append({
            "type_code": code,
            "type": info["type"],
            "min_fw": info["min_fw"],
            "rev_A": "A" in info["supported_hw_revs"],
            "rev_B": "B" in info["supported_hw_revs"],
            "rev_C": "C" in info["supported_hw_revs"],
            "description": info["description"],
        })
    return {
        "rules": COMPATIBILITY_RULES,
        "hw_revs": ["A", "B", "C"],
        "matrix": matrix,
    }

@app.post("/api/devices/{device_id}/decommission")
async def decommission_device(device_id: str, req: DecommissionRequest = None, request: Request = None):
    user = get_current_user_optional(request) if request else None
    if user and user.get("role") not in ["super_admin", "fleet_manager"]:
        log_audit_event(user, "device:decommission", "device", device_id, "DENIED", "Only Super Admin and Fleet Manager can decommission hardware", request.client.host if request.client else "127.0.0.1")
        raise HTTPException(status_code=403, detail="Access Denied: Only Super Admin and Fleet Manager can decommission devices.")

    reason = req.reason if req else "Operator decommissioning"

    if device_id in robots:
        r = robots[device_id]
        r["lifecycle"] = "decommissioned"
        for aid, a in attachments.items():
            if a.get("attached_to") == device_id:
                a["attached_to"] = None
                a["lifecycle"] = "inventory"
        save_fleet()
        await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
        log_audit_event(user, "device:decommission", "robot", device_id, "SUCCESS", f"Decommissioned robot. Reason: {reason}", request.client.host if request.client else "127.0.0.1")
        return {"status": "ok", "type": "robot", "device": robot_view(device_id, r)}
    elif device_id in attachments:
        a = attachments[device_id]
        a["lifecycle"] = "decommissioned"
        a["attached_to"] = None
        save_fleet()
        await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
        log_audit_event(user, "device:decommission", "attachment", device_id, "SUCCESS", f"Decommissioned attachment. Reason: {reason}", request.client.host if request.client else "127.0.0.1")
        return {"status": "ok", "type": "attachment", "device": attachment_view(device_id, a)}
    else:
        raise HTTPException(status_code=404, detail="Device not found")

# --- Remote Operations & Diagnostics Command Engine ---

@app.post("/api/robot/{robot_id}/command")
async def dispatch_robot_command(robot_id: str, req: CommandRequest, request: Request = None):
    user = get_current_user_optional(request) if request else None

    # RBAC Enforcement
    if user:
        role = user.get("role", "super_admin")
        perms = user.get("permissions", [])
        is_super = "*" in perms or role == "super_admin"

        if role == "viewer":
            log_audit_event(user, f"remoteops:{req.command}", "robot", robot_id, "DENIED", "Viewer role cannot execute remote operations", request.client.host if request.client else "127.0.0.1")
            raise HTTPException(status_code=403, detail="Access Denied: Viewers cannot execute remote operations.")

        # Specific permission checks
        if req.command in ["selftest", "can_query"] and not (is_super or "diagnostics:selftest" in perms or "remoteops:selftest" in perms):
            log_audit_event(user, f"remoteops:{req.command}", "robot", robot_id, "DENIED", "Lacks diagnostics:selftest permission", request.client.host if request.client else "127.0.0.1")
            raise HTTPException(status_code=403, detail="Access Denied: diagnostics:selftest permission required.")
        elif req.command == "reboot" and not (is_super or "remoteops:reboot" in perms):
            log_audit_event(user, "remoteops:reboot", "robot", robot_id, "DENIED", "Lacks remoteops:reboot permission", request.client.host if request.client else "127.0.0.1")
            raise HTTPException(status_code=403, detail="Access Denied: remoteops:reboot permission required.")
        elif req.command == "dock" and not (is_super or "remoteops:dock" in perms or "device:manage" in perms):
            log_audit_event(user, "remoteops:dock", "robot", robot_id, "DENIED", "Lacks remoteops:dock permission", request.client.host if request.client else "127.0.0.1")
            raise HTTPException(status_code=403, detail="Access Denied: remoteops:dock permission required.")
        elif req.command == "partition_reset" and not (is_super or "system:manage" in perms):
            log_audit_event(user, "remoteops:partition_reset", "robot", robot_id, "DENIED", "Super Admin permission required for partition reset", request.client.host if request.client else "127.0.0.1")
            raise HTTPException(status_code=403, detail="Access Denied: Super Admin permission required for partition reset.")

    if robot_id not in robots:
        raise HTTPException(status_code=404, detail="Robot not found")

    cmd_id = f"cmd_{uuid.uuid4().hex[:10]}"
    issuer_name = user.get("name", "Operator") if user else "Operator"
    issuer_role = user.get("role", "super_admin") if user else "super_admin"

    cmd_obj = {
        "command_id": cmd_id,
        "robot_id": robot_id,
        "robot_name": robots[robot_id].get("name", robot_id),
        "command": req.command,
        "parameters": req.parameters or {},
        "status": "pending",
        "progress": 0,
        "created_at": datetime.now().isoformat(),
        "delivered_at": None,
        "completed_at": None,
        "issued_by": issuer_name,
        "issued_by_role": issuer_role,
        "result": None,
        "logs": [f"[{datetime.now().strftime('%H:%M:%S')}] Command '{req.command}' queued by {issuer_name} ({issuer_role})"],
    }
    commands.append(cmd_obj)
    save_fleet()
    await broadcast({"type": "command_update", "command": cmd_obj})
    log_audit_event(user, f"remoteops:{req.command}", "robot", robot_id, "SUCCESS", f"Enqueued '{req.command}' for {robots[robot_id].get('name', robot_id)}", request.client.host if request.client else "127.0.0.1")
    return {"status": "ok", "command": cmd_obj}

@app.post("/api/robot/commands/{command_id}/ack")
async def robot_command_ack(command_id: str, req: CommandAckRequest, request: Request = None):
    cmd = next((c for c in commands if c.get("command_id") == command_id), None)
    if not cmd:
        raise HTTPException(status_code=404, detail="Command not found")

    cmd["status"] = req.status
    if req.progress is not None:
        cmd["progress"] = req.progress
    if req.logs:
        cmd["logs"].extend(req.logs)
    if req.result:
        cmd["result"] = req.result
    if req.status in ["completed", "failed"]:
        cmd["completed_at"] = datetime.now().isoformat()
        if req.status == "completed":
            cmd["progress"] = 100

    save_fleet()
    await broadcast({"type": "command_update", "command": cmd})

    r_name = robots.get(req.robot_id, {}).get("name", req.robot_id)
    if req.status == "completed":
        await broadcast({
            "type": "toast",
            "message": f"{r_name}: '{cmd['command']}' completed successfully",
            "kind": "success"
        })
    elif req.status == "failed":
        await broadcast({
            "type": "toast",
            "message": f"{r_name}: '{cmd['command']}' reported an error",
            "kind": "error"
        })

    return {"status": "ok"}

@app.get("/api/commands/history")
def get_commands_history(robot_id: Optional[str] = None, limit: int = 50):
    hist = commands
    if robot_id:
        hist = [c for c in hist if c.get("robot_id") == robot_id]
    return {
        "commands": list(reversed(hist[-limit:])),
        "diagnostic_policy": diagnostic_policy,
    }

@app.post("/api/diagnostic/policy")
async def set_diagnostic_policy(req: DiagnosticPolicyRequest, request: Request = None):
    user = get_current_user_optional(request) if request else None
    if user and user.get("role") == "viewer":
        raise HTTPException(status_code=403, detail="Access Denied: Viewers cannot modify diagnostic policies.")

    if req.telemetry_interval:
        diagnostic_policy["telemetry_interval"] = req.telemetry_interval
    if req.can_log_level:
        diagnostic_policy["can_log_level"] = req.can_log_level
    if req.crash_dump_policy:
        diagnostic_policy["crash_dump_policy"] = req.crash_dump_policy

    save_fleet()
    await broadcast({"type": "diagnostic_policy", "policy": diagnostic_policy})
    log_audit_event(user, "diagnostics:set_policy", "fleet", "all", "SUCCESS", f"Updated diagnostic policy: interval={diagnostic_policy['telemetry_interval']}, CAN={diagnostic_policy['can_log_level']}", request.client.host if request.client else "127.0.0.1")
    return {"status": "ok", "policy": diagnostic_policy}
