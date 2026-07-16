# Chore Robotics — Fleet + Attachments + Demo Simulation Spec

This is the binding contract for the backend (server/app.py + simulator/fleet_simulator.py)
and the frontend (server/static/index.html). Both sides must match this exactly.

## 1. Naming & serial conventions

### Robots (main units)
- Serial format: `CR-2026-NNNNN` (5-digit, zero-padded). Enterprise fleet serials are
  **sequential**: `CR-2026-00001` … `CR-2026-00012`.
- `robot_id` == serial (the serial IS the identifier).
- Display name: `Chorerobot-NNNNN` matching the serial, e.g. serial `CR-2026-00007`
  → name `Chorerobot-00007`.
- Model names (hardware platform, NOT the serial prefix): `ChoreBot X1`, `ChoreBot X2`,
  `ChoreBot X2 Pro`. Spread across the fleet.
- Robot firmware versions: semver `1.2.4`, `1.3.0`, `1.3.1` (demo pushes `1.3.1`).

### Attachments
- Serial format: `AT-<TYPE>-2026-NNNNN`, sequential per type.
- Types and codes (exactly these four):
  | Code | Type          |
  |------|---------------|
  | SP   | Snow Plower   |
  | LM   | Lawn Mower    |
  | SR   | Snow Remover  |
  | LC   | Leaf Collector|
- Example: `AT-LM-2026-00002`, `AT-SP-2026-00001`.
- Attachments have their OWN firmware, versioned per type, e.g. Lawn Mower fw `2.4.0` →
  demo pushes `2.4.1`.
- Each attachment is either attached to one robot (`attached_to = <robot serial>`) or
  in inventory (`attached_to = null`).

## 2. Backend data model (server/app.py)

Persisted in `fleet_db.json` as `{"robots": {...}, "attachments": {...}}`.
(Migrate on load: if the file is the old flat robot dict, wrap it as `robots` and add
empty `attachments`.)

Robot record (keyed by serial):
```json
{
  "name": "Chorerobot-00001",
  "model": "ChoreBot X1",
  "serial": "CR-2026-00001",
  "hw_rev": "B",
  "current_version": "1.3.0",
  "pending_version": null,
  "update_status": "idle",          // idle | pending | triggered
  "first_seen": iso, "last_seen": iso
}
```

Attachment record (keyed by serial):
```json
{
  "type": "Lawn Mower",
  "type_code": "LM",
  "serial": "AT-LM-2026-00001",
  "attached_to": "CR-2026-00003",   // or null
  "current_version": "2.4.0",
  "pending_version": null,
  "update_status": "idle",
  "first_seen": iso, "last_seen": iso
}
```

Attachment online/offline is derived the same way as robots (last_seen within 20s),
except: an attachment with `attached_to = null` reports status `"inventory"`.

## 3. API endpoints

Keep ALL existing endpoints working (robot register/check/ack, /deploy, /upload,
/api/fleet, /api/versions, /api/clear, /ws). Additions/changes:

- `GET /api/fleet` → `{"robots": {...}, "attachments": {...}}` (views with derived status).
- `POST /api/robot/register` → body gains optional `serial`, `hw_rev` (default serial =
  robot_id, hw_rev = "A").
- `POST /api/attachment/register` → `{attachment_id, type, type_code, version, attached_to}`
- `GET /api/attachment/check?attachment_id=&version=` → same semantics as robot check
  (404 if unknown; updates last_seen; returns `{update_available, target_version}`).
- `POST /api/attachment/ack` → `{attachment_id, status, version?}` (same semantics as robot ack).
- `POST /deploy` → unchanged (robots).
- `POST /deploy/attachment` → `{version, type_code, attachment_ids?}`; null ids = all
  attachments of that type. Sets pending_version/update_status.
- `POST /api/demo/stage` → `{stage: int, title: str, section: str, caption: str}` —
  server just broadcasts it (see WS below) and returns `{"status":"ok"}`.

### WebSocket messages (server → dashboard)
- `init`: `{type:"init", fleet: {robots, attachments}, versions, attachment_versions,
  target_version, attachment_targets}`
  - `attachment_versions`: `[{type_code, version, filename?, size?, uploaded_at}]` —
    maintained in memory/db; seeded with baseline versions (no real files needed).
  - `attachment_targets`: `{ "LM": "2.4.1", ... }` current target per type (empty = none).
- `fleet_update`: `{type:"fleet_update", fleet: {robots, attachments}}` — **shape change**,
  frontend must read `.robots` / `.attachments`.
- `versions_update`, `target_update` — unchanged for robots; add
  `attachment_target_update`: `{type:"attachment_target_update", targets: {LM: "2.4.1"}}`.
- `toast` — unchanged.
- `demo_stage`: `{type:"demo_stage", stage, title, section, caption}` — frontend
  auto-navigates to `section` (a section name from the sidebar) and shows the caption bar.

## 4. Frontend requirements (server/static/index.html)

- Parse the new `{robots, attachments}` fleet shape everywhere (all existing renderers
  read robots; add attachments where specified).
- **Mock data** (shown only when server DB is empty, as today): rewrite to the new
  naming — 12 robots `Chorerobot-00001..00012` / serials `CR-2026-00001..00012`, models
  ChoreBot X1/X2/X2 Pro, plus ~12 attachments across the 4 types with the AT- serials.
- **Fleet Overview table**: show an attachment chip on robots that have one
  (e.g. `🔧 Lawn Mower · AT-LM-2026-00002`).
- **Device Registry section**: robots table shows Serial (monospace, prominent), HW Rev,
  Lifecycle, attached attachment. ADD a second table "Attachment Registry": Serial, Type,
  Firmware, Attached To (robot name or "In inventory"), Status, Update state.
  Update the compatibility matrix to the four real attachment types
  (Snow Plower / Lawn Mower / Snow Remover / Leaf Collector firmware vs HW Rev A/B/C).
- **Deploy section**: add an "Attachment Firmware" card — pick type + version, target
  attachments, POST /deploy/attachment. Show attachment deployment status alongside robots'.
- **Robot drawer**: show serial + attached attachment (with its firmware + update state).
- **Data Flywheel section**: new sidebar item under Platform. A circular flywheel visual
  (SVG, animatable) with the loop: Robots in the field → Telemetry & fault data →
  Fleet insights (predictive maintenance, quality scorecards) → Firmware improvements →
  OTA deployment → Better robots → (back to start). Each node shows a live counter fed
  from real state (robots online, telemetry pts/s, faults tracked, versions shipped,
  updates delivered). This is the closing shot of the video — make it beautiful.
- **Demo Mode**: on `demo_stage` WS message: switch to `section`, show a slim caption
  bar (top of content area) with `Act <stage> — <title>` and caption text, subtle
  fade-in. No manual clicks needed during recording. A small "DEMO" chip may show in
  the topbar while stages are active.
- Keep everything else (telemetry, faults, predictive, etc.) working with the new naming.

## 5. Fleet simulator (simulator/fleet_simulator.py)

Python 3, `requests` only. Drives the REAL server — every event in the video is real
API traffic. **NO fallbacks anywhere: any failed request must raise and crash loudly.**

CLI: `python3 fleet_simulator.py --server http://localhost:8000 [--fast]`
(`--fast` divides all sleeps by 4 for testing).

Fleet: 12 robots (serials/names per §1; 11 pre-registered, robot 00012 held back for
the live-registration act), 12 attachments:
3× SP, 4× LM, 2× SR, 3× LC; ~9 attached to robots, rest in inventory.
Heartbeats: every robot + attached attachment checks in every 4–6s (threaded), for the
entire run, so the dashboard stays live. Two robots go silent mid-demo is NOT wanted —
keep all online except one (`CR-2026-00009`) which stops heartbeating during Act 5 to
trigger diagnostics attention, then resumes.

### Demo timeline (~2m30s total; each act = POST /api/demo/stage first)
| Act | t | Section | What the simulator does |
|-----|------|---------|-------------------------|
| 1 "Fleet comes online" | 0:00 | fleet | Registers 11 robots + their attachments over ~12s (staggered), heartbeats start. |
| 2 "Zero-touch provisioning" | 0:25 | devices | Robot CR-2026-00012 (Chorerobot-00012) registers live with its Leaf Collector attachment. |
| 3 "Robot OTA rollout" | 0:45 | deploy | POSTs /deploy v1.3.1 to the 6 robots on 1.3.0/1.2.4; robots ack triggered→install (6–10s each, staggered)→ack idle at 1.3.1. |
| 4 "Attachment OTA" | 1:25 | deploy | POSTs /deploy/attachment LM v2.4.1; the 4 lawn mowers update the same way. |
| 5 "Remote diagnostics" | 1:50 | remoteops | CR-2026-00009 stops heartbeating ~15s (goes offline on dashboard), then resumes + posts a recovery toast via its check-in. |
| 6 "The data flywheel" | 2:10 | flywheel | Just navigates; heartbeats keep counters moving. Ends ~2:30. |

Console output should narrate acts clearly (nice for a picture-in-picture terminal shot).

## 6. V2 REVISION — "real operator" demo (supersedes conflicting parts above)

The demo must look like a real human driving a real dashboard. Recorded raw; the
user adds labels in video post-production.

### 6.1 No visible demo chrome
- REMOVE the caption bar ("ACT n — TITLE") and the "● DEMO" topbar chip entirely.
- `demo_stage` WS messages remain the sync signal, but they render NOTHING.
  They only trigger cursor choreography (below).

### 6.2 Virtual cursor
- A fake mouse pointer (SVG arrow, ~20px, subtle drop shadow) rendered fixed-position
  above everything (z-index above drawer/toasts), visible only while a demo is running
  (first demo_stage shows it; it fades out 3s after the last choreography step).
- Movement: animated with cubic ease-in-out, 600–1200ms per leg depending on distance,
  ±6px random curve/overshoot so it feels human. Brief 150–400ms pauses before clicks.
- Click: pointer dips (scale .9) + expanding ripple ring at the click point, then the
  engine dispatches a REAL .click() on the target element (and for <select>, sets
  .value + dispatches change after the click pause — native dropdowns can't be animated).
- Engine API: an async runner over steps like
  `[{move:'#selector'},{click:'#selector'},{selectValue:['#sel','1.3.1']},{pause:800},{scrollTo:'#selector'}]`
  resolving targets by CSS selector at execution time; if a selector matches nothing,
  log to console and skip the step (do NOT crash mid-video), but never fabricate state.

### 6.3 Choreography per stage (frontend, keyed by demo_stage.stage)
1. Stay on Fleet Overview while robots register. Cursor drifts naturally, hovers a
   robot row (~0:12), clicks it to open the drawer, lingers ~3s, closes it.
2. Cursor clicks "Device Registry" nav; after ~4s scrolls smoothly to the Attachment
   Registry table; hovers a row.
3. Cursor clicks "Deploy Updates" nav → clicks version select, value '1.3.1' →
   clicks "Select All Available" → pauses ~700ms → clicks "Push Update to Fleet".
   Rollout progress card (6.4) becomes the visual focus.
4. Cursor clicks attachment type select (value 'LM') → attachment version select
   (value '2.4.1') → "Select All" for attachments → "Push". Progress card again.
5. Cursor clicks "Remote Operations" nav → picks robot CR-2026-00009 in the target
   select → clicks "Run Self-Test". Command history shows sent → acked → executed.
   (The simulator's offline/recovery for 00009 happens in this window too.)
6. Cursor clicks "Data Flywheel" nav, moves gently to center, then fades out.

### 6.4 Active Rollout progress card (Deploy section, top)
- Appears (or fills) when a rollout is in flight; shows: target version, animated
  progress bar (percent = targeted devices now on target / total targeted),
  counters: "Updated N / M robots", "In progress: K", elapsed time. Same card handles
  attachment rollouts ("Updated 3 / 4 Lawn Mowers · v2.4.1").
- Derived 100% from real fleet state on fleet_update — no faked numbers. Must read
  like it would with 2,000 robots: aggregates only, no per-robot rows.
- When a rollout completes: bar green + ONE toast ("Rollout complete — 11 robots on
  v1.3.1").

### 6.5 Toast policy (server + frontend)
Real dashboards don't toast every device event. Total toasts in the 2:30 video ≤ 5.
- Server: REMOVE toast broadcasts for robot/attachment registration and for
  "received update trigger" acks. KEEP: deploy queued (1 per deploy action),
  offline→online recovery alert, database cleared.
- Frontend: no new per-device toasts; registrations show up as climbing stats +
  activity log entries only.

### 6.6 Simulator changes
- The simulator NO LONGER calls /deploy or /deploy/attachment. The virtual cursor's
  real button clicks do that. Stages 3/4 just broadcast demo_stage and wait out the
  window.
- Installs must be REACTIVE inside the heartbeat threads: any check that returns
  update_available → ack "triggered" → 6–10s → ack "idle" with the new version,
  regardless of which stage is active. (Robots respond to whatever the dashboard
  operator pushes — that's the point.)
- Timeline/timings otherwise unchanged.

## 7. Hard rules
- **No fallback logic.** If the server is unreachable or any request fails, the
  simulator exits non-zero with the error. Never substitute fake success.
- Don't break `client/robot_client.py` — it must still register (it becomes a
  13th ad-hoc robot if run).
- All timestamps ISO, same as today. Keep the 20s online threshold.

## 8. SHORT MODE — 15-second feature reel

A second simulator mode for recording a ~15.5s feature-showcase clip instead
of the full ~2:30 demo. Same fleet, same real API traffic, same six acts —
just staged back-to-back instead of spread over 2:30.

### 8.1 CLI
`python3 fleet_simulator.py --server http://localhost:8000 --short`
`--short` is mutually exclusive with `--fast` (the simulator errors out via
`parser.error` if both are passed — there's no meaningful "fast + short"
combination).

### 8.2 `tempo` field on `/api/demo/stage`
- `DemoStageRequest` gains an optional `tempo: str | None = None` field.
- The server's `/api/demo/stage` handler broadcasts it unchanged:
  `demo_stage` WS messages now carry `"tempo": <value>` (`"short"` or `null`).
- The simulator's `stage()` includes `"tempo": "short"` in the POST body only
  when `--short` is active; it's omitted (→ `null` on the wire) otherwise, so
  the normal ~2:30 timeline is unaffected.
- **Frontend contract**: the dashboard selects the fast choreography variant
  and runs the virtual cursor at ~0.4× normal physics (shorter move/pause
  durations) whenever `demo_stage.tempo === "short"`. This lets one cursor
  engine drive both timelines from the same `demo_stage` messages, keyed
  purely off `tempo`.

### 8.3 Compressed timings
Two module-level tunables replace the previously-hardcoded ranges, applied to
**both** `RobotAgent` and `AttachmentAgent` (`_loop` heartbeat interval and
`_install` duration):

| Tunable | Normal | `--short` |
|---|---|---|
| Heartbeat interval | 4–6s | 0.8–1.4s |
| Install duration | 6–10s | 1.0–1.8s |

Installs are still purely reactive inside the heartbeat threads regardless of
mode — the simulator never calls `/deploy` or `/deploy/attachment` itself,
short mode included.

### 8.4 Short timeline (`run_demo_short`, ~15.5s total)
After `upload_firmware_catalog()` and fleet build, the simulator prints a
"START RECORDING NOW" banner with a 3-2-1 countdown (1s apart) — a 15s clip
has no slack for the operator to react, so recording must already be rolling
before Act 1 fires.

| Act | ~t | Section | What the simulator does |
|-----|-----|---------|--------------------------|
| 1 "Fleet comes online" | 0.0s | fleet | Same job list as the normal Act 1 (11 robots + their attachments + non-held-back inventory attachments), staggered over ~2.0s total instead of ~12s. |
| 2 "Zero-touch provisioning" | 2.6s | devices | Robot 12 registers, ~0.3–0.5s gap, then leaf collector `AT-LC-2026-00002` registers. |
| 3 "Robot OTA rollout" | 5.2s | deploy | Stage post only — operator cursor pushes v1.3.1. |
| 4 "Attachment OTA" | 8.6s | deploy | Stage post only — operator cursor pushes Lawn Mower v2.4.1. |
| 5 "Remote diagnostics" | 11.8s | remoteops | Stage post only. Caption is about remote commands (dispatched/acked/executed), not "going quiet" — see 8.5. |
| 6 "The data flywheel" | 14.2s | flywheel | Stage post, then a closing banner: short timeline complete (~15s), heartbeats continue until Ctrl+C. |

Console act banners/logs are kept in short mode too — they're used for
picture-in-picture terminal shots just like the full demo.

### 8.5 Short mode skips the offline window
The normal Act 5 pauses `CR-2026-00009`'s heartbeats for ~24s to cross the
20s `ONLINE_THRESHOLD_SECONDS` and show the offline→recovery toast. That
cannot fit inside a 15s clip, so short mode's Act 5 does **not** pause any
robot's heartbeats — the dashboard's self-test command history (sent → acked
→ executed) is the visual instead, driven by the frontend's own choreography
for that stage.
