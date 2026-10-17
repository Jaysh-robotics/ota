import importlib.util
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVER = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER))
from storage import atomic_write_json, store_release


def robot(**overrides):
    return {'name': 'Test robot', 'current_version': '1.0.0', 'jetson_version': '1.0.0',
            'lifecycle': 'active', 'last_seen': datetime.now().isoformat(),
            'update_status': 'idle', 'jetson_update_status': 'idle',
            'pending_version': None, 'jetson_pending_version': None, **overrides}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv('CHORE_DATA_DIR', str(tmp_path))
    spec = importlib.util.spec_from_file_location('ota_test_app', SERVER / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.robots['R1'] = robot()
    module.robots['R2'] = robot(name='Second robot')
    (tmp_path / 'updates' / 'update_2.0.0.tar.gz').write_bytes(b'release')
    (tmp_path / 'updates' / 'jetson' / 'jetson_2.0.0.tar.gz').write_bytes(b'compute release')
    # Exercise real permission dependencies, substituting only the identity provider.
    module.app.dependency_overrides[module.get_current_user] = lambda: {
        'id': 'test-admin', 'role': 'super_admin', 'permissions': ['*']}
    with TestClient(module.app) as client:
        yield module, client, tmp_path


def test_preview_has_no_side_effects(api):
    module, client, path = api
    response = client.post('/api/deploy/preview', json={'channel': 'robot', 'version': '2.0.0', 'device_ids': ['R1']})
    assert response.status_code == 200
    assert response.json()['can_deploy'] is True
    assert response.json()['targets'] == ['R1']
    assert module.robots['R1']['pending_version'] is None
    assert not (path / 'fleet_db.json').exists()


@pytest.mark.parametrize('endpoint', ['/deploy', '/deploy/jetson'])
def test_empty_selection_never_targets_whole_fleet(api, endpoint):
    module, client, path = api
    response = client.post(endpoint, json={'version': '2.0.0', 'robot_ids': []})
    assert response.status_code == 409
    assert 'Select at least one device' in response.json()['detail']['preview']['errors']
    assert all(r['pending_version'] is None and r['jetson_pending_version'] is None for r in module.robots.values())
    assert not (path / 'fleet_db.json').exists()


@pytest.mark.parametrize('payload', [
    {'version': '9.0.0', 'robot_ids': ['R1']},
    {'version': '2.0.0', 'robot_ids': ['R1', 'missing']},
    {'version': '../../invalid', 'robot_ids': ['R1']},
])
def test_rejects_missing_release_or_target_without_partial_queue(api, payload):
    module, client, _ = api
    response = client.post('/deploy', json=payload)
    assert response.status_code == 409
    assert module.robots['R1']['pending_version'] is None
    assert module.current_target_version == ''


@pytest.mark.parametrize('overrides', [
    {'lifecycle': 'decommissioned'},
    {'lifecycle': 'maintenance'},
    {'last_seen': (datetime.now() - timedelta(minutes=2)).isoformat()},
    {'last_seen': 'invalid timestamp'},
    {'pending_version': '1.5.0', 'update_status': 'pending'},
    {'current_version': '2.0.0'},
])
def test_rechecks_device_readiness_and_preserves_entire_selection(api, overrides):
    module, client, _ = api
    module.robots['R2'].update(overrides)
    response = client.post('/deploy', json={'version': '2.0.0', 'robot_ids': ['R1', 'R2']})
    assert response.status_code == 409
    preview = response.json()['detail']['preview']
    assert preview['ready_count'] == 1 and preview['blocked_count'] == 1
    assert module.robots['R1']['pending_version'] is None


def test_deduplicates_and_persists_base_target_across_reload(api):
    module, client, path = api
    response = client.post('/deploy', json={'version': '2.0.0', 'robot_ids': ['R1', 'R1']})
    assert response.status_code == 200
    assert response.json()['targets'] == ['R1']
    state = module.load_state()
    assert state['current_target_version'] == '2.0.0'
    assert state['robots']['R1']['pending_version'] == '2.0.0'
    assert state['robots']['R2']['pending_version'] is None
    assert not list(path.glob('.fleet-*.tmp'))


def test_omitted_ids_explicitly_means_all(api):
    _, client, _ = api
    response = client.post('/deploy', json={'version': '2.0.0'})
    assert response.status_code == 200
    assert response.json()['targets'] == ['R1', 'R2']


def test_jetson_channel_is_independent(api):
    module, client, _ = api
    module.robots['R1'].update(pending_version='1.4.0', update_status='triggered')
    response = client.post('/deploy/jetson', json={'version': '2.0.0', 'robot_ids': ['R1']})
    assert response.status_code == 200
    assert module.robots['R1']['pending_version'] == '1.4.0'
    assert module.robots['R1']['jetson_pending_version'] == '2.0.0'


def test_state_change_between_preview_and_dispatch_is_rejected(api):
    module, client, _ = api
    response = client.post('/api/deploy/preview', json={'channel': 'robot', 'version': '2.0.0', 'device_ids': ['R1']})
    assert response.json()['can_deploy']
    module.robots['R1']['lifecycle'] = 'maintenance'
    assert client.post('/deploy', json={'version': '2.0.0', 'robot_ids': ['R1']}).status_code == 409


def test_attachment_requires_catalog_type_and_active_host(api):
    module, client, _ = api
    module.attachments['A1'] = robot(type_code='LM', type='Lawn Mower', attached_to='R1', serial='A1')
    payload = {'version': '2.4.1', 'type_code': 'LM', 'attachment_ids': ['A1']}
    module.robots['R1']['lifecycle'] = 'maintenance'
    assert client.post('/deploy/attachment', json=payload).status_code == 409
    module.robots['R1']['lifecycle'] = 'active'
    assert client.post('/deploy/attachment', json={**payload, 'version': '9.9.9'}).status_code == 409
    assert not any(v['version'] == '9.9.9' for v in module.attachment_versions)
    assert client.post('/deploy/attachment', json={**payload, 'attachment_ids': []}).status_code == 409
    assert client.post('/deploy/attachment', json={**payload, 'type_code': 'SP', 'version': '1.1.0'}).status_code == 409
    assert client.post('/deploy/attachment', json=payload).json()['targets'] == ['A1']


@pytest.mark.parametrize('route,payload', [
    ('/deploy', {'version': '2.0.0', 'robot_ids': ['R1']}),
    ('/deploy/jetson', {'version': '2.0.0', 'robot_ids': ['R1']}),
    ('/deploy/attachment', {'version': '2.4.1', 'type_code': 'LM', 'attachment_ids': []}),
    ('/api/deploy/preview', {'channel': 'robot', 'version': '2.0.0', 'device_ids': ['R1']}),
])
def test_anonymous_and_read_only_users_cannot_deploy(api, route, payload):
    module, client, _ = api
    module.app.dependency_overrides.clear()
    assert client.post(route, json=payload).status_code == 401
    module.app.dependency_overrides[module.get_current_user] = lambda: {'role': 'viewer', 'permissions': ['fleet:read']}
    assert client.post(route, json=payload).status_code == 403


@pytest.mark.parametrize('route,prefix', [('/upload', 'update_'), ('/upload/jetson', 'jetson_')])
def test_upload_validation_and_immutable_versions(api, route, prefix):
    module, client, path = api
    files = {'file': ('release.tar.gz', b'package bytes')}
    assert client.post(route, params={'version': '../../escape'}, files=files).status_code == 422
    assert client.post(route, params={'version': '3.0.0'}, files={'file': ('empty.tar.gz', b'')}).status_code == 422
    response = client.post(route, params={'version': '3.0.0'}, files=files)
    assert response.status_code == 200 and len(response.json()['sha256']) == 64
    assert client.post(route, params={'version': '3.0.0'}, files={'file': ('new.tar.gz', b'replacement')}).status_code == 409
    release = path / 'updates' / ('jetson' if prefix == 'jetson_' else '') / f'{prefix}3.0.0.tar.gz'
    assert release.read_bytes() == b'package bytes'
    module.app.dependency_overrides.clear()
    assert client.post(route, params={'version': '4.0.0'}, files=files).status_code == 401


def test_online_detection_handles_missing_and_timezone_timestamps(api):
    module, _, _ = api
    assert not module.device_is_online({})
    assert not module.device_is_online({'last_seen': None})
    assert module.device_is_online({'last_seen': datetime.now(timezone.utc).isoformat()})


def test_anonymous_database_clear_cannot_remove_fleet(api):
    module, client, _ = api
    module.app.dependency_overrides.clear()
    assert client.post('/api/clear').status_code == 401
    assert set(module.robots) == {'R1', 'R2'}


def test_atomic_snapshot_preserves_old_file_on_serialization_failure(tmp_path):
    path = tmp_path / 'fleet.json'
    path.write_text('{"old": true}')
    with pytest.raises(TypeError):
        atomic_write_json(path, {'cannot_serialize': object()})
    assert json.loads(path.read_text()) == {'old': True}
    assert not list(tmp_path.glob('*.tmp'))


def test_atomic_upload_size_limit_and_interrupted_stream(tmp_path):
    path = tmp_path / 'release.tar.gz'
    with pytest.raises(OverflowError):
        store_release(io.BytesIO(b'12345'), str(path), max_bytes=4)
    assert not path.exists()

    class BrokenStream:
        def read(self, _):
            raise OSError('interrupted upload')

    with pytest.raises(OSError):
        store_release(BrokenStream(), str(path))
    assert not path.exists() and not list(tmp_path.glob('*.tmp'))
