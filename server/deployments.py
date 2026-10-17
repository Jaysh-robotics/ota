"""Side-effect-free deployment readiness checks shared by preview and dispatch."""
import re

VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z")


def valid_version(version: str) -> bool:
    return len(version) <= 64 and bool(VERSION_PATTERN.fullmatch(version))


def deployment_preview(channel, version, device_ids, devices, releases, type_code=None, hosts=None):
    """An explicit empty list means nobody. Only None means all known devices."""
    release_exists = any(r['version'] == version and
                         (channel != 'attachment' or r.get('type_code') == type_code)
                         for r in releases)
    ids = list(dict.fromkeys(device_ids if device_ids is not None else [
        key for key, value in devices.items()
        if channel != 'attachment' or value.get('type_code') == type_code
    ]))
    targets, blocked = [], []
    version_key = 'jetson_version' if channel == 'jetson' else 'current_version'
    status_key = 'jetson_update_status' if channel == 'jetson' else 'update_status'
    pending_key = 'jetson_pending_version' if channel == 'jetson' else 'pending_version'
    for device_id in ids:
        device = devices.get(device_id)
        reason = None
        if not device:
            reason = 'Device is no longer registered'
        elif channel == 'attachment' and device.get('type_code') != type_code:
            reason = 'Attachment type does not match this release'
        elif device.get('lifecycle', 'active') != 'active':
            reason = 'Device lifecycle is not active'
        elif device.get('status') != 'online':
            reason = 'Device is offline or not paired'
        elif channel == 'attachment' and not device.get('attached_to'):
            reason = 'Attachment is not paired to a robot'
        elif channel == 'attachment' and (
            not (hosts or {}).get(device.get('attached_to')) or
            hosts[device['attached_to']].get('status') != 'online' or
            hosts[device['attached_to']].get('lifecycle', 'active') != 'active'
        ):
            reason = 'Paired robot must be online and active'
        elif device.get(status_key, 'idle') not in ('idle', 'failed') or device.get(pending_key):
            reason = 'Another update is already queued or running'
        elif device.get(version_key) == version:
            reason = 'Already running this version'
        if reason:
            blocked.append({'id': device_id, 'name': (device or {}).get('name', device_id), 'reason': reason})
        else:
            targets.append(device_id)
    errors = []
    if not valid_version(version):
        errors.append('Enter a valid release version, such as 1.3.0')
    if not release_exists:
        errors.append('This release is not available in the firmware catalog')
    if not ids:
        errors.append('Select at least one device')
    return {'channel': channel, 'version': version, 'type_code': type_code,
            'requested_count': len(ids), 'ready_count': len(targets),
            'blocked_count': len(blocked), 'targets': targets, 'blocked': blocked,
            'errors': errors, 'can_deploy': bool(targets) and not blocked and not errors}
