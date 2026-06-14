const state = { csrfCookieName: 'ultron_csrf', activePointerVersion: null };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function createShell() {
  const app = document.getElementById('app');
  state.csrfCookieName = app?.dataset?.csrfCookie || 'ultron_csrf';
  app.textContent = '';
  const header = el('header', 'topbar');
  const brand = el('div', 'brand');
  brand.append(el('span', 'brand-mark', 'U'), el('div', 'brand-copy'));
  brand.querySelector('.brand-copy').append(el('strong', null, 'Ultron Dashboard'), el('span', null, 'Operator ecology, evidence, safety, and metrics.'));
  const nav = el('nav', 'nav-links');
  const chat = el('a', null, 'Chat');
  chat.href = '/';
  nav.append(chat);
  header.append(brand, nav);

  const grid = el('main', 'dashboard-grid');
  for (const [id, title] of [['ecology', 'Evolution ecology'], ['runs', 'Runs & evidence'], ['personalization', 'Personalization / Self-evolution'], ['safety', 'Safety / approvals'], ['metrics', 'Metrics']]) {
    const section = el('section', 'panel');
    section.id = id;
    section.append(el('h2', null, title), el('div', 'panel-body', 'Loading…'));
    grid.append(section);
  }
  app.append(header, grid);
}

async function getJson(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok) throw new Error(`${url} failed`);
  return data;
}

async function refreshAll() {
  const [ecology, runs, ledger, personalization, metrics] = await Promise.all([
    getJson('/api/ecology'),
    getJson('/api/runs'),
    getJson('/api/ledger'),
    getJson('/api/personalization'),
    getJson('/api/metrics')
  ]);
  state.activePointerVersion = ecology.active_pointer_version;
  renderEcology(ecology);
  renderRuns(runs, ledger);
  renderPersonalization(personalization);
  renderSafety(ledger.safety || {});
  renderMetrics(metrics);
}

function body(id) {
  const target = document.querySelector(`#${id} .panel-body`);
  target.textContent = '';
  return target;
}

function renderEcology(data) {
  const parent = body('ecology');
  parent.append(el('p', 'summary', `Active pointer version ${data.active_pointer_version}`));
  const columns = el('div', 'lifecycle-grid');
  const grouped = data.modules_by_lifecycle || {};
  for (const key of ['seed', 'candidate', 'survivor', 'decaying', 'pruned', 'quarantined']) {
    const column = el('section', 'lifecycle-column');
    column.append(el('h3', null, key === 'pruned' ? 'pruned graveyard' : key));
    for (const module of grouped[key] || []) column.append(moduleCard(module));
    columns.append(column);
  }
  parent.append(columns);
  const lineage = el('section', 'subpanel');
  lineage.append(el('h3', null, 'Lineage parent → child'));
  const list = el('ul', 'lineage-list');
  for (const edge of data.lineage || []) list.append(el('li', null, `${shortHash(edge.parent_id)} → ${shortHash(edge.child_id)} (${edge.module_id})`));
  if (!data.lineage || data.lineage.length === 0) list.append(el('li', null, 'No child modules yet.'));
  lineage.append(list);
  parent.append(lineage);
}

function moduleCard(module) {
  const card = el('article', 'module-card');
  card.append(el('strong', null, `${module.module_id} v${module.version}`));
  card.append(el('span', null, `hash ${module.content_hash}`));
  card.append(el('span', null, `parent ${shortHash(module.parent_id)}`));
  const fitness = module.fitness || {};
  card.append(el('span', null, `fitness use=${fitness.usage_count || 0} state=${fitness.promotion_state || '—'} metric=${formatValue(fitness.primary_metric)} decay=${formatValue(fitness.decay_score)}`));
  return card;
}

function renderRuns(runs, ledger) {
  const parent = body('runs');
  const runPanel = el('section', 'subpanel');
  runPanel.append(el('h3', null, 'Recent RunManifests'));
  const runList = el('div', 'table-list');
  for (const run of runs.runs || []) runList.append(row(['run', run.run_id, run.workflow, run.active_module_set_hash, `${run.model_snapshot?.provider || '—'}/${run.model_snapshot?.name || '—'}`, run.created_at, run.trajectory_id]));
  if (!runs.runs || runs.runs.length === 0) runList.append(el('p', 'empty', 'No runs yet.'));
  runPanel.append(runList);

  const ledgerPanel = el('section', 'subpanel');
  ledgerPanel.append(el('h3', null, 'Append-only ledger'));
  const ledgerList = el('div', 'table-list');
  for (const entry of ledger.entries || []) ledgerList.append(row(['ledger', entry.entry_id, entry.kind, entry.module_hash, entry.canary_id || '—', entry.actor || 'system', entry.quarantined ? 'quarantined' : 'clean']));
  if (!ledger.entries || ledger.entries.length === 0) ledgerList.append(el('p', 'empty', 'No ledger entries yet.'));
  ledgerPanel.append(ledgerList);
  parent.append(runPanel, ledgerPanel);
}

function renderSafety(safety) {
  const parent = body('safety');
  parent.append(el('p', 'summary', `Canary ${safety.last_canary_id || 'none'} · candidate ${safety.last_candidate_hash || 'none'}`));
  const requests = el('section', 'subpanel');
  requests.append(el('h3', null, 'Pending permission expansions'));
  const list = el('div', 'table-list');
  for (const item of safety.pending_permission_expansions || []) list.append(row(['request', item.request_id, item.status, item.tool || '—', item.reason || '—']));
  if (!safety.pending_permission_expansions || safety.pending_permission_expansions.length === 0) list.append(el('p', 'empty', 'No pending permission requests.'));
  requests.append(list);
  const controls = el('section', 'subpanel');
  controls.append(el('h3', null, 'Gated controls'));
  controls.append(el('p', null, 'Approve, rollback, restore, and benchmark mutations remain POST /api/action gated by CSRF, session scope, pointer version, and evidence policy.'));
  parent.append(requests, controls);
}

function renderPersonalization(data) {
  const parent = body('personalization');
  const summary = data.summary || {};
  const trail = data.causal_trail || {};
  const aggregates = trail.aggregates || {};
  parent.append(el('p', 'summary', `Redacted summary ${shortHash(summary.summary_hash || aggregates.summary_hash)} · ${trail.approval_state || 'none'}`));

  const counts = el('section', 'subpanel');
  counts.append(el('h3', null, 'Usage counts'));
  const countGrid = el('div', 'metrics-grid');
  const signalCounts = aggregates.signal_counts || {};
  for (const key of ['runs', 'feedback', 'acceptances', 'corrections']) {
    const card = el('article', 'metric-card');
    card.append(el('span', 'metric-value', signalCounts[key] ?? 0), el('span', 'metric-label', key));
    countGrid.append(card);
  }
  counts.append(countGrid);

  const evidence = el('section', 'subpanel');
  evidence.append(el('h3', null, 'Evidence labels'));
  const labels = el('ul', 'lineage-list');
  for (const label of aggregates.evidence_labels || []) labels.append(el('li', null, label));
  if (!aggregates.evidence_labels || aggregates.evidence_labels.length === 0) labels.append(el('li', null, 'No evidence labels yet.'));
  evidence.append(labels);

  const usage = el('section', 'subpanel');
  usage.append(el('h3', null, 'Module usage'));
  const usageList = el('div', 'table-list');
  for (const [moduleId, count] of Object.entries(aggregates.module_usage || {})) usageList.append(row(['module', moduleId, count]));
  if (!aggregates.module_usage || Object.keys(aggregates.module_usage).length === 0) usageList.append(el('p', 'empty', 'No module usage yet.'));
  usage.append(usageList);

  const proposal = el('section', 'subpanel');
  proposal.append(el('h3', null, 'Last summary-derived proposal'));
  const last = trail.last_proposal;
  if (last) {
    proposal.append(row(['proposal', last.primitive || '—', last.rationale || '—', last.candidate_short_hash || '—', last.lifecycle || '—', last.promotable ? 'promotable' : 'pending']));
  } else {
    proposal.append(el('p', 'empty', 'No stored personalization proposal.'));
  }

  parent.append(counts, evidence, usage, proposal);
}

function renderMetrics(metrics) {
  const parent = body('metrics');
  const grid = el('div', 'metrics-grid');
  for (const key of ['runs_started', 'benchmarks_run', 'promotions', 'rollbacks', 'guardrail_breaches', 'auth_failures', 'permission_requests', 'prunes', 'restores']) {
    const card = el('article', 'metric-card');
    card.append(el('span', 'metric-value', metrics[key] ?? 0), el('span', 'metric-label', key.replaceAll('_', ' ')));
    grid.append(card);
  }
  parent.append(grid);
}

function row(values) {
  const item = el('article', 'data-row');
  for (const value of values) item.append(el('span', null, formatValue(value)));
  return item;
}

function formatValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

function shortHash(value) {
  return value ? String(value).slice(0, 12) : '—';
}

createShell();
refreshAll().catch((error) => {
  const app = document.getElementById('app');
  app.append(el('p', 'notice', String(error.message || error)));
});
window.setInterval(() => getJson('/api/metrics').then(renderMetrics).catch(() => {}), 5000);
