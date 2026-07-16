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
    log("Uploading robot firmware catalog to the OTA server...")
    for v in ROBOT_FIRMWARE_VERSIONS:
        blob = make_firmware_blob(v)
        resp = requests.post(
            f"{SERVER}/upload",
            params={"version": v},
            files={"file": (f"update_{v}.tar.gz", blob, "application/gzip")},
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

    def _install(self, target: str):
        api_post("/api/robot/ack", {"robot_id": self.robot_id, "status": "triggered"})
        log(f"  ⚡ {self.name} received OTA trigger -> installing v{target}")
        wait_range(*INSTALL_DURATION_RANGE)
        api_post("/api/robot/ack", {"robot_id": self.robot_id, "status": "idle", "version": target})
        self.version = target
        log(f"  ✓ {self.name} now running v{target}")


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
# for a ~15.5s feature-reel clip instead of ~2:30. Acts 3, 4 and 6 have no
# internal timing of their own in the normal timeline either (stage post +
# log only), so act3_robot_ota/act4_attachment_ota/act6_data_flywheel are
# reused as-is below; only the acts with internal waits get short variants.

def act1_fleet_online_short(robots: dict, attachments: list):
    stage(1, "Fleet comes online", "fleet",
          "11 robots and their attachments come online in real time")

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
    stage(2, "Zero-touch provisioning", "devices",
          "A brand new robot registers itself and its attachment with zero manual setup")

    robot12 = robots[12]
    robot12.register()
    robot12.start_heartbeat()
    wait_range(0.3, 0.5)

    leaf_collector = next(a for a in attachments if a.serial == "AT-LC-2026-00002")
    leaf_collector.register()
    leaf_collector.start_heartbeat()

    log("Act 2 complete — Chorerobot-00012 is live with its Leaf Collector attached.")


def act5_remote_diagnostics_short():
    stage(5, "Remote diagnostics", "remoteops",
          "Remote commands reach the fleet instantly — self-test dispatched, acked, executed")
    log("Standing by — short mode skips the offline/recovery window (the 20s offline "
        "threshold can't fit in a 15s clip); the self-test command history is the visual.")


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

    act3_robot_ota(robots)                          # t≈5.2 (stage post only)
    wait(3.4)                                        # land Act 4 at t≈8.6

    act4_attachment_ota(attachments)                # t≈8.6 (stage post only)
    wait(3.2)                                        # land Act 5 at t≈11.8

    act5_remote_diagnostics_short()                 # t≈11.8 (stage post only)
    wait(2.4)                                        # land Act 6 at t≈14.2

    act6_data_flywheel()                            # t≈14.2 (stage post only)
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
