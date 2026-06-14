const state = { csrfCookieName: 'ultron_csrf', lastRunId: null };
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
  const command = { type, payload: payload || {}, csrf_token: csrf };
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
  const result = data.result || {};
  const turn = el('article', 'turn agent-turn');
  const manifest = result.run_manifest || {};
  state.lastRunId = manifest.run_id || state.lastRunId;
  turn.append(el('p', 'bubble agent-bubble', 'I ran the request and generated an inline control surface for the resulting plan, risk, tests, and evidence.'));
  const cards = el('div', 'cards');
  renderRunOutput(cards, result.run_result || result.adapter_result?.output || {});
  renderUiSpec(cards, result.ui_spec || data.ui_spec);
  if (data.candidate || data.canary_id) cards.append(renderHarnessShaping(data));
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
  for (const component of components) {
    const props = component && typeof component.props === 'object' && component.props !== null ? component.props : {};
    const built = card(String(component.type || 'PANEL').replaceAll('_', ' '), props.summary || props.title || props);
    built.classList.add('uispec-card');
    applyAnimation(built, component.animation);
    parent.append(built);
  }
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
