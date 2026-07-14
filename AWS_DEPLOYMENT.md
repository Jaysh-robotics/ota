# AWS Compute Requirements — Chore Robotics OTA Fleet Manager

Draft sizing/infra plan for running `server/app.py` (FastAPI + WebSocket dashboard, already Dockerized) on AWS.

## Current architecture (as of this build)

- Single-process FastAPI app, run via `uvicorn app:app`
- **State is in-memory + a local JSON file** (`fleet_db.json`) — no database
- **Firmware binaries stored on local disk** (`updates/*.tar.gz`), served via StaticFiles
- **Real-time updates** pushed over a single WebSocket (`/ws`); the list of connected dashboard clients (`active_connections`) lives in process memory
- Already has a `Dockerfile` — container-ready, not yet cloud-deployed

The important consequence: **this app can only run as a single process today.** Multiple instances would each have their own disconnected copy of the fleet state and their own WebSocket client list. That's fine for a pilot, not fine at scale — see "Non-negotiable changes" below.

---

## Sizing tiers

### Tier 1 — Pilot / internal beta (≤200 robots, <10 dashboard users)
Run exactly what exists today, just hosted.

| Component | Recommendation |
|---|---|
| Compute | 1× ECS Fargate task, 0.5 vCPU / 1GB RAM (or `t4g.small` EC2 if you'd rather not containerize yet) |
| Storage | 20GB gp3 EBS/EFS for `updates/` + `fleet_db.json` |
| Load balancer | ALB (supports WebSocket natively) |
| TLS/DNS | ACM cert + Route53 |
| **Est. cost** | **~$50–75/mo** |

No code changes required. Deploy the existing Dockerfile as-is.

### Tier 2 — Early fleet (200–2,000 robots)
Same compute footprint, but stop relying on local disk for anything that needs to survive a restart or be shared.

| Component | Recommendation |
|---|---|
| Compute | 1× Fargate task, 1 vCPU / 2GB |
| Firmware storage | Move to **S3 + CloudFront** — firmware images (often 5–50MB) shouldn't be served from the app's own disk/bandwidth, and Fargate disk is ephemeral anyway |
| Fleet state | Move `fleet_db.json` → **DynamoDB** (single-table, robot_id as key — near drop-in replacement for the current dict-of-dicts) |
| **Est. cost** | **~$60–100/mo** |

### Tier 3 — Production scale (2,000–50,000+ robots)
This is where the single-process assumption breaks and requires actual re-architecture, not just bigger boxes:

- Run **2+ Fargate tasks** behind the ALB with WebSocket sticky sessions, OR
- Move the broadcast fan-out to a shared layer (**ElastiCache Redis pub/sub** or SNS) so a robot event reaching instance A still notifies a dashboard client connected to instance B
- DynamoDB on-demand (carried over from Tier 2)
- S3 + CloudFront mandatory at this point (bandwidth alone would saturate a single instance)
- Auto Scaling on the Fargate service (target tracking on CPU or request count)
- **Est. cost:** **$300–800+/mo**, driven mostly by data transfer and task count

---

## Non-negotiable changes before scaling past Tier 1

1. **Replace the JSON file.** `json.dump()` on every check-in works with one writer at low volume; it will corrupt or drop writes once robot check-in traffic gets concurrent (a few hundred robots polling every few seconds is enough to hit this).
2. **Move firmware binaries to S3.** Local/EBS storage doesn't scale with fleet size or with concurrent downloads, and Fargate's local disk doesn't persist across task restarts/deploys.
3. **The in-memory `fleet` dict and `active_connections` list cap you at one process.** That's the real ceiling on Tier 1 — not CPU or RAM.

## Suggested AWS service list

- **Compute:** ECS Fargate (Dockerfile already exists — least new work) or EC2 Auto Scaling Group
- **Load balancing:** Application Load Balancer (WebSocket-native)
- **Firmware storage:** S3 + CloudFront
- **Database:** DynamoDB
- **Fan-out (Tier 3+):** ElastiCache Redis or SNS
- **TLS/DNS:** ACM + Route53
- **Secrets:** Secrets Manager / SSM Parameter Store (for when auth is added)
- **Observability:** CloudWatch Logs + Container Insights
- **CI/CD:** ECR + GitHub Actions (or CodeBuild/CodePipeline) → ECS deploy

## Recommendation

Given this is still early (dashboard currently seeded with sample data), start at **Tier 1**: one Fargate task + ALB + S3 for firmware artifacts. Fastest path to a real URL, under $75/mo, zero code changes. Do the Tier 2 changes (DynamoDB + S3) *before* onboarding real robots beyond a handful — the JSON file is the part most likely to bite you first.
