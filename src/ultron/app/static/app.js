const componentRenderers = {
  PLAN_PANEL: renderListPanel,
  RISK_PANEL: renderListPanel,
  TEST_PANEL: renderListPanel,
  FEEDBACK_PANEL: renderFeedbackPanel,
  TRACE_PANEL: renderKeyValuePanel,
  MUTATION_DIFF_PANEL: renderKeyValuePanel,
  APPROVAL_PANEL: renderActionPanel,
  ROLLBACK_PANEL: renderActionPanel,
  INTAKE_PANEL: renderIntakePanel,
  CONTEXT_PANEL: renderKeyValuePanel
};

function panelTitle(type) {
  return type.toLowerCase().replaceAll('_', ' ');
}

function renderListPanel(component) {
  const section = document.createElement('section');
  section.className = 'panel';
  const title = document.createElement('h2');
  title.textContent = panelTitle(component.type);
  const body = document.createElement('pre');
  body.textContent = JSON.stringify(component.props, null, 2);
  section.append(title, body);
  return section;
}

function renderKeyValuePanel(component) {
  return renderListPanel(component);
}

function renderFeedbackPanel(component) {
  const section = renderListPanel(component);
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = 'Send feedback';
  button.addEventListener('click', () => sendAction('GIVE_FEEDBACK', { rating: 1, comment: 'useful' }));
  section.append(button);
  return section;
}

function renderActionPanel(component) {
  return renderListPanel(component);
}

function renderIntakePanel(component) {
  const section = renderListPanel(component);
  const form = document.createElement('form');
  const input = document.createElement('textarea');
  input.name = 'request_text';
  input.value = 'Triage this request end-to-end';
  const button = document.createElement('button');
  button.type = 'submit';
  button.textContent = 'Submit request';
  form.append(input, button);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    sendAction('SUBMIT_REQUEST', { request_text: input.value });
  });
  section.append(form);
  return section;
}

function cookieValue(name) {
  return document.cookie.split('; ').find((part) => part.startsWith(name + '='))?.split('=')[1] || '';
}

async function sendAction(type, payload) {
  const csrf = cookieValue('ultron_csrf');
  const response = await fetch('/api/action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    body: JSON.stringify({ type, payload, csrf_token: csrf })
  });
  const data = await response.json();
  const output = document.getElementById('action-output') || document.createElement('pre');
  output.id = 'action-output';
  output.textContent = JSON.stringify(data, null, 2);
  document.getElementById('app').append(output);
}

async function renderUiSpec() {
  const response = await fetch('/api/uispec');
  const spec = await response.json();
  const app = document.getElementById('app');
  app.textContent = '';
  for (const component of spec.components) {
    const renderer = componentRenderers[component.type];
    if (!renderer) {
      throw new Error('Unknown server component: ' + component.type);
    }
    app.append(renderer(component));
  }
}

renderUiSpec().catch((error) => {
  document.getElementById('app').textContent = String(error);
});
