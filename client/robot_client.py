#!/usr/bin/env python3
"""
Chore Robotics OTA client daemon.

Emulates a robot booting up, registering itself with the OTA server,
and polling for firmware updates. Run this on any machine to have it
show up live on the OTA dashboard.
"""
import argparse
import json
import socket
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

sys.stdout.reconfigure(line_buffering=True)

STATE_DIR = Path.home() / ".chorebot"
STATE_FILE = STATE_DIR / "state.json"

DEFAULT_SERVER = "http://3.139.62.253"
DEFAULT_VERSION = "1.0.0"
DEFAULT_INTERVAL = 5


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def new_robot_id() -> str:
    parts = uuid.uuid4().hex.upper()
    return f"CHORE-{parts[0:4]}-{parts[4:8]}"


def load_state(reset: bool) -> dict | None:
    """Only the robot's identity persists across runs. The firmware version is
    intentionally NOT persisted — this is a demo rig, so every rerun boots the
    robot back at its baseline version, as if the last update was never made
    permanent. This is what makes pushed updates "temporary" for demo purposes."""
    if reset and STATE_FILE.exists():
        STATE_FILE.unlink()
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return None


def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def apply_update(target_version: str):
    """Simulated update routine: runs a fake precondition check (docked,
    charging, etc.) then "installs" the update. No real download/flash yet —
    this proves the push-notification path end to end for the demo."""
    print()
    print(f"  \U0001f527 UPDATE TRIGGER RECEIVED — target version v{target_version}")
    print("  Running pre-update checks...")
    time.sleep(1)
    print("    ✓ Robot docked")
    time.sleep(0.6)
    print("    ✓ Battery charging")
    time.sleep(0.6)
    print("    ✓ Safe to update")
    time.sleep(0.8)
    print(f"  Installing firmware v{target_version}...")
    time.sleep(1.2)
    print(f"  ✓ Update complete — now running v{target_version}")
    print()


def register(server: str, robot_id: str, name: str, model: str, version: str):
    resp = requests.post(
        f"{server}/api/robot/register",
        json={"robot_id": robot_id, "name": name, "model": model, "version": version},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def check(server: str, robot_id: str, version: str) -> dict:
    resp = requests.get(
        f"{server}/api/robot/check",
        params={"robot_id": robot_id, "version": version},
        timeout=10,
    )
    if resp.status_code == 404:
        return {"not_registered": True}
    resp.raise_for_status()
    return resp.json()


def ack(server: str, robot_id: str, status: str, version: str = None):
    payload = {"robot_id": robot_id, "status": status}
    if version:
        payload["version"] = version
    resp = requests.post(f"{server}/api/robot/ack", json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def main():
    parser = argparse.ArgumentParser(description="Chore Robotics OTA client simulator")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="OTA server base URL")
    parser.add_argument("--id", dest="robot_id", default=None, help="Manually set robot ID (overrides persisted ID)")
    parser.add_argument("--version", default=None, help="Baseline firmware version to boot with (every run resets to this)")
    parser.add_argument("--name", default=None, help="Robot display name")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Poll interval in seconds")
    parser.add_argument("--reset", action="store_true", help="Forget persisted identity and boot as a brand new robot")
    args = parser.parse_args()

    state = load_state(reset=args.reset)
    version = args.version or DEFAULT_VERSION

    if state is None:
        robot_id = args.robot_id or new_robot_id()
        print("=" * 56)
        print("  FIRST BOOT DETECTED — provisioning new robot identity")
        print("=" * 56)
        save_state({"robot_id": robot_id})
    else:
        robot_id = args.robot_id or state["robot_id"]
        print("=" * 56)
        print("  RESUMING KNOWN ROBOT — booting at baseline firmware")
        print("=" * 56)

    name = args.name or f"ChoreBot-{socket.gethostname().split('.')[0]}"
    model = "Simulated Client (laptop)"

    print(f"  Robot ID   : {robot_id}")
    print(f"  Name       : {name}")
    print(f"  Firmware   : v{version}")
    print(f"  OTA Server : {args.server}")
    print("=" * 56)
    print()

    log("Booting robot control stack...")
    time.sleep(0.5)
    log(f"Loaded firmware v{version}")
    time.sleep(0.5)
    log(f"Connecting to OTA server at {args.server} ...")

    while True:
        try:
            register(args.server, robot_id, name, model, version)
            log("Registered with OTA server. Now polling for updates.")
            break
        except requests.exceptions.RequestException as e:
            log(f"Could not reach server ({e}). Retrying in {args.interval}s...")
            time.sleep(args.interval)

    current_version = version

    try:
        while True:
            try:
                result = check(args.server, robot_id, current_version)
                if result.get("not_registered"):
                    log("Server does not recognize this robot (database was likely cleared). Re-registering...")
                    register(args.server, robot_id, name, model, current_version)
                    log("Re-registered with OTA server.")
                elif result.get("update_available"):
                    target = result["target_version"]
                    ack(args.server, robot_id, "triggered")
                    apply_update(target)
                    current_version = target
                    ack(args.server, robot_id, "idle", version=current_version)
                    log(f"Now running v{current_version}. Awaiting next deployment.")
            except requests.exceptions.RequestException as e:
                log(f"Connection error: {e}")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        log("Shutting down robot simulator.")
        sys.exit(0)


if __name__ == "__main__":
    main()
