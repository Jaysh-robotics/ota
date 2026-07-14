from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os
import shutil
import json
import glob
import asyncio
from typing import Optional
from datetime import datetime, timedelta

app = FastAPI(title="Chore Robotics OTA Server")

UPDATE_DIR = "updates"
DB_FILE = "fleet_db.json"
ONLINE_THRESHOLD_SECONDS = 20  # robot/attachment considered offline if no check-in within this window

os.makedirs(UPDATE_DIR, exist_ok=True)
os.makedirs("static", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/updates", StaticFiles(directory="updates"), name="updates")

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
    return data

def save_fleet():
    with open(DB_FILE, "w") as f:
        json.dump({
            "robots": robots,
            "attachments": attachments,
            "attachment_versions": attachment_versions,
            "attachment_targets": attachment_targets,
        }, f, indent=2)

_state = load_state()
robots: dict[str, dict] = _state["robots"]
attachments: dict[str, dict] = _state["attachments"]
attachment_versions: list[dict] = _state["attachment_versions"]
attachment_targets: dict[str, str] = _state["attachment_targets"]
current_target_version = ""
active_connections: list[WebSocket] = []

# --- Models ---

class RegisterRequest(BaseModel):
    robot_id: str
    name: str
    model: str = "Unknown"
    version: str
    serial: Optional[str] = None
    hw_rev: Optional[str] = "A"

class AckRequest(BaseModel):
    robot_id: str
    status: str  # "triggered" | "idle"
    version: Optional[str] = None

class DeployRequest(BaseModel):
    version: str
    robot_ids: Optional[list[str]] = None  # None = all known robots

class AttachmentRegisterRequest(BaseModel):
    attachment_id: str
    type: str
    type_code: str
    version: str
    attached_to: Optional[str] = None

class AttachmentAckRequest(BaseModel):
    attachment_id: str
    status: str  # "triggered" | "idle"
    version: Optional[str] = None

class AttachmentDeployRequest(BaseModel):
    version: str
    type_code: str
    attachment_ids: Optional[list[str]] = None  # None = all attachments of that type

class DemoStageRequest(BaseModel):
    stage: int
    title: str
    section: str
    caption: str

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

def robot_view(robot_id: str, r: dict) -> dict:
    """Compute derived fields (online/offline) at read time."""
    last_seen = datetime.fromisoformat(r["last_seen"])
    is_online = (datetime.now() - last_seen) < timedelta(seconds=ONLINE_THRESHOLD_SECONDS)
    out = dict(r)
    out["status"] = "online" if is_online else "offline"
    return out

def attachment_view(attachment_id: str, a: dict) -> dict:
    """Compute derived fields at read time. Unattached attachments (sitting in
    inventory) never heartbeat, so they always report status "inventory"
    regardless of last_seen."""
    out = dict(a)
    if a.get("attached_to") is None:
        out["status"] = "inventory"
    else:
        last_seen = datetime.fromisoformat(a["last_seen"])
        is_online = (datetime.now() - last_seen) < timedelta(seconds=ONLINE_THRESHOLD_SECONDS)
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

# --- Dashboard routes ---

@app.get("/", response_class=HTMLResponse)
def read_root():
    with open("static/index.html", "r") as f:
        return f.read()

@app.get("/api/fleet")
def get_fleet():
    return fleet_snapshot()

@app.get("/api/versions")
def get_versions():
    return {"versions": scan_uploaded_versions(), "target": current_target_version}

@app.post("/upload")
async def upload_update(file: UploadFile = File(...), version: str = ""):
    file_path = os.path.join(UPDATE_DIR, f"update_{version}.tar.gz")
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    await broadcast({"type": "versions_update", "versions": scan_uploaded_versions()})
    return {"status": "uploaded", "version": version}

@app.post("/api/clear")
async def clear_database():
    global robots, attachments, current_target_version, attachment_targets
    robots = {}
    attachments = {}
    current_target_version = ""
    attachment_targets = {}
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    await broadcast({"type": "target_update", "target_version": current_target_version})
    await broadcast({"type": "attachment_target_update", "targets": dict(attachment_targets)})
    await broadcast({"type": "toast", "message": "Database cleared — waiting for robots to connect", "kind": "info"})
    return {"status": "cleared"}

@app.post("/deploy")
async def deploy_update(req: DeployRequest):
    global current_target_version
    current_target_version = req.version
    ids = req.robot_ids if req.robot_ids else list(robots.keys())
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
    return {"status": "queued", "targets": targets}

@app.post("/deploy/attachment")
async def deploy_attachment_update(req: AttachmentDeployRequest):
    now = datetime.now().isoformat()
    if not any(v["type_code"] == req.type_code and v["version"] == req.version for v in attachment_versions):
        attachment_versions.append({
            "type_code": req.type_code,
            "version": req.version,
            "filename": None,
            "size": None,
            "uploaded_at": now,
        })
    attachment_targets[req.type_code] = req.version

    candidate_ids = req.attachment_ids if req.attachment_ids else [
        aid for aid, a in attachments.items() if a["type_code"] == req.type_code
    ]
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
    }))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in active_connections:
            active_connections.remove(ws)

# --- Robot-facing routes ---

@app.post("/api/robot/register")
async def robot_register(req: RegisterRequest):
    now = datetime.now().isoformat()
    existing = robots.get(req.robot_id)
    robots[req.robot_id] = {
        "name": req.name,
        "model": req.model,
        "serial": req.serial or req.robot_id,
        "hw_rev": req.hw_rev or "A",
        "current_version": req.version,
        "pending_version": existing.get("pending_version") if existing else None,
        "update_status": existing.get("update_status", "idle") if existing else "idle",
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
    was_offline = (datetime.now() - datetime.fromisoformat(r["last_seen"])) >= timedelta(seconds=ONLINE_THRESHOLD_SECONDS)
    r["last_seen"] = datetime.now().isoformat()
    r["current_version"] = version
    pending = r.get("pending_version")
    update_available = bool(pending) and pending != version and r.get("update_status") != "triggered"
    save_fleet()
    await broadcast({"type": "fleet_update", "fleet": fleet_snapshot()})
    if was_offline:
        await broadcast({"type": "toast", "message": f"{r['name']} back online", "kind": "success"})
    return {
        "update_available": update_available,
        "target_version": pending if update_available else None,
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

# --- Attachment-facing routes ---

@app.post("/api/attachment/register")
async def attachment_register(req: AttachmentRegisterRequest):
    now = datetime.now().isoformat()
    existing = attachments.get(req.attachment_id)
    attachments[req.attachment_id] = {
        "type": req.type,
        "type_code": req.type_code,
        "serial": req.attachment_id,
        "attached_to": req.attached_to,
        "current_version": req.version,
        "pending_version": existing.get("pending_version") if existing else None,
        "update_status": existing.get("update_status", "idle") if existing else "idle",
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
