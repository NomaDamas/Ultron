const state = {
  spec: null,
  metrics: null,
  activePointerVersion: null,
  csrfCookieName: 'ultron_csrf'
};

const componentRenderers = {
  PLAN_PANEL: renderReadablePanel,
  RISK_PANEL: renderReadablePanel,
  TEST_PANEL: renderReadablePanel,
  FEEDBACK_PANEL: renderFeedbackPanel,
  TRACE_PANEL: renderKeyValuePanel,
  MUTATION_DIFF_PANEL: renderKeyValuePanel,
  APPROVAL_PANEL: renderApprovalPanel,
  ROLLBACK_PANEL: renderRollbackPanel,
  INTAKE_PANEL: renderIntakePanel,
  CONTEXT_PANEL: renderKeyValuePanel
};

const regionLabels = {
  sidebar: 'Session',
  main: 'Triage workspace',
  details: 'Evidence',
  actions: 'Actions'
};

const allowedRegions = new Set(Object.keys(regionLabels));

function safeRegion(region) {
  return allowedRegions.has(region) ? region : 'main';
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function friendlyTitle(type) {
  return String(type || 'PANEL').replace(/_PANEL$/, '').replaceAll('_', ' ').toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function normalizeType(type) {
  return String(type || '').toLowerCase().replaceAll('_panel', '').replaceAll('_', '-');
}

function componentProps(component) {
  return component && typeof component.props === 'object' && component.props !== null ? component.props : {};
}

function createShell() {
  const app = document.getElementById('app');
  state.csrfCookieName = app?.dataset?.csrfCookie || 'ultron_csrf';
  app.textContent = '';

  const header = el('header', 'app-topbar');
  const brand = el('div', 'brand');
  brand.append(el('span', 'brand-mark', 'U'), el('div', 'brand-copy'));
  brand.querySelector('.brand-copy').append(el('strong', null, 'Ultron'), el('span', null, 'Generative triage console'));

  const status = el('div', 'session-strip');
  status.append(el('span', 'scope-badge', 'scope: local'), el('span', 'status-pill'));
  status.querySelector('.status-pill').append(el('span', 'status-dot'), document.createTextNode(' Live'));
  header.append(brand, status);

  const metrics = el('section', 'metrics-strip');
  metrics.id = 'metrics';
  metrics.setAttribute('aria-label', 'Runtime metrics');

  const layout = el('div', 'app-grid');
  layout.id = 'layout';
  for (const region of allowedRegions) {
    const column = el('section', `region region-${region}`);
    column.dataset.region = region;
    column.setAttribute('aria-label', regionLabels[region]);
    column.append(el('h2', 'region-title', regionLabels[region]));
    layout.append(column);
  }

  const toasts = el('div', 'toast-stack');
  toasts.id = 'toasts';
  toasts.setAttribute('aria-live', 'polite');
  toasts.setAttribute('aria-relevant', 'additions');

  app.append(header, metrics, layout, toasts);
}

function panel(component, bodyClass) {
  const section = el('article', `panel panel-${normalizeType(component.type)}`);
  section.dataset.region = component.region || 'main';
  const header = el('div', 'panel-header');
  header.append(el('h3', null, friendlyTitle(component.type)));
  if (Number.isFinite(component.priority)) header.append(el('span', 'panel-priority', `P${component.priority}`));
  const body = el('div', bodyClass || 'panel-body');
  section.append(header, body);
  return { section, body };
}

function addValue(parent, label, value) {
  const row = el('div', 'kv-row');
  row.append(el('dt', null, label), el('dd', null, formatValue(value)));
  parent.append(row);
}

function formatValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.map(formatValue).join(', ');
  if (typeof value === 'object') return JSON.stringify(value, null, 2);
  return String(value);
}

function renderReadablePanel(component) {
  const props = componentProps(component);
  const built = panel(component);
  const summary = props.summary || props.title || props.panel || props.manifest_hash || 'Ready for the next triage run.';
  built.body.append(el('p', 'panel-summary', summary));
  const list = el('dl', 'kv-list');
  for (const [key, value] of Object.entries(props)) addValue(list, humanizeKey(key), value);
  if (Object.keys(props).length === 0) addValue(list, 'Status', 'Waiting for live data');
  built.body.append(list);
  return built.section;
}

function renderKeyValuePanel(component) {
  const props = componentProps(component);
  const built = panel(component, 'panel-body mono-panel');
  const list = el('dl', 'kv-list');
  for (const [key, value] of Object.entries(props)) addValue(list, humanizeKey(key), value);
  if (Object.keys(props).length === 0) addValue(list, 'Status', 'No evidence captured yet');
  built.body.append(list);
  return built.section;
}

function renderIntakePanel(component) {
  const built = panel(component);
  const form = el('form', 'intake-form');
  const label = el('label', null, 'Request');
  label.setAttribute('for', 'request-text');
  const input = el('textarea', 'intake-input');
  input.id = 'request-text';
  input.name = 'request_text';
  input.rows = 7;
  input.placeholder = 'Describe the product, model, or safety issue to triage…';
  input.value = componentProps(component).request_text || '';
  const button = el('button', 'btn btn-primary', 'Submit request');
  button.type = 'submit';
  form.append(label, input, button);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    sendAction('SUBMIT_REQUEST', { request_text: input.value.trim() });
  });
  built.body.append(form);
  return built.section;
}

function renderFeedbackPanel(component) {
  const built = panel(component);
  const form = el('form', 'feedback-form');
  const fieldset = el('fieldset', 'rating-group');
  fieldset.append(el('legend', null, 'Rate this run'));
  for (let rating = 1; rating <= 5; rating += 1) {
    const label = el('label', 'rating-choice');
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'rating';
    input.value = String(rating);
    if (rating === 4) input.checked = true;
    label.append(input, el('span', null, '★'.repeat(rating)));
    fieldset.append(label);
  }
  const comment = el('textarea', 'feedback-comment');
  comment.name = 'comment';
  comment.rows = 3;
  comment.placeholder = 'What should Ultron improve or preserve?';
  const button = el('button', 'btn btn-secondary', 'Send feedback');
  button.type = 'submit';
  form.append(fieldset, comment, button);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const data = new FormData(form);
    sendAction('GIVE_FEEDBACK', { rating: Number(data.get('rating') || 1), comment: String(data.get('comment') || '') });
  });
  built.body.append(form);
  return built.section;
}

function renderApprovalPanel(component) {
  const props = componentProps(component);
  const built = panel(component);
  built.body.append(el('p', 'panel-summary', 'Promote a benchmarked candidate when policy evidence is sufficient.'));
  const button = el('button', 'btn btn-primary', 'Approve promotion');
  button.type = 'button';
  button.addEventListener('click', () => sendAction('APPROVE_PROMOTION', { candidate_hash: props.candidate_hash || props.manifest_hash || '' }));
  built.body.append(button);
  return built.section;
}

function renderRollbackPanel(component) {
  const props = componentProps(component);
  const built = panel(component);
  built.body.append(el('p', 'panel-summary', 'Rollback the active canary if guardrails or quality checks regress.'));
  const button = el('button', 'btn btn-danger', 'Rollback canary');
  button.type = 'button';
  button.addEventListener('click', () => sendAction('ROLLBACK_CANARY', { canary_id: props.canary_id || '' }));
  built.body.append(button);
  return built.section;
}

function humanizeKey(key) {
  return String(key).replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function cookieValue(name) {
  const prefix = `${name}=`;
  const pair = document.cookie.split('; ').find((part) => part.startsWith(prefix));
  return pair ? decodeURIComponent(pair.slice(prefix.length)) : '';
}

function derivePointerVersion(data) {
  const candidates = [
    data?.active_pointer_version,
    data?.current_pointer_version,
    data?.pointer_version,
    data?.result?.active_pointer_version,
    data?.result?.run_manifest?.pointer_version,
    data?.decision?.report?.active_pointer_version
  ];
  const found = candidates.find((value) => Number.isInteger(value));
  if (found !== undefined) state.activePointerVersion = found;
}

async function sendAction(type, payload) {
  const csrf = cookieValue(state.csrfCookieName);
  const command = { type, payload: payload || {}, csrf_token: csrf, active_pointer_version: state.activePointerVersion };
  try {
    const response = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify(command)
    });
    const data = await safeJson(response);
    derivePointerVersion(data);
    if (!response.ok) {
      showToast(friendlyError(response.status, data), 'error');
      return data;
    }
    showToast(successMessage(type), 'success');
    await refreshAll();
    return data;
  } catch (error) {
    showToast(`Network error: ${error.message || error}`, 'error');
    return null;
  }
}

function friendlyError(status, data) {
  const detail = typeof data?.detail === 'string' ? data.detail : '';
  const map = {
    401: 'Session expired. Reload and try again.',
    403: 'Action blocked by scope, CSRF, pointer, or policy gates.',
    422: 'The server rejected malformed action data.',
    503: 'A live dependency is unavailable. Try again after it recovers.'
  };
  return detail ? `${map[status] || 'Action failed'} ${detail}` : (map[status] || `Action failed with HTTP ${status}.`);
}

function successMessage(type) {
  const messages = {
    SUBMIT_REQUEST: 'Request submitted.',
    GIVE_FEEDBACK: 'Feedback recorded.',
    APPROVE_PROMOTION: 'Promotion request accepted.',
    ROLLBACK_CANARY: 'Rollback request accepted.'
  };
  return messages[type] || 'Action completed.';
}

async function safeJson(response) {
  try {
    return await response.json();
  } catch (_error) {
    return {};
  }
}

function showToast(message, kind) {
  const stack = document.getElementById('toasts');
  if (!stack) return;
  const toast = el('div', `toast toast-${kind || 'info'}`, message);
  stack.append(toast);
  window.setTimeout(() => toast.remove(), 7000);
}

async function fetchUiSpec() {
  const response = await fetch('/api/uispec');
  const spec = await response.json();
  if (!response.ok) throw new Error('Unable to load UiSpec');
  state.spec = spec;
  derivePointerVersion(spec);
  return spec;
}

async function fetchMetrics() {
  const response = await fetch('/api/metrics');
  const metrics = await response.json();
  if (!response.ok) throw new Error('Unable to load metrics');
  state.metrics = metrics;
  derivePointerVersion(metrics);
  renderMetrics(metrics);
  return metrics;
}

function renderMetrics(metrics) {
  const container = document.getElementById('metrics');
  if (!container) return;
  container.textContent = '';
  const items = [
    ['Runs', metrics?.runs_started],
    ['Benchmarks', metrics?.benchmarks_run],
    ['Promotions', metrics?.promotions],
    ['Rollbacks', metrics?.rollbacks],
    ['Guardrails', metrics?.guardrail_breaches],
    ['Auth fails', metrics?.auth_failures]
  ];
  for (const [label, value] of items) {
    const card = el('div', 'metric-card');
    card.append(el('span', 'metric-value', value ?? 0), el('span', 'metric-label', label));
    container.append(card);
  }
}

function renderUiSpec(spec) {
  const layout = document.getElementById('layout');
  for (const region of layout.querySelectorAll('.region')) {
    const title = region.querySelector('.region-title');
    region.textContent = '';
    region.append(title);
  }
  const components = Array.isArray(spec?.components) ? [...spec.components] : [];
  components.sort((left, right) => (left.priority ?? 0) - (right.priority ?? 0));
  for (const component of components) {
    const renderer = componentRenderers[component.type];
    const targetRegion = safeRegion(component.region);
    const target = layout.querySelector(`[data-region="${targetRegion}"]`) || layout.querySelector('[data-region="main"]');
    target.append(renderer ? renderer(component) : renderReadablePanel(component));
  }
}

async function refreshAll() {
  const spec = await fetchUiSpec();
  renderUiSpec(spec);
  await fetchMetrics();
}

createShell();
refreshAll().catch((error) => {
  showToast(String(error.message || error), 'error');
});
window.setInterval(() => fetchMetrics().catch(() => showToast('Metrics refresh failed.', 'error')), 5000);
