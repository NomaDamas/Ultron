// Ultron command surface: a command bar + a bounded generative-UI canvas.
// CSP-strict: DOM is built with createElement/textContent only (no unsafe HTML sinks).
// Modes: REPLACE (A) transforms the canvas each command; ACCUMULATE (B) keeps a
// capped workspace of validated cards. No raw prompt/image/key/payload is retained.

const MAX_CANVAS_ENVELOPES = 20;
const MAX_CANVAS_CARDS = 120;
const MAX_IMAGE_BYTES = 4 * 1024 * 1024; // mirror server cap (pre-decode)
const ALLOWED_IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp'];

const state = {
  csrfCookieName: 'ultron_csrf',
  lastRunId: null,
  activePointerVersion: null,
  mode: 'replace',
  pendingImage: null, // { dataUrl, name } cleared immediately after send
  envelopeCount: 0
};

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

  const shell = el('div', 'hud-shell');

  const header = el('header', 'topbar');
  const brand = el('div', 'brand');
  const orb = el('span', 'ultron-orb');
  orb.setAttribute('aria-hidden', 'true');
  const copy = el('div', 'brand-copy');
  copy.append(el('strong', null, 'Ultron'), el('span', null, 'JARVIS HUD · generative command surface'));
  brand.append(orb, copy);
  const presence = el('div', 'status-presence');
  presence.append(el('span', 'status-dot'), el('span', null, 'Core stable'));
  const nav = el('nav', 'nav-links');
  const dashboard = el('a', null, 'Settings');
  dashboard.href = '/dashboard';
  nav.append(dashboard);
  header.append(brand, presence, nav);

  const main = el('main', 'hud-main');

  // Canvas: the generative-UI workspace.
  const canvasWrap = el('section', 'canvas-wrap');
  canvasWrap.setAttribute('aria-live', 'polite');
  const canvas = el('div', 'canvas');
  canvas.id = 'canvas';
  const empty = el('div', 'canvas-empty');
  empty.id = 'canvas-empty';
  empty.append(el('p', null, 'Issue a command. Tools surface here as inline GenUI cards.'));
  canvas.append(empty);
  canvasWrap.append(canvas);

  // Status line (notices, errors) — not a transcript.
  const status = el('div', 'status-line');
  status.id = 'status-line';

  // Command bar.
  const bar = el('form', 'command-bar');
  const input = el('textarea', 'command-input');
  input.name = 'request_text';
  input.rows = 2;
  input.placeholder = 'Command Ultron — build or tune a harness tool…';
  input.id = 'command-input';

  const controls = el('div', 'command-controls');

  // Mode toggle (A replace / B accumulate).
  const modeToggle = el('button', 'mode-toggle', modeLabel());
  modeToggle.type = 'button';
  modeToggle.id = 'mode-toggle';
  modeToggle.setAttribute('aria-pressed', 'false');
  modeToggle.addEventListener('click', () => {
    state.mode = state.mode === 'replace' ? 'accumulate' : 'replace';
    modeToggle.textContent = modeLabel();
    modeToggle.setAttribute('aria-pressed', String(state.mode === 'accumulate'));
    setStatus(`Canvas mode: ${state.mode === 'replace' ? 'Replace (A)' : 'Accumulate (B)'}`);
  });

  // Image picker.
  const fileInput = el('input', 'image-input');
  fileInput.type = 'file';
  fileInput.id = 'image-input';
  fileInput.accept = ALLOWED_IMAGE_TYPES.join(',');
  fileInput.addEventListener('change', onImageSelected);
  const imageButton = el('button', 'image-button', 'Attach image');
  imageButton.type = 'button';
  imageButton.addEventListener('click', () => fileInput.click());
  const imageChip = el('span', 'image-chip');
  imageChip.id = 'image-chip';
  imageChip.hidden = true;

  const send = el('button', 'send-button', 'Run');
  send.type = 'submit';

  controls.append(modeToggle, imageButton, imageChip, fileInput, send);
  bar.append(input, controls);
  bar.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text && !state.pendingImage) return;
    input.value = '';
    submitRequest(text);
  });

  main.append(canvasWrap, status, bar);
  shell.append(header, main);
  app.append(shell);
}

function modeLabel() {
  return state.mode === 'replace' ? 'Mode: Replace (A)' : 'Mode: Accumulate (B)';
}

function onImageSelected(event) {
  const file = event.target.files && event.target.files[0];
  // Drop any prior attachment first: a rejected/failed new selection must never
  // leave a stale data URL resident that a later submit could send.
  clearPendingImage();
  if (!file) return;
  if (!ALLOWED_IMAGE_TYPES.includes(file.type)) {
    setStatus('Unsupported image type. Use PNG, JPEG, or WebP.');
    return;
  }
  if (file.size > MAX_IMAGE_BYTES) {
    setStatus('Image too large (max 4 MiB).');
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    state.pendingImage = { dataUrl: String(reader.result), name: file.name };
    showImageChip(file.name);
  };
  reader.onerror = () => { clearPendingImage(); setStatus('Could not read the selected image.'); };
  reader.readAsDataURL(file);
}

function showImageChip(name) {
  const chip = document.getElementById('image-chip');
  if (!chip) return;
  chip.textContent = '';
  chip.append(el('span', null, `Image attached`));
  const clear = el('button', 'chip-clear', '×');
  clear.type = 'button';
  clear.setAttribute('aria-label', 'Remove attached image');
  clear.addEventListener('click', clearPendingImage);
  chip.append(clear);
  chip.hidden = false;
}

function clearPendingImage() {
  state.pendingImage = null;
  const chip = document.getElementById('image-chip');
  if (chip) { chip.hidden = true; chip.textContent = ''; }
  const fileInput = document.getElementById('image-input');
  if (fileInput) fileInput.value = '';
}

async function submitRequest(requestText) {
  setStatus('Working on the run and shaping your harness…', true);
  const payload = { request_text: requestText };
  if (state.pendingImage) payload.image_base64 = state.pendingImage.dataUrl;
  // Drop the raw image reference immediately; it is not retained in client state.
  clearPendingImage();
  const data = await sendAction('SUBMIT_REQUEST', payload);
  if (!data || !data.ok) { setStatus(''); return; }
  setStatus('');
  reduceCanvas(data);
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
      setStatus(friendlyError(response.status, data));
      return data;
    }
    return data;
  } catch (error) {
    setStatus(`Network error: ${error.message || error}`);
    return null;
  }
}

// Bounded reducer: REPLACE clears prior cards; ACCUMULATE appends up to caps.
function reduceCanvas(data) {
  const canvas = document.getElementById('canvas');
  if (!canvas) return;
  state.lastRunId = data.envelope?.run_id || data.run_id || state.lastRunId;
  state.activePointerVersion = data.envelope?.provenance?.active_pointer_version ?? data.active_pointer_version ?? state.activePointerVersion;

  const empty = document.getElementById('canvas-empty');
  if (empty) empty.remove();

  const group = el('section', 'canvas-group');
  group.dataset.pinned = 'false';
  const head = el('div', 'group-head');
  const pin = el('button', 'pin-toggle', 'Pin');
  pin.type = 'button';
  pin.setAttribute('aria-pressed', 'false');
  pin.addEventListener('click', () => {
    const pinned = group.dataset.pinned === 'true';
    group.dataset.pinned = pinned ? 'false' : 'true';
    group.classList.toggle('pinned', !pinned);
    pin.setAttribute('aria-pressed', String(!pinned));
    pin.textContent = pinned ? 'Pin' : 'Pinned';
  });
  head.append(pin);
  const cards = el('div', 'cards');
  if (data.envelope) renderInlineEnvelope(cards, data.envelope, data);
  else cards.append(card('Action complete', { status: data.status || 'ok' }));
  group.append(head, cards);

  if (state.mode === 'replace') {
    canvas.textContent = '';
    canvas.append(group);
    state.envelopeCount = 1;
  } else {
    canvas.append(group);
    state.envelopeCount += 1;
    enforceCanvasCaps(canvas);
  }
  group.scrollIntoView({ block: 'nearest' });
}

function evictOldestGroup(canvas) {
  // Prefer the oldest non-pinned group; only evict a pinned group if every
  // remaining group is pinned.
  const groups = Array.from(canvas.querySelectorAll('.canvas-group'));
  const victim = groups.find((g) => g.dataset.pinned !== 'true') || groups[0];
  if (victim) victim.remove();
}

function enforceCanvasCaps(canvas) {
  // Drop oldest non-pinned groups beyond the envelope cap.
  while (canvas.querySelectorAll('.canvas-group').length > MAX_CANVAS_ENVELOPES) {
    evictOldestGroup(canvas);
  }
  // Drop oldest non-pinned groups until total card count is within the card cap.
  while (canvas.querySelectorAll('.card').length > MAX_CANVAS_CARDS && canvas.querySelectorAll('.canvas-group').length > 1) {
    evictOldestGroup(canvas);
  }
  state.envelopeCount = canvas.querySelectorAll('.canvas-group').length;
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
    if (data?.ok) setStatus(`${humanize(type)} accepted.`);
  });
  return button;
}

function applyAnimation(node, animation) {
  if (REDUCED_MOTION) return;
  const kind = typeof animation?.kind === 'string' ? animation.kind : 'none';
  const className = ANIMATION_CLASS[kind] || '';
  if (className) node.classList.add(className);
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

function setStatus(message, busy) {
  const status = document.getElementById('status-line');
  if (!status) return;
  status.textContent = message || '';
  status.classList.toggle('busy', Boolean(busy));
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

createShell();
