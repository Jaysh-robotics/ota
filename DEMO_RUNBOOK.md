# Demo Video Runbook — Chore Robotics Fleet Platform

A hands-free ~2:30 demo that looks like a real operator at work. A virtual mouse
cursor moves, hovers, and clicks the actual UI — it selects the firmware version,
clicks "Select All Available", pushes the update, runs a self-test. Every deploy
is a real button click hitting the real API; robots react on their own poll cycle.
No captions or demo chrome appear on screen — add your labels in post-production.
You only press record.

## Setup (once)

```bash
# Terminal 1 — server
cd server
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000

# Terminal 2 — simulator deps
cd simulator
pip install -r requirements.txt
```

Open http://localhost:8000 in a clean browser window (hide bookmarks bar, full
screen, 100% zoom). With an empty DB you'll see sample data — that's fine; the
simulator replaces it the moment Act 1 starts.

## Recording

1. Start your screen recorder (QuickTime / OBS) on the browser window.
2. If the DB isn't empty from a previous run, click **Clear Database** first.
3. Run:

```bash
# Terminal 2
python3 fleet_simulator.py --server http://localhost:8000
```

4. Do nothing. The dashboard switches sections by itself. Stop recording ~10s
   after Act 6's flywheel shot. Ctrl+C the simulator when done.

`--fast` runs the whole timeline in ~38s for rehearsal. (In fast mode the
Chorerobot-00009 offline blip won't cross the 20s offline threshold — that
part only shows at real speed.)

Keep the browser window in the foreground and don't move your real mouse over
it during recording — the virtual cursor is doing the driving.

## Timeline (what the viewer sees — the cursor does all of this itself)

| Time | Scene | Section shown | What happens |
|------|-------|---------------|--------------|
| 0:00 | Fleet comes online | Fleet Overview | Simulator uploads the firmware catalog, then 11 robots (Chorerobot-00001…00011, serials CR-2026-…) + attachments register live; stats and charts fill in while the cursor hovers rows. |
| 0:25 | Zero-touch provisioning | Device Registry | Cursor opens the registry; Chorerobot-00012 registers itself with its Leaf Collector; cursor scrolls to the Attachment Registry and opens a robot drawer. |
| 0:45 | Robot OTA rollout | Deploy Updates | Cursor opens the version dropdown, picks v1.3.1 → "Select All Available" → "Push Update to Fleet", then scrolls up to the Active Rollout progress bar for ~3s. |
| 1:03 | Attachment OTA | Deploy Updates | Cursor moves down to the attachment card, picks Lawn Mower + v2.4.1, pushes — then back up to the progress bar for ~3s. |
| 1:19 | Remote diagnostics | Remote Operations | Cursor targets Chorerobot-00009, runs a Self-Test, then rests on Command History while sent → acked → executed plays out. 00009 also goes briefly silent and recovers with one alert toast. |
| ~1:55 | Flywheel + closing | Data Flywheel → Fleet Overview | ~3s on the animated flywheel, then the cursor returns to the main dashboard and fades out. Total ≈ 2:05. |

Total toasts on screen for the whole video: ~4 (two deploy confirmations, rollout
complete, one recovery alert). Everything else surfaces through stats, tables,
progress bars, and the activity log — like a real dashboard.

## Fleet reference

- Robots: `CR-2026-00001` … `CR-2026-00012`, names `Chorerobot-00001` … `00012`,
  models ChoreBot X1 / X2 / X2 Pro.
- Attachments: 3 Snow Plower (`AT-SP-…`), 4 Lawn Mower (`AT-LM-…`),
  2 Snow Remover (`AT-SR-…`), 3 Leaf Collector (`AT-LC-…`); 10 attached, 2 in inventory.

## Notes

- The simulator has **no fallback logic**: if the server is unreachable or any
  call fails, it exits non-zero with the error. A clean run means everything on
  screen was real API traffic.
- After the timeline ends the simulator keeps heartbeating so you can linger on
  any section; robots stay green until you Ctrl+C.
- Re-record anytime: Ctrl+C simulator → Clear Database in the dashboard → rerun.

## 15-second short cut

For a ~15s feature-showcase clip instead of the full demo, use `--short`. It
plays the same six acts with the same real API traffic, just staged
back-to-back (see DEMO_SPEC.md §8 for the exact timeline and timings).

1. Click **Clear Database** in the dashboard (same as any re-record).
2. Start your screen recorder on the browser window.
3. Run:

```bash
# Terminal 2
python3 fleet_simulator.py --server http://localhost:8000 --short
```

4. The simulator prints a "START RECORDING NOW" banner followed by a 3-2-1
   countdown (1s apart) before Act 1 fires — a 15s clip has no slack, so make
   sure recording is already rolling by the time the countdown starts, and
   use the countdown itself as your cue for when Act 1 begins on screen.
5. Do nothing else. The six acts land at roughly t=0.0 / 2.6 / 5.2 / 8.6 /
   11.8 / 14.2s. Stop recording ~2s after the flywheel appears (~16–17s total
   recording). Ctrl+C the simulator when done.

Notes specific to short mode:
- `--short` and `--fast` are mutually exclusive — the simulator errors out if
  you pass both.
- Heartbeat and install timings are compressed (0.8–1.4s heartbeats, 1.0–1.8s
  installs) so a full OTA install cycle is still visible on screen in the
  short window.
- Short mode never triggers the Chorerobot-00009 offline/recovery beat — the
  20s offline threshold can't fit in a 15s clip, so Act 5's visual is the
  self-test command history instead.
- Re-record flow is the same as the main runbook: Ctrl+C simulator → Clear
  Database in the dashboard → rerun.
