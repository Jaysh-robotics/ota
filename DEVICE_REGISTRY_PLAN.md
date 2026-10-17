# Device & Attachment Registry — Implementation Plan

## 1. Overview & Objective
The Device Registry is the central hardware inventory and lifecycle management system for ChoreRobotics base robots, Jetson compute modules, and tool attachments (Snow Plowers, Lawn Mowers, Snow Removers, Leaf Collectors).

---

## 2. Architecture & Data Model

### Robot Record Schema Extension (`fleet_db.json`)
```json
{
  "robot_id": "CR-2026-00001",
  "name": "Chorerobot-00001",
  "serial": "CR-2026-00001",
  "model": "ChoreBot X1",
  "hw_rev": "B",
  "carrier_rev": "1.2",
  "jetson_serial": "NV-JET-2026-00101",
  "current_version": "1.3.0",
  "pending_version": null,
  "update_status": "idle",
  "jetson_version": "1.0.0",
  "jetson_pending_version": null,
  "jetson_update_status": "idle",
  "lifecycle": "active",
  "tags": ["cohort:pilot-2026", "region:us-west", "depot:san-jose"],
  "manufactured_date": "2026-01-15T08:00:00Z",
  "commissioned_date": "2026-02-01T10:00:00Z",
  "location": "San Jose Fleet Yard",
  "notes": "Assigned to North Turf Maintenance",
  "first_seen": "2026-02-01T10:00:00Z",
  "last_seen": "2026-08-25T18:00:00Z"
}
```

### Attachment Record Schema Extension (`fleet_db.json`)
```json
{
  "attachment_id": "AT-LM-2026-00001",
  "serial": "AT-LM-2026-00001",
  "type": "Lawn Mower",
  "type_code": "LM",
  "attached_to": "CR-2026-00003",
  "current_version": "2.4.0",
  "pending_version": null,
  "update_status": "idle",
  "lifecycle": "active",
  "paired_history": [
    {
      "robot_id": "CR-2026-00003",
      "paired_at": "2026-06-10T12:00:00Z",
      "unpaired_at": null
    }
  ],
  "first_seen": "2026-06-10T12:00:00Z",
  "last_seen": "2026-08-25T18:00:00Z"
}
```

---

## 3. REST API Specifications

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/devices` | Query robots & attachments with multi-field filtering (`q`, `lifecycle`, `hw_rev`, `tag`, `model`, `attached`) |
| `PATCH` | `/api/robot/{robot_id}` | Update robot metadata (`name`, `lifecycle`, `tags`, `location`, `notes`) |
| `POST` | `/api/robot/{robot_id}/tags` | Add tag(s) to a robot |
| `DELETE` | `/api/robot/{robot_id}/tags/{tag}` | Remove a tag from a robot |
| `POST` | `/api/attachment/{attachment_id}/pair` | Pair an unattached attachment with a target robot (`attached_to: robot_id`) |
| `POST` | `/api/attachment/{attachment_id}/unpair` | Unpair attachment and return it to inventory (`attached_to: null`) |
| `GET` | `/api/devices/compatibility` | Returns dynamic compatibility matrix of HW revisions vs Attachment firmware |
| `POST` | `/api/devices/{device_id}/decommission` | Transition device to `decommissioned` state |

---

## 4. Dynamic Compatibility Engine

```python
COMPATIBILITY_RULES = {
    "SP": {"min_fw": "1.1.0", "supported_hw_revs": ["A", "B"]},
    "LM": {"min_fw": "2.4.0", "supported_hw_revs": ["A", "B", "C"]},
    "SR": {"min_fw": "1.0.0", "supported_hw_revs": ["B", "C"]},
    "LC": {"min_fw": "3.0.0", "supported_hw_revs": ["A", "B", "C"]},
}
```
- Validates pairing requests before committing.
- Emits warnings or blocks pairing when hardware revision is incompatible with attachment type/firmware.

---

## 5. UI Upgrades (`server/static/index.html`)

1. **KPI Header Banner:** Total Robots, Active, In Maintenance, Decommissioned, Total Attachments, In Inventory, Attached.
2. **Advanced Search & Filter Bar:** Full-text search on serial/name/tag, with filter chips.
3. **Interactive Device Table:** Lifecycle state selector, clickable tag pills, action menus (Edit, Pair, Detach, Decommission).
4. **Interactive Attachment Table:** "Attach to Robot" modal selector for inventory units, "Detach" one-click action.
5. **Dynamic Compatibility Card:** Live grid rendered from backend rules.
