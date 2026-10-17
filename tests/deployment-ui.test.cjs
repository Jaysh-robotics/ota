const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../server/static/deployment.js'), 'utf8');

function setup(fetch) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: id.includes('version') ? '2.0.0' : '', innerHTML: 'Deploy', textContent: '',
      disabled: false, dataset: {}, querySelectorAll: () => [],
      setAttribute() {}, removeAttribute() {},
    });
    return elements.get(id);
  };
  const toasts = [], log = [];
  const context = vm.createContext({
    fetch, AbortController, setTimeout, clearTimeout, FormData,
    currentUser: { permissions: ['deploy:trigger'] }, fleet: {}, attachments: {},
    rolloutTrackers: {}, showToast: (message, kind) => toasts.push({ message, kind }),
    addLog: message => log.push(message), renderRolloutProgress() {},
    escapeHtml: text => String(text || '').replaceAll('<', '&lt;'),
    document: { addEventListener() {}, getElementById: element,
      querySelectorAll: () => [{ value: 'R1', checked: true, disabled: false }] },
  });
  vm.runInContext(source, context);
  return { context, elements, element, toasts, log,
    run: code => vm.runInContext(code, context) };
}

test('HTTP errors never create a success toast or rollout tracker', async () => {
  const ui = setup(async () => ({ ok: false, status: 403, json: async () => ({ detail: 'Deployment permission was revoked' }) }));
  await ui.run("submitDeployment('robot')");
  assert.deepEqual(ui.toasts, [{ message: 'Deployment permission was revoked', kind: 'error' }]);
  assert.deepEqual(ui.context.rolloutTrackers, {});
  assert.equal(ui.log.length, 0);
  assert.match(ui.element('readiness-robot').innerHTML, /Deployment not confirmed/);
});

test('a readiness conflict displays the backend reason and queues no tracker', async () => {
  const preview = { ready_count: 0, blocked_count: 1, errors: [], blocked: [{ name: 'R1', reason: 'Already running this version' }] };
  const ui = setup(async () => ({ ok: false, status: 409, json: async () => ({ detail: { message: 'Readiness changed', preview } }) }));
  await ui.run("submitDeployment('robot')");
  assert.match(ui.element('readiness-robot').innerHTML, /Already running this version/);
  assert.equal(ui.toasts[0].kind, 'error');
});

test('network failures do not claim successful deployment', async () => {
  const ui = setup(async () => { throw new Error('Network unavailable'); });
  await ui.run("submitDeployment('robot')");
  assert.equal(ui.toasts[0].kind, 'error');
  assert.deepEqual(ui.context.rolloutTrackers, {});
  assert.equal(ui.element('deploy-btn').disabled, false);
});

test('empty server-confirmed target lists never fall back to selected IDs', async () => {
  const ui = setup(async () => ({ ok: true, json: async () => ({ status: 'queued', targets: [] }) }));
  await ui.run("submitDeployment('robot')");
  assert.equal(ui.toasts[0].kind, 'error');
  assert.deepEqual(ui.context.rolloutTrackers, {});
});

test('only server-confirmed targets are tracked on success', async () => {
  const ui = setup(async () => ({ ok: true, json: async () => ({ status: 'queued', targets: ['R2'] }) }));
  await ui.run("submitDeployment('robot')");
  assert.equal(ui.toasts[0].kind, 'success');
  assert.deepEqual([...ui.context.rolloutTrackers.robot.targetIds], ['R2']);
});

test('double clicking while a deployment is pending sends one request', async () => {
  let resolve, calls = 0;
  const ui = setup(() => { calls++; return new Promise(done => { resolve = done; }); });
  const first = ui.run("submitDeployment('robot')");
  await ui.run("submitDeployment('robot')");
  assert.equal(calls, 1);
  resolve({ ok: true, json: async () => ({ status: 'queued', targets: ['R1'] }) });
  await first;
  assert.equal(ui.toasts.length, 1);
});

test('live target rendering preserves a deliberate deselection', () => {
  const ui = setup(async () => ({}));
  ui.context.currentUser = null; // No requests needed for this rendering test.
  ui.context.statusBadge = () => '<span>Online</span>';
  ui.context.fleet = { R1: { name: 'Robot', current_version: '1.0.0', lifecycle: 'active', status: 'online', update_status: 'idle' } };
  const list = ui.element('deploy-robot-list');
  ui.run("renderDeploymentTargets('robot')");
  assert.match(list.innerHTML, / checked/);
  list.querySelectorAll = () => [{ value: 'R1', checked: false, disabled: false }];
  ui.run("renderDeploymentTargets('robot')");
  assert.doesNotMatch(list.innerHTML, / checked/);
  // A newly arriving device must not silently broaden an existing selection.
  ui.context.fleet.R2 = { ...ui.context.fleet.R1, name: 'New robot' };
  ui.run("renderDeploymentTargets('robot')");
  assert.doesNotMatch(list.innerHTML, / checked/);
});
