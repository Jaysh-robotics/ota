// Shared deployment controls: stable selections, server readiness, honest results.
const deploymentChannels = {
  robot: { prefix: 'chk-', list: 'deploy-robot-list', version: 'deploy-version-select', button: 'deploy-btn', endpoint: '/deploy', label: 'robots' },
  jetson: { prefix: 'jchk-', list: 'deploy-jetson-robot-list', version: 'deploy-jetson-version-select', button: 'deploy-jetson-btn', endpoint: '/deploy/jetson', label: 'Jetson modules' },
  attachment: { prefix: 'achk-', list: 'deploy-attachment-list', version: 'deploy-attachment-version', button: 'deploy-attachment-btn', endpoint: '/deploy/attachment', label: 'attachments' },
};
const deploymentState = Object.fromEntries(Object.keys(deploymentChannels).map(channel =>
  [channel, { selections: new Map(), initialized: false, revision: 0, timer: null, submitting: false }]));

function canTriggerDeployment() {
  return Boolean(currentUser && (currentUser.permissions || []).some(p => p === '*' || p === 'deploy:trigger'));
}

async function requestJSON(url, options = {}) {
  const { timeoutMs = 15000, ...fetchOptions } = options;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...fetchOptions, signal: controller.signal });
    let data;
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) {
      const detail = data.detail;
      const message = typeof detail === 'string' ? detail : Array.isArray(detail)
        ? detail.map(item => item.msg).join('; ') : detail?.message;
      const error = new Error(message || `Request failed (${response.status}). Please try again.`);
      error.preview = detail?.preview;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('The server took too long to respond. Check deployment status before retrying.');
    if (error instanceof TypeError) throw new Error('Cannot reach the server. Check your connection and try again.');
    throw error;
  } finally { clearTimeout(timeout); }
}

function deploymentSelection(channel) {
  const config = deploymentChannels[channel];
  return [...document.querySelectorAll(`#${config.list} input[type="checkbox"]`)]
    .filter(input => input.checked && !input.disabled).map(input => input.value);
}

function unavailableReason(channel, device, version) {
  const jetson = channel === 'jetson';
  if ((device.lifecycle || 'active') !== 'active') return 'Lifecycle is not active';
  if (device.status !== 'online') return 'Offline or not paired';
  if (channel === 'attachment') {
    const host = fleet[device.attached_to];
    if (!host || host.status !== 'online' || (host.lifecycle || 'active') !== 'active') return 'Paired robot must be online and active';
  }
  if (!['idle', 'failed'].includes(device[jetson ? 'jetson_update_status' : 'update_status'] || 'idle') ||
      device[jetson ? 'jetson_pending_version' : 'pending_version']) return 'Update already queued or running';
  if (version && device[jetson ? 'jetson_version' : 'current_version'] === version) return 'Already on this version';
  return '';
}

function renderDeploymentTargets(channel) {
  const config = deploymentChannels[channel], state = deploymentState[channel];
  const container = document.getElementById(config.list);
  if (!container) return;
  container.querySelectorAll('input[type="checkbox"]').forEach(input => {
    if (!input.disabled) state.selections.set(input.value, input.checked);
  });
  const type = document.getElementById('deploy-attachment-type').value;
  const version = document.getElementById(config.version).value;
  const devices = channel === 'attachment' ? Object.entries(attachments).filter(([, a]) => a.type_code === type) : Object.entries(fleet);
  if (channel === 'attachment' && !type) {
    container.innerHTML = '<div class="empty">Select an attachment type above</div>';
    scheduleDeploymentPreview(channel);
    return;
  }
  container.innerHTML = devices.map(([id, device]) => {
    const reason = unavailableReason(channel, device, version);
    if (!state.selections.has(id)) state.selections.set(id, !state.initialized && !reason);
    const selected = !reason && state.selections.get(id);
    const currentVersion = device[channel === 'jetson' ? 'jetson_version' : 'current_version'] || 'Unknown';
    const name = channel === 'attachment' ? (device.serial || id) : (device.name || id);
    const where = channel === 'attachment' ? (fleet[device.attached_to]?.name || device.attached_to || 'Inventory') : id;
    return `<div class="deploy-target${reason ? ' target-unavailable' : ''}">
      <div class="deployment-target-label">
        <input type="checkbox" id="${config.prefix}${escapeHtml(id)}" value="${escapeHtml(id)}" ${reason ? 'disabled' : ''} ${selected ? 'checked' : ''}>
        <label for="${config.prefix}${escapeHtml(id)}">
          <span class="deployment-target-name">${escapeHtml(name)}</span>
          <span class="deployment-target-meta">${escapeHtml(where)} · v${escapeHtml(currentVersion)} · ${escapeHtml(reason || device.status)}</span>
        </label>
      </div>
      ${channel === 'attachment' ? attachmentStatusBadge(device) : statusBadge(device)}
    </div>`;
  }).join('') || '<div class="empty">No devices available in this group</div>';
  if (devices.length) state.initialized = true;
  updateDeployVisual(channel);
  scheduleDeploymentPreview(channel);
}

function selectDeploymentTargets(channel, selected) {
  const config = deploymentChannels[channel], state = deploymentState[channel];
  document.getElementById(config.list).querySelectorAll('input[type="checkbox"]').forEach(input => {
    if (!input.disabled) { input.checked = selected; state.selections.set(input.value, selected); }
  });
  updateDeployVisual(channel);
  scheduleDeploymentPreview(channel);
}

function showDeploymentReadiness(channel, { title, detail = '', preview = null, error = false }) {
  const container = document.getElementById(`readiness-${channel}`);
  if (!container) return;
  container.dataset.state = error ? 'blocked' : preview?.can_deploy ? 'ready' : 'idle';
  const items = preview ? [...preview.errors, ...preview.blocked.map(item => `${item.name}: ${item.reason}`)] : [];
  container.innerHTML = `<div class="readiness-heading"><strong>${escapeHtml(title)}</strong>
    ${preview ? `<span>${preview.ready_count} ready · ${preview.blocked_count} blocked</span>` : ''}</div>
    ${detail ? `<p>${escapeHtml(detail)}</p>` : ''}
    ${items.length ? `<ul>${items.slice(0, 4).map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : ''}
    ${items.length > 4 ? `<p>And ${items.length - 4} more. Update your selection and check again.</p>` : ''}`;
}

function scheduleDeploymentPreview(channel) {
  const config = deploymentChannels[channel], state = deploymentState[channel];
  clearTimeout(state.timer);
  updateDeployVisual(channel);
  const revision = ++state.revision;
  const button = document.getElementById(config.button);
  button.disabled = true;
  if (state.submitting) return;
  if (!canTriggerDeployment()) {
    showDeploymentReadiness(channel, { title: 'Deployment access required', detail: 'Your role needs permission to deploy firmware.' });
    return;
  }
  const version = document.getElementById(config.version).value;
  const ids = deploymentSelection(channel);
  if (!version || !ids.length) {
    showDeploymentReadiness(channel, { title: !version ? 'Choose a release to check readiness' : 'Select devices to check readiness',
      detail: 'Online, active devices with no update in progress are eligible.' });
    return;
  }
  showDeploymentReadiness(channel, { title: `Checking ${ids.length} selected device${ids.length === 1 ? '' : 's'}…` });
  state.timer = setTimeout(async () => {
    try {
      const preview = await requestJSON('/api/deploy/preview', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel, version, device_ids: ids,
          type_code: channel === 'attachment' ? document.getElementById('deploy-attachment-type').value : null }),
      });
      if (revision !== state.revision || state.submitting) return;
      button.disabled = !preview.can_deploy || !canTriggerDeployment();
      showDeploymentReadiness(channel, { title: preview.can_deploy ? `Ready to deploy v${version}` : 'Selection needs attention',
        detail: preview.can_deploy ? `${ids.length} selected. Live status is checked again when you deploy.` : 'No devices will be queued until these checks pass.',
        preview, error: !preview.can_deploy });
    } catch (error) {
      if (revision !== state.revision) return;
      showDeploymentReadiness(channel, { title: 'Readiness check unavailable', detail: error.message, error: true });
    }
  }, 200);
}

async function submitDeployment(channel, explicitIds = null, explicitVersion = null) {
  const config = deploymentChannels[channel], state = deploymentState[channel];
  if (state.submitting) return;
  const version = explicitVersion || document.getElementById(config.version).value;
  const ids = explicitIds || deploymentSelection(channel);
  if (!version || !ids.length) { showToast('Select a release and at least one device', 'error'); return; }
  const type = channel === 'attachment' ? document.getElementById('deploy-attachment-type').value : null;
  const button = document.getElementById(config.button), original = button.innerHTML;
  let queuedCount = 0;
  state.submitting = true;
  ++state.revision;
  clearTimeout(state.timer);
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  button.textContent = 'Queueing deployment…';
  try {
    const data = await requestJSON(config.endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version, ...(channel === 'attachment' ? { attachment_ids: ids, type_code: type } : { robot_ids: ids }) }) });
    if (data.status !== 'queued' || !Array.isArray(data.targets) || !data.targets.length) throw new Error('The server did not confirm any queued devices. Refresh fleet status before retrying.');
    queuedCount = data.targets.length;
    const label = queuedCount === 1 ? { robot: 'robot', jetson: 'Jetson module', attachment: 'attachment' }[channel] : config.label;
    showToast(`v${version} queued for ${queuedCount} ${label}`, 'success');
    addLog(`Deployment confirmed: v${version} → ${data.targets.length} ${config.label}`);
    rolloutTrackers[channel] = { version, label: config.label, typeCode: type,
      targetIds: new Set(data.targets), startedAt: Date.now(), completed: false, collapseAt: null };
    renderRolloutProgress();
  } catch (error) {
    showToast(error.message, 'error');
    showDeploymentReadiness(channel, { title: 'Deployment not confirmed', detail: error.message, preview: error.preview, error: true });
  } finally {
    state.submitting = false;
    button.innerHTML = original;
    button.removeAttribute('aria-busy');
    // Keep an error explanation visible. The retry explicitly revalidates server state.
    button.disabled = Boolean(queuedCount) || !canTriggerDeployment();
    if (queuedCount) showDeploymentReadiness(channel, { title: 'Deployment queued',
      detail: `${queuedCount} device${queuedCount === 1 ? '' : 's'} confirmed. Follow progress in Active Rollout.` });
  }
}

async function uploadFirmware(channel) {
  const jetson = channel === 'jetson';
  const fileInput = document.getElementById(jetson ? 'jetsonFileInput' : 'fileInput');
  const versionInput = document.getElementById(jetson ? 'jetsonVersionInput' : 'versionInput');
  const button = document.getElementById(jetson ? 'upload-jetson-btn' : 'upload-robot-btn');
  const file = fileInput.files[0], version = versionInput.value.trim();
  if (!file || !version) { showToast('Choose a firmware package and enter its version', 'error'); return; }
  if (button.disabled) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Uploading…';
  try {
    const body = new FormData(); body.append('file', file);
    await requestJSON(`${jetson ? '/upload/jetson' : '/upload'}?version=${encodeURIComponent(version)}`, { method: 'POST', body, timeoutMs: 180000 });
    showToast(`${jetson ? 'Jetson ' : ''}v${version} uploaded successfully`, 'success');
    versionInput.value = ''; fileInput.value = '';
    document.getElementById(jetson ? 'jetson-file-name' : 'file-name').textContent = '';
  } catch (error) { showToast(error.message, 'error'); }
  finally { button.disabled = false; button.textContent = original; }
}

document.addEventListener('change', event => {
  for (const [channel, config] of Object.entries(deploymentChannels)) {
    if (event.target.id === config.version) renderDeploymentTargets(channel);
    else if (event.target.closest(`#${config.list}`)) scheduleDeploymentPreview(channel);
  }
});

// Presentation helpers. Hidden search matches retain their explicit selections.
function switchDeployChannel(channel) {
  if (!deploymentChannels[channel]) return;
  Object.keys(deploymentChannels).forEach(key => {
    const selected = key === channel;
    document.getElementById(`dp-panel-${key}`).hidden = !selected;
    const tab = document.getElementById(`dp-tab-${key}`);
    tab.classList.toggle('active', selected);
    tab.setAttribute('aria-pressed', String(selected));
  });
  updateDeployVisual(channel);
}
function switchUploadChannel(channel) {
  const channels = ['robot', 'jetson'];
  if (!channels.includes(channel)) return;
  channels.forEach(key => {
    const selected = key === channel;
    const panel = document.getElementById(`upload-panel-${key}`);
    if (panel) panel.hidden = !selected;
    const tab = document.getElementById(`upload-tab-${key}`);
    if (tab) {
      tab.classList.toggle('active', selected);
      tab.setAttribute('aria-pressed', String(selected));
    }
  });
}
function filterDeployDevices(channel) {
  const search = document.getElementById(`dp-search-${channel}`);
  if (!search) return;
  const query = search.value.trim().toLowerCase();
  document.getElementById(deploymentChannels[channel].list).querySelectorAll('.deploy-target').forEach(row => {
    row.hidden = !(row.textContent || '').toLowerCase().includes(query);
  });
}
function updateDeployVisual(channel) {
  const config = deploymentChannels[channel];
  const counter = document.getElementById(`dp-selection-${channel}`);
  if (!counter) return;
  const rows = [...document.getElementById(config.list).querySelectorAll('input[type="checkbox"]')];
  const selected = rows.filter(input => input.checked && !input.disabled).length;
  const available = rows.filter(input => !input.disabled).length;
  counter.textContent = `${selected} selected · ${available} available`;
  const count = document.getElementById(`dp-count-${channel}`);
  count.textContent = channel === 'attachment' ? Object.keys(attachments).length : Object.keys(fleet).length;
  filterDeployDevices(channel);
}
