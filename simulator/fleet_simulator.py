#!/usr/bin/env python3
"""
Chore Robotics fleet simulator.

Drives the REAL OTA server end-to-end for the product demo video: registers a
fleet of 12 robots and 12 attachments, keeps them heartbeating for the whole
run, and plays out the 6-act demo timeline from DEMO_SPEC.md §5/§6 (staging
each act via POST /api/demo/stage so the dashboard's virtual cursor drives
the actual clicks — the simulator itself never calls /deploy or
/deploy/attachment). Installs are reactive: any heartbeat check that comes
back update_available triggers that robot/attachment to ack "triggered",
"install" for a few seconds, then ack "idle" at the new version — whenever
the operator actually pushes the update from the dashboard.

HARD RULE — NO FALLBACKS: every HTTP call uses response.raise_for_status().
Nothing here catches-and-continues or fakes data on failure. If the server is
unreachable or returns an error, the offending request raises, the exception
propagates out of whatever thread it happened in, and the whole process exits
non-zero (see the threading.excepthook override below, which is what makes a
background-thread failure crash the demo instead of dying silently).

Usage:
    python3 fleet_simulator.py --server http://localhost:8000 [--fast | --short]

--fast divides every sleep by 4 (for quick pipeline testing; it will NOT
look like a polished demo — it's for verifying the simulator drives the API
correctly, not for recording).

--short runs a compressed ~15.5s feature-reel timeline instead of the full
~2:30 demo (see DEMO_SPEC.md §8) — same six acts, same API traffic, just
staged back-to-back for a short-form clip. Heartbeat and install timings are
also compressed in this mode so a full install cycle fits on screen.
Mutually exclusive with --fast.
"""
import argparse
import io
import os
import random
import sys
import tarfile
import threading
import time
import traceback

import requests

# --- Globals set by main() before any agent/thread is created ---
SERVER = "http://localhost:8000"
DIV = 1  # sleep divisor; becomes 4 under --fast
SHORT = False  # becomes True under --short (mutually exclusive with --fast)

# Heartbeat interval and install-duration ranges (seconds). Compressed under
# --short so a full check-in/install cycle still fits inside a ~15s clip.
# Applies to both RobotAgent and AttachmentAgent (_loop and _install).
HEARTBEAT_INTERVAL_RANGE = (4, 6)
INSTALL_DURATION_RANGE = (6, 10)

TIMEOUT = 10


def _thread_excepthook(args):
    """Any unhandled exception in a background (heartbeat) thread must crash
    the whole simulator, loudly, non-zero. Python's default behavior is to
    print a traceback and let the thread die silently — that IS a fallback
    (the demo would keep running with a dead robot and nobody would know).
    We refuse that: kill the whole process immediately."""
    print(f"\n[FATAL] Unhandled exception in thread {args.thread.name!r}:", file=sys.stderr)
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
    sys.stderr.flush()
    os._exit(1)


threading.excepthook = _thread_excepthook


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def act_banner(stage: int, title: str):
    print()
    print("=" * 72)
    print(f"  ACT {stage} — {title}")
    print("=" * 72)


def wait(seconds: float):
    time.sleep(seconds / DIV)


def wait_range(lo: float, hi: float):
    time.sleep(random.uniform(lo, hi) / DIV)


# --- HTTP helpers (server-facing). Every call raises on failure, no excuses. ---

def api_post(path: str, payload: dict) -> dict:
    resp = requests.post(f"{SERVER}{path}", json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def api_get(path: str, params: dict) -> dict:
    resp = requests.get(f"{SERVER}{path}", params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def stage(n: int, title: str, section: str, caption: str):
    payload = {"stage": n, "title": title, "section": section, "caption": caption}
    if SHORT:
        payload["tempo"] = "short"
    api_post("/api/demo/stage", payload)
    act_banner(n, title)
    log(f"» {caption}")


# --- Firmware catalog upload -------------------------------------------------
# The dashboard's "Firmware Version to Deploy" dropdown only lists packages
# that actually exist on the server, and Act 3's cursor choreography selects
# v1.3.1 — so the catalog must be uploaded before the demo starts. Real
# multipart uploads through the real /upload endpoint; raises on any failure.

ROBOT_FIRMWARE_VERSIONS = ["1.2.4", "1.3.0", "1.3.1"]


def make_firmware_blob(version: str) -> bytes:
    """A genuine .tar.gz with a firmware payload inside. Size is deterministic
    per version (~5 MB) so the dashboard shows realistic package sizes."""
    size = 4_600_000 + int(version.replace(".", "")) * 3_000
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = os.urandom(size)  # incompressible → archive stays ~size
        info = tarfile.TarInfo(name=f"chorebot_firmware_{version}.bin")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def upload_firmware_catalog():
    existing = {release["version"] for release in api_get("/api/versions", {}).get("versions", [])}
    missing = [version for version in ROBOT_FIRMWARE_VERSIONS if version not in existing]
    if not missing:
        log("Robot firmware catalog is already available; reusing existing releases.")
        return
    token = os.environ.get("CHORE_OPERATOR_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Catalog releases are missing. Upload them from the dashboard, or set "
                           "CHORE_OPERATOR_TOKEN to an authenticated firmware uploader's access token.")
    log("Uploading missing robot firmware releases to the OTA server...")
    for v in missing:
        blob = make_firmware_blob(v)
        resp = requests.post(
            f"{SERVER}/upload",
            params={"version": v},
            files={"file": (f"update_{v}.tar.gz", blob, "application/gzip")},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        resp.raise_for_status()
        log(f"  ✓ update_{v}.tar.gz uploaded ({len(blob) // 1024} KB)")


# --- Fleet definition (serials/names per DEMO_SPEC.md §1) ---

MODELS = ["ChoreBot X1", "ChoreBot X2", "ChoreBot X2 Pro"]
HW_REVS = ["A", "B", "C"]

ATTACHMENT_TYPE_NAMES = {
    "SP": "Snow Plower",
    "LM": "Lawn Mower",
    "SR": "Snow Remover",
    "LC": "Leaf Collector",
}


def robot_serial(num: int) -> str:
    return f"CR-2026-{num:05d}"


def robot_name(num: int) -> str:
    return f"Chorerobot-{num:05d}"


def robot_model(num: int) -> str:
    return MODELS[(num - 1) % 3]


def robot_hw_rev(num: int) -> str:
    return HW_REVS[(num - 1) % 3]


def robot_baseline_version(num: int) -> str:
    if 1 <= num <= 6:
        return "1.3.0"
    if 7 <= num <= 11:
        return "1.2.4"
    if num == 12:
        return "1.3.1"  # already current — registers live in Act 2, no update needed
    raise ValueError(f"no baseline version rule for robot {num}")


def attachment_serial(code: str, num: int) -> str:
    return f"AT-{code}-2026-{num:05d}"


class RobotAgent:
    def __init__(self, num: int):
        self.num = num
        self.serial = robot_serial(num)
        self.robot_id = self.serial
        self.name = robot_name(num)
        self.model = robot_model(num)
        self.hw_rev = robot_hw_rev(num)
        self.version = robot_baseline_version(num)
        self.paused = threading.Event()
        self._thread = None

    def register(self):
        api_post("/api/robot/register", {
            "robot_id": self.robot_id,
            "name": self.name,
            "model": self.model,
            "version": self.version,
            "serial": self.serial,
            "hw_rev": self.hw_rev,
        })
        log(f"  + {self.name} ({self.serial}, {self.model}, hw_rev {self.hw_rev}) online @ v{self.version}")

    def start_heartbeat(self):
        self._thread = threading.Thread(target=self._loop, name=f"hb-{self.robot_id}", daemon=True)
        self._thread.start()

    def pause(self):
        self.paused.set()

    def resume(self):
        self.paused.clear()

    def _loop(self):
        while True:
            if not self.paused.is_set():
                self._check_in()
            wait_range(*HEARTBEAT_INTERVAL_RANGE)

    def _check_in(self):
        data = api_get("/api/robot/check", {"robot_id": self.robot_id, "version": self.version})
        if data.get("update_available"):
            self._install(data["target_version"])

        # Process any pending remote operations / diagnostics commands
        pending_cmds = data.get("pending_commands", [])
        for cmd in pending_cmds:
            threading.Thread(
                target=self._execute_command,
                args=(cmd,),
                name=f"cmd-{self.robot_id}-{cmd['command_id']}",
                daemon=True
            ).start()

    def _install(self, target: str):
        api_post("/api/robot/ack", {"robot_id": self.robot_id, "status": "triggered"})
        log(f"  ⚡ {self.name} received OTA trigger -> installing v{target}")
        wait_range(*INSTALL_DURATION_RANGE)
        api_post("/api/robot/ack", {"robot_id": self.robot_id, "status": "idle", "version": target})
        self.version = target
        log(f"  ✓ {self.name} now running v{target}")

    def _execute_command(self, cmd: dict):
        cmd_id = cmd["command_id"]
        cmd_type = cmd["command"]
        log(f"  🔧 {self.name} executing remote command: '{cmd_type}' ({cmd_id})")

        # Initial acknowledgment: executing state
        api_post(f"/api/robot/commands/{cmd_id}/ack", {
            "robot_id": self.robot_id,
            "status": "executing",
            "progress": 15,
            "logs": [f"[{time.strftime('%H:%M:%S')}] Received dispatch for '{cmd_type}' — initializing subsystem handler"]
        })

        if cmd_type == "selftest":
            self._handle_selftest(cmd_id)
        elif cmd_type == "reboot":
            self._handle_reboot(cmd_id)
        elif cmd_type == "can_query":
            self._handle_can_query(cmd_id)
        elif cmd_type == "fetch_logs":
            self._handle_fetch_logs(cmd_id)
        elif cmd_type == "blink_led":
            self._handle_blink_led(cmd_id)
        elif cmd_type == "dock":
            self._handle_dock(cmd_id)
        elif cmd_type == "partition_reset":
            self._handle_partition_reset(cmd_id)
        else:
            wait(1.5)
            api_post(f"/api/robot/commands/{cmd_id}/ack", {
                "robot_id": self.robot_id,
                "status": "completed",
                "progress": 100,
                "result": {"status": "OK", "detail": f"Command {cmd_type} executed"},
                "logs": [f"[{time.strftime('%H:%M:%S')}] Command '{cmd_type}' finished successfully"]
            })

    def _handle_selftest(self, cmd_id: str):
        wait(0.8)
        api_post(f"/api/robot/commands/{cmd_id}/ack", {
            "robot_id": self.robot_id,
            "status": "executing",
            "progress": 45,
            "logs": [
                f"[{time.strftime('%H:%M:%S')}] [MCU] Querying mainboard MCU at CAN 0x10... ACK (3.3V rail: 3.31V, Core Temp: 38.5°C)",
                f"[{time.strftime('%H:%M:%S')}] [CAN] Scanning CAN 2.0B bus topology... 3 active nodes discovered (0 drop frames)"
            ]
        })
        wait(0.8)
        api_post(f"/api/robot/commands/{cmd_id}/ack", {
            "robot_id": self.robot_id,
            "status": "executing",
            "progress": 80,
            "logs": [
                f"[{time.strftime('%H:%M:%S')}] [IMU] 6-DOF IMU self-test: Accelerometer & Gyro calibrated (Drift: 0.02 deg/s)",
                f"[{time.strftime('%H:%M:%S')}] [MOTOR] PWM short-pulse test: Left=1.42A, Right=1.39A (within nominal limits)"
            ]
        })
        wait(0.6)
        api_post(f"/api/robot/commands/{cmd_id}/ack", {
            "robot_id": self.robot_id,
            "status": "completed",
            "progress": 100,
            "result": {
                "overall": "PASSED",
                "subsystems": {
                    "mcu": {"status": "OK", "voltage_v": 3.31, "temp_c": 38.5},
                    "can_bus": {"status": "OK", "nodes_discovered": 3, "bus_load_pct": 14.2},
                    "imu": {"status": "OK", "drift_rate_deg_s": 0.02, "calibrated": True},
                    "motor_drivers": {"status": "OK", "left_draw_a": 1.42, "right_draw_a": 1.39},
                    "bms": {"status": "OK", "cell_balance_mv": 11, "soc_pct": 89}
                }
            },
            "logs": [
                f"[{time.strftime('%H:%M:%S')}] [BMS] Battery Management System: 89% SoC, cell differential 11mV (nominal)",
                f"[{time.strftime('%H:%M:%S')}] [DIAG_DONE] Hardware Self-Test finished: All 5 subsystems operational (PASSED)"
            ]
        })
        log(f"  ✓ {self.name} Hardware Self-Test PASSED")

    def _handle_reboot(self, cmd_id: str):
        wait(0.6)
        api_post(f"/api/robot/commands/{cmd_id}/ack", {
            "robot_id": self.robot_id,
            "status": "executing",
            "progress": 50,
            "logs": [
                f"[{time.strftime('%H:%M:%S')}] Syncing storage buffers & terminating active ROS2 nodes...",
                f"[{time.strftime('%H:%M:%S')}] Flushing NVRAM, trigger watchdog reboot sequence"
            ]
        })
        self.pause()
        wait(2.2)
        self.resume()
        api_post(f"/api/robot/commands/{cmd_id}/ack", {
            "robot_id": self.robot_id,
            "status": "completed",
            "progress": 100,
            "result": {"status": "REBOOTED", "uptime_sec": 4, "kernel": "5.15-tegra-cr"},
            "logs": [
                f"[{time.strftime('%H:%M:%S')}] Power cycle complete — Kernel 5.15-tegra-cr, Base MCU ready",
                f"[{time.strftime('%H:%M:%S')}] System services online"
            ]
        })
        log(f"  ✓ {self.name} reboot completed")

    def _handle_can_query(self, cmd_id: str):
        wait(0.8)
        api_post(f"/api/robot/commands/{cmd_id}/ack", {
            "robot_id": self.robot_id,
            "status": "completed",
            "progress": 100,
            "result": {
                "active_nodes": 3,
                "bus_speed": "1Mbps",
                "bitrate_utilization": "12.8%",
                "rx_errors": 0,
                "tx_errors": 0,
                "nodes": [
                    {"node_id": "0x10", "name": f"Base Chassis MCU (Rev {self.hw_rev})", "rtt_ms": 1.2, "status": "HEALTHY"},
                    {"node_id": "0x20", "name": "Jetson Orin Compute Carrier", "rtt_ms": 0.8, "status": "HEALTHY"},
                    {"node_id": "0x30", "name": "Tool Attachment CAN Interface", "rtt_ms": 1.5, "status": "ACTIVE"}
                ]
            },
            "logs": [
                f"[{time.strftime('%H:%M:%S')}] Initiating CAN Bus 1Mbps frame broadcast ping...",
                f"[{time.strftime('%H:%M:%S')}] [Node 0x10] Base Chassis MCU (ChoreBot X1 Rev {self.hw_rev}) -> RTT: 1.2ms, status: HEALTHY",
                f"[{time.strftime('%H:%M:%S')}] [Node 0x20] Jetson Orin Compute Carrier -> RTT: 0.8ms, status: HEALTHY",
                f"[{time.strftime('%H:%M:%S')}] [Node 0x30] Tool Attachment Interface -> RTT: 1.5ms, status: ACTIVE",
                f"[{time.strftime('%H:%M:%S')}] CAN bus diagnostic query complete: 3 nodes responding, 0 error frames"
            ]
        })
        log(f"  ✓ {self.name} CAN Bus query complete (3 active nodes)")

    def _handle_fetch_logs(self, cmd_id: str):
        wait(0.7)
        api_post(f"/api/robot/commands/{cmd_id}/ack", {
            "robot_id": self.robot_id,
            "status": "completed",
            "progress": 100,
            "result": {
                "bundle_size_kb": 124,
                "entries_scanned": 820,
                "critical_panics": 0,
                "warnings": 2,
                "log_archive": f"/var/log/cr_dump_{cmd_id}.tar.gz"
            },
            "logs": [
                f"[{time.strftime('%H:%M:%S')}] Capturing journalctl -u chorerobot-agent -n 50...",
                f"[{time.strftime('%H:%M:%S')}] Dumping kernel ring buffer (dmesg | tail -n 30)...",
                f"[{time.strftime('%H:%M:%S')}] Packaged 124KB diagnostic bundle: /var/log/cr_dump_{cmd_id}.tar.gz",
                f"[{time.strftime('%H:%M:%S')}] Diagnostic logs successfully retrieved"
            ]
        })
        log(f"  ✓ {self.name} crash & diagnostic logs packaged")

    def _handle_blink_led(self, cmd_id: str):
        wait(0.5)
        api_post(f"/api/robot/commands/{cmd_id}/ack", {
            "robot_id": self.robot_id,
            "status": "completed",
            "progress": 100,
            "result": {"gpio_pin": 18, "pattern": "identification_pulse_2hz", "duration_sec": 10},
            "logs": [
                f"[{time.strftime('%H:%M:%S')}] GPIO pin 18 (Beacon LED) pulsing at 2Hz for 10s pattern",
                f"[{time.strftime('%H:%M:%S')}] Chassis beacon active"
            ]
        })
        log(f"  ✓ {self.name} Beacon LED pattern initiated")

    def _handle_dock(self, cmd_id: str):
        wait(0.9)
        api_post(f"/api/robot/commands/{cmd_id}/ack", {
            "robot_id": self.robot_id,
            "status": "completed",
            "progress": 100,
            "result": {"dock_id": "BAY-03", "charging": True, "battery_v": 52.4, "current_a": 9.8},
            "logs": [
                f"[{time.strftime('%H:%M:%S')}] Aborting active chore waypoint navigation...",
                f"[{time.strftime('%H:%M:%S')}] Computing path to charging station Bay #03...",
                f"[{time.strftime('%H:%M:%S')}] Aligning IR dock fiducials... Docked and charging engaged (48V 10A)"
            ]
        })
        log(f"  ✓ {self.name} docked at Bay #03")

    def _handle_partition_reset(self, cmd_id: str):
        wait(1.0)
        api_post(f"/api/robot/commands/{cmd_id}/ack", {
            "robot_id": self.robot_id,
            "status": "completed",
            "progress": 100,
            "result": {"active_partition": "SLOT_B_GOLDEN", "sha256": "e3b0c44298fc1c149afbf4c8996fb924", "status": "RESET_COMPLETE"},
            "logs": [
                f"[{time.strftime('%H:%M:%S')}] Switching active A/B slot to Golden Recovery Partition B...",
                f"[{time.strftime('%H:%M:%S')}] Factory partition verified (SHA256 valid). Restarting...",
                f"[{time.strftime('%H:%M:%S')}] Golden recovery partition loaded successfully"
            ]
        })
        log(f"  ✓ {self.name} Partition Reset to Golden Slot B complete")


class AttachmentAgent:
    def __init__(self, code: str, num: int, version: str, attached_robot_num: int | None):
        self.code = code
        self.type_name = ATTACHMENT_TYPE_NAMES[code]
        self.serial = attachment_serial(code, num)
        self.attachment_id = self.serial
        self.version = version
        self.attached_robot_num = attached_robot_num
        self.attached_to = robot_serial(attached_robot_num) if attached_robot_num else None
        self._thread = None

    def register(self):
        api_post("/api/attachment/register", {
            "attachment_id": self.attachment_id,
            "type": self.type_name,
            "type_code": self.code,
            "version": self.version,
            "attached_to": self.attached_to,
        })
        where = self.attached_to if self.attached_to else "inventory"
        log(f"  + {self.type_name} {self.serial} registered @ v{self.version} ({where})")

    def start_heartbeat(self):
        if not self.attached_to:
            return  # inventory attachments register once and never heartbeat
        self._thread = threading.Thread(target=self._loop, name=f"hb-{self.attachment_id}", daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            self._check_in()
            wait_range(*HEARTBEAT_INTERVAL_RANGE)

    def _check_in(self):
        data = api_get("/api/attachment/check", {"attachment_id": self.attachment_id, "version": self.version})
        if data.get("update_available"):
            self._install(data["target_version"])

    def _install(self, target: str):
        api_post("/api/attachment/ack", {"attachment_id": self.attachment_id, "status": "triggered"})
        log(f"  ⚡ {self.type_name} {self.serial} received OTA trigger -> installing v{target}")
        wait_range(*INSTALL_DURATION_RANGE)
        api_post("/api/attachment/ack", {"attachment_id": self.attachment_id, "status": "idle", "version": target})
        self.version = target
        log(f"  ✓ {self.type_name} {self.serial} now running v{target}")


def build_fleet():
    """Returns (robots_by_num, attachments) per the spec's exact distribution:
    12 robots CR-2026-00001..00012; 12 attachments (3 SP, 4 LM, 2 SR, 3 LC),
    10 attached / 2 in inventory. Robot 00012 and its Leaf Collector
    (AT-LC-2026-00002) are held back for the live-registration Act 2.
    """
    robots = {num: RobotAgent(num) for num in range(1, 13)}

    # (code, num, baseline_version, attached_robot_num_or_None)
    plan = [
        ("SP", 1, "1.1.0", 1),
        ("SP", 2, "1.1.0", 2),
        ("SP", 3, "1.1.0", None),          # inventory
        ("LM", 1, "2.4.0", 3),
        ("LM", 2, "2.4.0", 4),
        ("LM", 3, "2.4.0", 5),
        ("LM", 4, "2.4.0", 6),
        ("SR", 1, "1.0.2", 7),
        ("SR", 2, "1.0.2", 8),
        ("LC", 1, "3.0.0", 10),
        ("LC", 2, "3.0.0", 12),            # held back for Act 2 (with robot 00012)
        ("LC", 3, "3.0.0", None),          # inventory
    ]
    attachments = [AttachmentAgent(code, num, version, robot_num) for code, num, version, robot_num in plan]
    return robots, attachments


# --- Demo acts ---

def act1_fleet_online(robots: dict, attachments: list):
    stage(1, "Fleet comes online", "fleet",
          "11 robots and their attachments come online in real time")

    held_back = {12}
    held_back_attachment = "AT-LC-2026-00002"

    jobs = []
    for num in sorted(robots.keys()):
        if num in held_back:
            continue
        jobs.append(("robot", robots[num]))
        for att in attachments:
            if att.attached_robot_num == num:
                jobs.append(("attachment", att))
    for att in attachments:
        if att.attached_to is None and att.serial != held_back_attachment:
            jobs.append(("attachment", att))

    # ~20 registrations staggered over ~12s.
    per_item = 12.0 / max(len(jobs), 1)
    for kind, obj in jobs:
        obj.register()
        obj.start_heartbeat()
        wait_range(per_item * 0.6, per_item * 1.4)

    log(f"Act 1 complete — {len(robots) - 1} robots and {len(attachments) - 1} attachments live.")


def act2_zero_touch(robots: dict, attachments: list):
    stage(2, "Zero-touch provisioning", "devices",
          "A brand new robot registers itself and its attachment with zero manual setup")

    robot12 = robots[12]
    robot12.register()
    robot12.start_heartbeat()
    wait_range(1.5, 2.5)

    leaf_collector = next(a for a in attachments if a.serial == "AT-LC-2026-00002")
    leaf_collector.register()
    leaf_collector.start_heartbeat()

    log("Act 2 complete — Chorerobot-00012 is live with its Leaf Collector attached.")


def act3_robot_ota(robots: dict):
    stage(3, "Robot OTA rollout", "deploy",
          "Waiting for the operator to push firmware v1.3.1 to the fleet from the dashboard")
    log("Standing by — the simulator no longer triggers deploys itself; robots react the "
        "instant the operator clicks Push Update to Fleet on the dashboard.")


def act4_attachment_ota(attachments: list):
    stage(4, "Attachment OTA", "deploy",
          "Waiting for the operator to push Lawn Mower firmware v2.4.1 from the dashboard")
    log("Standing by — attachments react reactively too, the instant the operator pushes "
        "the Lawn Mower update from the dashboard.")


def act5_remote_diagnostics(robots: dict):
    stage(5, "Remote diagnostics", "remoteops",
          "When a robot goes quiet, the fleet dashboard notices immediately")

    watched = robots[9]
    pause_seconds = 24  # comfortably clears the 20s online threshold regardless of jitter
    log(f"{watched.name} going silent for diagnostics window (~{pause_seconds}s)...")
    watched.pause()
    wait(pause_seconds)
    log(f"{watched.name} resuming heartbeats...")
    watched.resume()
    wait_range(6, 8)  # let its next check-in land so the recovery toast fires
    log(f"Act 5 complete — {watched.name} back online.")


def act6_data_flywheel():
    stage(6, "The data flywheel", "flywheel",
          "Every deployment feeds the next one — telemetry in, better firmware out")
    log("Act 6 — heartbeats keep flowing; flywheel counters stay live.")


def run_demo(robots: dict, attachments: list):
    act1_fleet_online(robots, attachments)
    wait(13)  # land near the 0:25 mark

    act2_zero_touch(robots, attachments)
    wait(17)  # land near the 0:45 mark

    act3_robot_ota(robots)
    wait(18)  # deploy click + 2-3s on the progress bar, then straight to attachments

    act4_attachment_ota(attachments)
    wait(16)  # attachment push + progress beat, then on to diagnostics

    act5_remote_diagnostics(robots)

    act6_data_flywheel()

    print()
    print("=" * 72)
    log("Demo timeline complete (~2:30). Heartbeats continue so the dashboard stays live.")
    log("Press Ctrl+C to stop the simulator.")
    print("=" * 72)


# --- Short-mode acts (DEMO_SPEC.md §8) ---------------------------------------
# Same six acts, same API traffic as the full demo — just staged back-to-back
# for a ~15.5s feature-reel clip instead of ~2:30. Every act has its own
# _short stage() call (rather than reusing the normal-mode act1/act3/act4/act6
# functions) so the short reel's on-screen caption overlay (DEMO_SPEC.md §8.6,
# frontend-only, short tempo only) can read feature-forward — title = feature
# name, caption = one punchy benefit line — without changing the strings the
# normal 2:30 demo prints to its console/PiP terminal shot.

def act1_fleet_online_short(robots: dict, attachments: list):
    stage(1, "Live Fleet Monitoring", "fleet",
          "Every robot online and reporting in real time")

    held_back = {12}
    held_back_attachment = "AT-LC-2026-00002"

    # Same job list as act1_fleet_online: every robot except #12 (held back
    # for Act 2) plus its attached attachments, plus inventory attachments
    # not held back for Act 2.
    jobs = []
    for num in sorted(robots.keys()):
        if num in held_back:
            continue
        jobs.append(("robot", robots[num]))
        for att in attachments:
            if att.attached_robot_num == num:
                jobs.append(("attachment", att))
    for att in attachments:
        if att.attached_to is None and att.serial != held_back_attachment:
            jobs.append(("attachment", att))

    # Staggered over ~2.0s total instead of ~12s so the whole fleet lands
    # in-frame inside a 15s clip.
    per_item = 2.0 / max(len(jobs), 1)
    for kind, obj in jobs:
        obj.register()
        obj.start_heartbeat()
        wait_range(per_item * 0.6, per_item * 1.4)

    log(f"Act 1 complete — {len(robots) - 1} robots and {len(attachments) - 1} attachments live.")


def act2_zero_touch_short(robots: dict, attachments: list):
    stage(2, "Zero-Touch Provisioning", "devices",
          "New hardware joins the fleet with zero manual setup")

    robot12 = robots[12]
    robot12.register()
    robot12.start_heartbeat()
    wait_range(0.3, 0.5)

    leaf_collector = next(a for a in attachments if a.serial == "AT-LC-2026-00002")
    leaf_collector.register()
    leaf_collector.start_heartbeat()

    log("Act 2 complete — Chorerobot-00012 is live with its Leaf Collector attached.")


def act3_robot_ota_short(robots: dict):
    stage(3, "OTA Firmware Rollouts", "deploy",
          "Push new firmware to the whole fleet in one click")
    log("Standing by — the simulator no longer triggers deploys itself; robots react the "
        "instant the operator clicks Push Update to Fleet on the dashboard.")


def act4_attachment_ota_short(attachments: list):
    stage(4, "Attachment Firmware OTA", "deploy",
          "Update attachment firmware fleet-wide, instantly")
    log("Standing by — attachments react reactively too, the instant the operator pushes "
        "the Lawn Mower update from the dashboard.")


def act5_remote_diagnostics_short():
    stage(5, "Remote Diagnostics & Commands", "remoteops",
          "Send commands, get acked-and-executed proof, remotely")
    log("Standing by — short mode skips the offline/recovery window (the 20s offline "
        "threshold can't fit in a 15s clip); the self-test command history is the visual.")


def act6_data_flywheel_short():
    stage(6, "The Data Flywheel", "flywheel",
          "Every rollout makes the next one smarter")
    log("Act 6 — heartbeats keep flowing; flywheel counters stay live.")


def run_demo_short(robots: dict, attachments: list):
    """~15.5s feature-reel timeline (DEMO_SPEC.md §8). Act schedule (t = seconds
    from the start of Act 1, after the countdown):
      Act 1 fleet     t=0.0
      Act 2 devices   t=2.6
      Act 3 deploy    t=5.2
      Act 4 deploy    t=8.6
      Act 5 remoteops t=11.8
      Act 6 flywheel  t=14.2
    """
    print()
    print("=" * 72)
    print("  START RECORDING NOW")
    print("=" * 72)
    for n in (3, 2, 1):
        print(f"  {n}...", flush=True)
        wait(1)

    act1_fleet_online_short(robots, attachments)   # t=0.0 (~2.0s internal)
    wait(0.6)                                       # land Act 2 at t≈2.6

    act2_zero_touch_short(robots, attachments)      # t≈2.6 (~0.3-0.5s internal)
    wait(2.2)                                       # land Act 3 at t≈5.2

    act3_robot_ota_short(robots)                    # t≈5.2 (stage post only)
    wait(3.4)                                        # land Act 4 at t≈8.6

    act4_attachment_ota_short(attachments)          # t≈8.6 (stage post only)
    wait(3.2)                                        # land Act 5 at t≈11.8

    act5_remote_diagnostics_short()                 # t≈11.8 (stage post only)
    wait(2.4)                                        # land Act 6 at t≈14.2

    act6_data_flywheel_short()                      # t≈14.2 (stage post only)
    wait(1.3)                                        # settle before closing banner

    print()
    print("=" * 72)
    log("Short timeline complete (~15s). Heartbeats continue so the dashboard stays live.")
    log("Press Ctrl+C to stop the simulator.")
    print("=" * 72)


def main():
    global SERVER, DIV, SHORT, HEARTBEAT_INTERVAL_RANGE, INSTALL_DURATION_RANGE

    parser = argparse.ArgumentParser(description="Chore Robotics fleet simulator")
    parser.add_argument("--server", default="http://localhost:8000", help="OTA server base URL")
    parser.add_argument("--fast", action="store_true", help="divide all sleeps by 4 (for testing, not recording)")
    parser.add_argument("--short", action="store_true",
                         help="run the ~15.5s feature-reel timeline instead of the full ~2:30 demo "
                              "(for a short-form clip); mutually exclusive with --fast")
    args = parser.parse_args()

    if args.short and args.fast:
        parser.error("--short and --fast cannot be combined")

    SERVER = args.server.rstrip("/")
    DIV = 4 if args.fast else 1
    SHORT = args.short
    if SHORT:
        HEARTBEAT_INTERVAL_RANGE = (0.8, 1.4)
        INSTALL_DURATION_RANGE = (1.0, 1.8)

    mode = "FAST (testing)" if args.fast else "SHORT (15s feature reel)" if SHORT else "DEMO (real-time)"
    print("=" * 72)
    print("  CHORE ROBOTICS FLEET SIMULATOR")
    print(f"  Server : {SERVER}")
    print(f"  Mode   : {mode}")
    print("=" * 72)

    upload_firmware_catalog()
    robots, attachments = build_fleet()

    try:
        if SHORT:
            run_demo_short(robots, attachments)
        else:
            run_demo(robots, attachments)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        log("Shutting down fleet simulator.")
        sys.exit(0)


if __name__ == "__main__":
    main()
