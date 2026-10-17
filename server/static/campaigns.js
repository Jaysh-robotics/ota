/* Presentation-only campaign model. No deployment requests or device mutations. */
let campaignDemoStage = 2;
let campaignDemoPaused = false;
let campaignDemoFilter = 'all';
function campaignDemoAction(action) {
  if (action === 'pause') campaignDemoPaused = !campaignDemoPaused;
  if (action === 'advance' && !campaignDemoPaused) campaignDemoStage = Math.min(4, campaignDemoStage + 1);
  if (action === 'reset') { campaignDemoStage = 2; campaignDemoPaused = false; }
  renderCampaignVisual();
}
function campaignDemoFilterChange(value) { campaignDemoFilter = value; renderCampaignVisual(); }
function renderCampaignVisual() {
  const root = document.getElementById('campaign-visual');
  if (!root) return;
  const finished = campaignDemoStage === 4;
  const complete = [0, 12, 48, 96, 120][campaignDemoStage];
  const active = finished ? 0 : 12;
  const queued = 120 - complete - active;
  const pct = Math.round(complete / 120 * 100);
  const status = finished ? 'Completed' : campaignDemoPaused ? 'Paused' : 'In progress';
  const waves = [['01','Canary','5%','6 robots'],['02','Early access','10%','12 robots'],['03','Fleet expansion','50%','60 robots'],['04','Full fleet','100%','120 robots']];
  const devices = Array.from({length:12}, (_,i) => {
    const state = finished || i < 5 ? 'Updated' : i < 9 ? (campaignDemoPaused ? 'Paused' : 'Installing') : 'Queued';
    return {id:`CR-${String(1041+i).padStart(4,'0')}`,site:['Warehouse A','Warehouse B','Assembly floor'][i%3],state,progress:state==='Updated'?100:state==='Queued'?0:Math.min(95,42+(i-5)*13)};
  });
  root.innerHTML = `
    <div class="cv-demo"><span><strong>CAMPAIGN PREVIEW</strong> &nbsp; Sample fleet · Interactive presentation</span><span>No device commands are sent</span></div>
    <div class="card cv-hero">
      <div class="cv-hero-top"><div><div class="cv-eyebrow">FIRMWARE CAMPAIGN &nbsp; / &nbsp; CMP-2026-014</div><h2>Smarter cleaning. One fleet.</h2><p>Navigation &amp; motion update <strong>v1.2.0</strong> <span class="cv-meta">from v1.1.0 · Base controller</span></p></div><span class="stat-badge ${finished?'badge-green':'badge-orange'}">● ${status}</span></div>
      <div class="cv-hero-bottom"><div><strong>Production fleet</strong><span>3 sites · 120 robots · Started today, 10:30</span></div><div class="cv-actions"><button class="btn btn-ghost" onclick="campaignDemoAction('reset')">Reset demo</button><button class="btn btn-ghost" ${finished?'disabled':''} onclick="campaignDemoAction('pause')">${campaignDemoPaused?'Resume preview':'Pause preview'}</button><button class="btn btn-primary" ${finished||campaignDemoPaused?'disabled':''} onclick="campaignDemoAction('advance')">${finished?'Rollout complete':'Advance wave →'}</button></div></div>
    </div>
    <div class="cv-metrics">${[['Fleet updated',complete,'of 120 robots','green'],['Installing',active,campaignDemoPaused?'Preview paused':'Receiving firmware','orange'],['Waiting',queued,'Scheduled for next waves','gray'],['Failed',0,'No failures in this scenario','green']].map(([label,value,sub,color])=>`<div class="card cv-metric"><span>${label}</span><strong class="cv-${color}">${value}</strong><small>${sub}</small></div>`).join('')}</div>
    <div class="cv-columns"><div>
      <div class="card cv-panel"><div class="cv-heading"><div><h3>Rollout journey</h3><p>Validate a small cohort, then expand with confidence.</p></div><strong class="cv-percent">${pct}%</strong></div><div class="cv-track" role="progressbar" aria-label="Demo fleet updated" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><div style="width:${pct}%"></div></div>
      <div class="cv-waves">${waves.map(([num,name,size,count],i)=>`<div class="cv-wave ${i<campaignDemoStage?'done':i===campaignDemoStage?'current':''}"><div class="cv-wave-number">${i<campaignDemoStage?'✓':num}</div><strong>${name}</strong><b>${size}</b><span>${count}</span><small>${i<campaignDemoStage?'Completed':i===campaignDemoStage?(campaignDemoPaused?'Paused':'Active wave'):'Upcoming'}</small></div>`).join('')}</div>
      <div class="cv-callout"><span>✓</span><div><strong>${finished?'All waves completed':campaignDemoPaused?'Rollout preview paused':'Health gate passed'}</strong><p>${finished?'All 120 demo robots are on v1.2.0.':campaignDemoPaused?'Resume the preview to continue to the next wave.':'Canary cohort is healthy. No failures observed in the sample rollout.'}</p></div></div></div>
      <div class="card cv-panel"><div class="cv-heading"><div><h3>Device rollout</h3><p>Representative devices across the selected sites</p></div><label class="cv-filter">Status <select aria-label="Filter demo devices by status" onchange="campaignDemoFilterChange(this.value)">${['all','Updated','Installing','Paused','Queued'].map(x=>`<option value="${x}" ${campaignDemoFilter===x?'selected':''}>${x==='all'?'All devices':x}</option>`).join('')}</select></label></div>
      <div class="cv-table-wrap"><table class="fleet-table cv-table"><thead><tr><th>Device / site</th><th>Version</th><th>Progress</th><th>Status</th></tr></thead><tbody>${devices.filter(d=>campaignDemoFilter==='all'||d.state===campaignDemoFilter).map(d=>`<tr><td><strong>${d.id}</strong><small>${d.site}</small></td><td class="cv-mono">${d.state==='Updated'?'v1.2.0':'v1.1.0'}</td><td><div class="cv-mini-track"><div style="width:${d.progress}%"></div></div><small>${d.progress}%</small></td><td><span class="stat-badge ${d.state==='Updated'?'badge-green':d.state==='Queued'?'badge-gray':'badge-orange'}">${d.state}</span></td></tr>`).join('')||'<tr><td colspan="4" class="empty">No demo devices with this status.</td></tr>'}</tbody></table></div></div>
    </div><aside>
      <div class="card cv-panel"><div class="cv-heading"><h3>Rollout safeguards</h3><span class="stat-badge badge-green">Preview policy</span></div><div class="cv-policy"><span>Automatic pause</span><strong>Failure rate &gt; 5%</strong></div><div class="cv-policy"><span>Minimum battery</span><strong>20%</strong></div><div class="cv-policy"><span>Install conditions</span><strong>Docked &amp; idle</strong></div><div class="cv-policy"><span>Observation window</span><strong>10 minutes / wave</strong></div><div class="cv-policy"><span>Recovery release</span><strong>v1.1.0</strong></div><p class="cv-footnote">Illustrative policy for this campaign preview.</p></div>
      <div class="card cv-panel"><div class="cv-heading"><h3>Campaign activity</h3><span class="cv-meta">Today</span></div><ol class="cv-timeline">${[[finished?'Rollout completed':campaignDemoPaused?'Preview paused':campaignDemoStage===3?'Full fleet wave started':'Fleet expansion started',finished?'All devices on target':campaignDemoPaused?'Waiting for operator':`Wave ${campaignDemoStage+1} · staged deployment`,'Now'],['Health checks passed','Canary and early access verified','10:42'],['Early access completed','12 robots updated successfully','10:40'],['Canary completed','6 robots · no failures reported','10:34'],['Campaign created','v1.2.0 → Production fleet','10:30']].map(([title,desc,time])=>`<li><div><strong>${title}</strong><time>${time}</time></div><p>${desc}</p></li>`).join('')}</ol></div>
    </aside></div>`;
}
