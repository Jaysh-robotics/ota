import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def simulator(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'simulator' / 'fleet_simulator.py'
    spec = importlib.util.spec_from_file_location('catalog_test_simulator', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv('CHORE_OPERATOR_TOKEN', raising=False)
    monkeypatch.setattr(module, 'log', lambda _: None)
    return module


def test_reuses_existing_releases_without_upload_or_credentials(simulator, monkeypatch):
    monkeypatch.setattr(simulator, 'api_get', lambda *args: {'versions': [{'version': v} for v in simulator.ROBOT_FIRMWARE_VERSIONS]})
    post = Mock()
    monkeypatch.setattr(simulator.requests, 'post', post)
    simulator.upload_firmware_catalog()
    post.assert_not_called()


def test_missing_release_requires_operator_token(simulator, monkeypatch):
    monkeypatch.setattr(simulator, 'api_get', lambda *args: {'versions': []})
    post = Mock()
    monkeypatch.setattr(simulator.requests, 'post', post)
    with pytest.raises(RuntimeError, match='CHORE_OPERATOR_TOKEN'):
        simulator.upload_firmware_catalog()
    post.assert_not_called()


def test_uploads_only_missing_versions_with_operator_identity(simulator, monkeypatch):
    monkeypatch.setattr(simulator, 'api_get', lambda *args: {'versions': [{'version': '1.2.4'}, {'version': '1.3.0'}]})
    monkeypatch.setattr(simulator, 'make_firmware_blob', lambda _: b'test-package')
    monkeypatch.setenv('CHORE_OPERATOR_TOKEN', 'test-only-token')
    post = Mock()
    monkeypatch.setattr(simulator.requests, 'post', post)
    simulator.upload_firmware_catalog()
    post.assert_called_once()
    assert post.call_args.kwargs['params'] == {'version': '1.3.1'}
    assert post.call_args.kwargs['headers'] == {'Authorization': 'Bearer test-only-token'}
