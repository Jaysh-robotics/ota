# OTA deployment reliability and UI improvements

## Implemented

- Added `POST /api/deploy/preview` for robot, Jetson and attachment readiness. The same validation runs immediately before queueing, so a stale preview cannot bypass checks.
- Explicit empty target lists mean no devices; only omitted/null lists mean the entire known channel population. IDs are deduplicated. Unknown devices, unavailable releases, inactive/offline devices, existing updates and already-installed versions block the whole request without partially queueing a cohort.
- Attachment deployment requires a matching catalog release and an online, active paired robot. It no longer invents a new catalog version during deployment. The existing seeded attachment catalog remains a demo catalog, not evidence of a downloadable signed package.
- Enforced authentication and existing role permissions for deployment, preview, upload and database-clear operations.
- Persisted the base-controller target across restarts, alongside Jetson and attachment targets. JSON snapshots use a temporary file, flush/fsync and atomic replacement to avoid partially written state.
- Uploads validate version syntax, reject empty/oversized packages, return SHA-256/size metadata and publish atomically. Existing versions cannot be overwritten, including concurrent submissions. The upload limit is 256 MiB per package. This does not perform firmware signature or archive-content validation.
- Dashboard readiness panels show eligible counts and rejection reasons. Deployment results use only server-confirmed targets. HTTP/network errors do not create success toasts or rollout trackers, and repeat clicks are suppressed while a request is in progress.
- Robot/attachment selections survive live refreshes. Newly arriving devices do not silently join an existing selection.
- Added keyboard-accessible navigation links, associated form labels, visible focus, readable primary button text and narrow-screen navigation/layout.

## Design contract

Audience: fleet operators and firmware engineers. Primary task: choose a release and a specific device cohort, check readiness, and queue a deployment with a reliable result. Stack: existing FastAPI backend and vanilla HTML/CSS/JavaScript. The existing dashboard is the design authority; no separate approved brand version was present in this OTA folder.

The implementation reuses `--orange`, `--gold`, `--surface2`, `--border`, `--text`, `--text2`, existing cards, badges and buttons. Desktop navigation remains a sidebar; at 600 px and below it becomes a horizontally scrollable navigation strip. This pass targets keyboard usability and readable responsive states, not a claim of full WCAG conformance.

## Run and test

From the `ota` directory:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_deployments.py -q
node --test tests/deployment-ui.test.cjs
python -m uvicorn server.app:app --host 127.0.0.1 --port 8000
```

For an isolated preview, set `CHORE_DATA_DIR` to a separate directory before starting the server. It stores fleet snapshots and uploaded packages there while using the existing static assets. The default remains `server/`. Use one server worker: in-memory state is not coordinated across multiple worker processes.

The simulator reuses existing catalog versions. When versions are missing, upload them through the signed-in dashboard or supply `CHORE_OPERATOR_TOKEN` in the simulator environment. It no longer attempts to overwrite catalog releases or upload anonymously.

## Validation record

- 30 Python tests passed for backend requests, atomic storage and simulator catalog behavior.
- 7 JavaScript tests passed for HTTP/network failures, readiness conflicts, server-confirmed targets, duplicate clicks and selection retention.
- Browser checks covered the existing 1280 px desktop viewport, 900 px tablet layout, and 390/320 px narrow layouts. Readiness, disabled targets, live selection retention, a one-robot deployment and its rollout status were exercised against an isolated preview fleet. No console errors were observed during these checks.
- JavaScript syntax checks and Git whitespace checks passed. Existing FastAPI startup-event deprecation warnings remain.

## Next improvements identified

1. **Complete operator and device authentication.** Other pre-existing operator routes still have optional-user checks; enforce route-specific permissions throughout. Device registration, polling and acknowledgements need device identities rather than trusting submitted IDs. Remove demo credentials/default signing-key fallbacks before an internet-facing deployment.
2. **Signed, verified release manifests.** Verify signatures and hardware compatibility on devices, validate archive contents, and persist checksums with the catalog. SHA-256 returned on upload is an integrity identifier, not a signature or device-side verification.
3. **Durable campaigns and acknowledgements.** Store per-target campaign records, correlate acknowledgements with a deployment ID, record failures/timeouts and permit explicit retry or rollback. Current acknowledgements can still report a version without a campaign identity.
4. **Transactional persistence.** Move fleet/campaign/audit state to SQLite or PostgreSQL before multiple workers or large fleets. Atomic JSON writes prevent partial snapshots but do not provide cross-process transactions.
5. **Replace remaining illustrative panels.** Several dashboard sections still contain mock metrics and campaign controls. Wire them to persisted API data and clearly label sample content.

No release was sent to real devices during validation. Tests use temporary storage and an isolated, clearly named preview fleet.

## Campaign presentation view

The Campaigns page now provides a clearly labeled, frontend-only sample campaign with wave progression, pause/resume/reset, device status filtering, fleet totals, safeguards and a timeline. It reuses existing brand tokens and never sends device commands. Persistent campaign orchestration and actual rollback remain future backend work.

## Deploy page visual refinement

Added focused Base Controller / Jetson / Attachment channel selectors, compact numbered steps, device search and live selected/available counts. Search only changes visibility; selected devices remain selected. Existing readiness and dispatch handlers remain active. Reused the dashboard typography, orange accent and cards. Seven deployment UI regression tests passed.
