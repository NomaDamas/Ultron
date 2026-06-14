const state = { csrfCookieName: 'ultron_csrf', lastRunId: null, activePointerVersion: null };
const ANIMATION_CLASS = {
  none: '',
  fade_in: 'anim-fade-in',
  slide_up: 'anim-slide-up',
  pulse_glow: 'anim-pulse-glow',
  reticle_scan: 'anim-reticle-scan',
  expand: 'anim-expand'
};
const REDUCED_MOTION = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function cookieValue(name) {
  const prefix = `${name}=`;
  const pair = document.cookie.split('; ').find((part) => part.startsWith(prefix));
  return pair ? decodeURIComponent(pair.slice(prefix.length)) : '';
}

function createShell() {
  const app = document.getElementById('app');
  state.csrfCookieName = app?.dataset?.csrfCookie || 'ultron_csrf';
  app.textContent = '';

  const shell = el('div', 'chat-shell');
  const header = el('header', 'topbar');
  const brand = el('div', 'brand');
  const orb = el('span', 'ultron-orb');
  orb.setAttribute('aria-hidden', 'true');
  const copy = el('div', 'brand-copy');
  copy.append(el('strong', null, 'Ultron'), el('span', null, 'JARVIS HUD online · chat control surface'));
  brand.append(orb, copy);
  const presence = el('div', 'status-presence');
  presence.append(el('span', 'status-dot'), el('span', null, 'Core stable'));
  const nav = el('nav', 'nav-links');
  const dashboard = el('a', null, 'Settings');
  dashboard.href = '/dashboard';
  nav.append(dashboard);
  header.append(brand, presence, nav);

  const main = el('main', 'chat-main');
  const thread = el('section', 'thread');
  thread.id = 'thread';
  thread.setAttribute('aria-live', 'polite');
  const intro = el('article', 'turn agent-turn');
  intro.append(el('p', 'bubble agent-bubble', 'Mission control online. Tell me the workflow harness behavior you want; inline GenUI cards will render in this thread.'));
  thread.append(intro);

  const composer = el('form', 'composer');
  const input = el('textarea', 'composer-input');
  input.name = 'request_text';
  input.rows = 3;
  input.placeholder = 'Build or tune a tool for my workflow…';
  const send = el('button', 'send-button', 'Send');
  send.type = 'submit';
  composer.append(input, send);
  composer.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    appendUserTurn(text);
    submitRequest(text);
  });
  main.append(thread, composer);

  shell.append(header, main);
  app.append(shell);
}

function appendUserTurn(text) {
  const turn = el('article', 'turn user-turn');
  turn.append(el('p', 'bubble user-bubble', text));
  appendTurn(turn);
}

function appendTurn(turn) {
  const thread = document.getElementById('thread');
  thread.append(turn);
  thread.scrollTop = thread.scrollHeight;
}

async function submitRequest(requestText) {
  const pending = el('article', 'turn agent-turn');
  pending.append(el('p', 'bubble agent-bubble', 'Working locally on the run and shaping your harness…'));
  appendTurn(pending);
  const data = await sendAction('SUBMIT_REQUEST', { request_text: requestText });
  pending.remove();
  if (!data || !data.ok) return;
  appendAgentTurn(data);
}

async function sendAction(type, payload) {
  const csrf = cookieValue(state.csrfCookieName);
  const command = { type, payload: payload || {}, csrf_token: csrf, active_pointer_version: payload?.active_pointer_version };
  try {
    const response = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify(command)
    });
    const data = await safeJson(response);
    if (!response.ok) {
      appendNotice(friendlyError(response.status, data));
      return data;
    }
    return data;
  } catch (error) {
    appendNotice(`Network error: ${error.message || error}`);
    return null;
  }
}

function appendAgentTurn(data) {
  const turn = el('article', 'turn agent-turn');
  state.lastRunId = data.envelope?.run_id || data.run_id || state.lastRunId;
  state.activePointerVersion = data.envelope?.provenance?.active_pointer_version ?? data.active_pointer_version ?? state.activePointerVersion;
  turn.append(el('p', 'bubble agent-bubble', 'I ran the request and generated an inline control surface for the resulting plan, risk, tests, and evidence.'));
  const cards = el('div', 'cards');
  if (data.envelope) renderInlineEnvelope(cards, data.envelope, data);
  else cards.append(card('Action complete', { status: data.status || 'ok' }));
  turn.append(cards);
  appendTurn(turn);
}

function renderRunOutput(parent, output) {
  for (const key of ['plan', 'risk', 'tests']) {
    const value = output?.[key];
    if (value !== undefined) parent.append(card(humanize(key), value));
  }
}

function renderUiSpec(parent, spec) {
  const components = Array.isArray(spec?.components) ? spec.components : [];
  for (const component of components) parent.append(renderComponent(component, {}));
}

function renderInlineEnvelope(parent, envelope, data) {
  const context = { envelope, data };
  const components = Array.isArray(envelope?.components) ? envelope.components : [];
  for (const component of components) parent.append(renderComponent(component, context));
}

function renderComponent(component, context) {
  const type = String(component?.type || 'UNKNOWN_COMPONENT');
  const props = component && typeof component.props === 'object' && component.props !== null ? component.props : {};
  const renderer = COMPONENT_RENDERERS[type] || renderUnknownComponent;
  const node = renderer(props, component || {}, context || {});
  applyAnimation(node, component?.animation);
  return node;
}

const COMPONENT_RENDERERS = {
  RUN_SUMMARY_CARD: renderRunSummaryCard,
  TOOL_RESULT_CARD: renderToolResultCard,
  HARNESS_EVOLUTION_CARD: renderHarnessEvolutionCard,
  EVIDENCE_STATUS_CARD: renderEvidenceStatusCard,
  PERSONALIZATION_SIGNAL_CARD: renderPersonalizationSignalCard,
  SAFETY_STATUS_CARD: renderSafetyStatusCard,
  ORB_STATUS: renderOrbStatus,
  TIMELINE_STEP: renderTimelineStep
};

function renderRunSummaryCard(props) {
  const section = card('Run summary', { run_id: props.run_id, workflow: props.workflow, manifest_hash: props.manifest_hash, trajectory_id: props.trajectory_id, status: props.status });
  appendList(section, props.summary_lines);
  return section;
}

function renderToolResultCard(props) {
  const section = card(`Tool: ${props.tool || 'result'}`, { status: props.status, output_redacted: props.output_redacted, secrets_redacted: props.secrets_redacted });
  appendList(section, props.output_summary);
  return section;
}

function renderHarnessEvolutionCard(props, _component, context) {
  const section = card('Harness evolution', props);
  const controls = el('div', 'feedback-controls');
  const up = actionButton('Keep shaping this way', 'GIVE_FEEDBACK', { run_id: context.envelope?.run_id || state.lastRunId || 'run', rating: 1, comment: 'preserve this harness direction' });
  const down = actionButton('Less like this', 'GIVE_FEEDBACK', { run_id: context.envelope?.run_id || state.lastRunId || 'run', rating: -1, comment: 'avoid this harness direction' });
  controls.append(up, down);
  section.append(controls);
  return section;
}

function renderEvidenceStatusCard(props, _component, context) {
  const section = card('Evidence status', props);
  const controls = el('div', 'feedback-controls');
  controls.append(actionButton('Run benchmark', 'RUN_BENCHMARK', withActivePointerVersion({ candidate_hash: context.envelope?.candidate_hash, canary_id: context.envelope?.canary_id })));
  section.append(controls);
  return section;
}

function renderPersonalizationSignalCard(props) {
  return card('Personalization signal', props);
}

function renderSafetyStatusCard(props, _component, context) {
  const section = card('Safety status', props);
  const controls = el('div', 'feedback-controls');
  const actions = Array.isArray(props.gated_actions) ? props.gated_actions : [];
  for (const type of actions) {
    if (type === 'APPROVE_PROMOTION') controls.append(actionButton('Approve promotion', type, withActivePointerVersion({ candidate_hash: context.envelope?.candidate_hash })));
    if (type === 'ROLLBACK_CANARY') controls.append(actionButton('Rollback canary', type, withActivePointerVersion({ canary_id: context.envelope?.canary_id })));
  }
  if (controls.childNodes.length) section.append(controls);
  return section;
}

function renderOrbStatus(props) {
  return card('Orb status', { state: props.state, status_text: props.status_text });
}

function renderTimelineStep(props) {
  return card('Timeline step', props);
}

function renderUnknownComponent(props, component) {
  return card(String(component?.type || 'Unknown component').replaceAll('_', ' '), props);
}

function appendList(parent, lines) {
  if (!Array.isArray(lines) || !lines.length) return;
  const list = el('ul', 'summary-list');
  for (const line of lines) list.append(el('li', null, line));
  parent.append(list);
}

function withActivePointerVersion(payload) {
  return { ...(payload || {}), active_pointer_version: state.activePointerVersion };
}

function actionButton(label, type, payload) {
  const button = el('button', 'feedback-button', label);
  button.type = 'button';
  button.addEventListener('click', async () => {
    const data = await sendAction(type, payload || {});
    if (data?.ok) appendNotice(`${humanize(type)} accepted.`);
  });
  return button;
}

function applyAnimation(node, animation) {
  if (REDUCED_MOTION) return;
  const kind = typeof animation?.kind === 'string' ? animation.kind : 'none';
  const className = ANIMATION_CLASS[kind] || '';
  if (className) node.classList.add(className);
}

function renderHarnessShaping(data) {
  const box = el('section', 'shaping-card anim-slide-up');
  box.append(el('h3', null, 'Built/tuned a tool for this workflow'));
  const candidate = data.candidate || {};
  const summary = el('dl', 'kv');
  addKv(summary, 'Module', candidate.name || candidate.module_id || 'Candidate harness module');
  addKv(summary, 'Candidate', shortHash(candidate.content_hash));
  addKv(summary, 'Canary', data.canary_id || '—');
  addKv(summary, 'Mutation', data.result?.run_result?.plan || 'Prompt/tooling tuned from this request.');
  box.append(summary);
  const controls = el('div', 'feedback-controls');
  const up = el('button', 'feedback-button', 'Keep shaping this way');
  const down = el('button', 'feedback-button', 'Less like this');
  up.type = 'button';
  down.type = 'button';
  up.addEventListener('click', () => sendFeedback(1, 'preserve this harness direction'));
  down.addEventListener('click', () => sendFeedback(-1, 'avoid this harness direction'));
  controls.append(up, down);
  box.append(controls);
  return box;
}

async function sendFeedback(rating, comment) {
  const data = await sendAction('GIVE_FEEDBACK', { run_id: state.lastRunId || 'run', rating, comment });
  if (data?.ok) appendNotice('Preference signal recorded. Your harness will be whittled with that feedback.');
}

function card(title, value) {
  const section = el('section', 'card anim-fade-in');
  section.append(el('h3', null, title));
  if (value && typeof value === 'object') {
    const list = el('dl', 'kv');
    for (const [key, item] of Object.entries(value)) addKv(list, humanize(key), formatValue(item));
    section.append(list);
  } else {
    section.append(el('p', null, formatValue(value)));
  }
  return section;
}

function addKv(parent, key, value) {
  const row = el('div', 'kv-row');
  row.append(el('dt', null, key), el('dd', null, formatValue(value)));
  parent.append(row);
}

function appendNotice(message) {
  const turn = el('article', 'turn notice-turn');
  turn.append(el('p', 'notice', message));
  appendTurn(turn);
}

function friendlyError(status, data) {
  const detail = typeof data?.detail === 'string' ? data.detail : '';
  const map = {
    401: 'Session expired. Reloading the page restores the local session.',
    403: 'That mutation is gated by scope, CSRF, pointer, or evidence policy.',
    422: 'The request shape was rejected before mutation.',
    503: 'Live model or Hermes is not configured; Ultron is calmly running in local/demo mode.'
  };
  return detail ? `${map[status] || 'Request failed.'} ${detail}` : (map[status] || `Request failed with HTTP ${status}.`);
}

async function safeJson(response) {
  try { return await response.json(); } catch (_error) { return {}; }
}

function humanize(key) {
  return String(key).replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.map(formatValue).join(', ');
  if (typeof value === 'object') return JSON.stringify(value, null, 2);
  return String(value);
}

function shortHash(value) {
  return value ? String(value).slice(0, 12) : '—';
}

createShell();
