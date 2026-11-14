// Task Backend tab: radio-group selector; each choice is POSTed and the stage mapping re-rendered from the reply.
import { api } from './api.js';
import { badge, clear, el, failure, facts, section, shortText, sourceNotice, table, text, utcTime } from './dom.js';

function mappingView(view, echo) {
  const backend = view.backend;
  const rows = view.mapping.map((item) => ({
    attrs: { 'data-stage': item.stage },
    cells: {
      stage: el('code', { text: item.stage }),
      key: el('code', { text: item.task_key }),
      status: badge(item.status, 'info'),
      label: text(item.label),
    },
  }));
  const columns = [
    { key: 'stage', label: 'Workflow stage' }, { key: 'key', label: 'Task key' },
    { key: 'status', label: 'Native status' }, { key: 'label', label: 'Label' },
  ];
  const meta = [
    ['Selected (server)', el('b', { id: 'backend-selected', text: view.selected })],
    ['Scope', text(view.scope)],
    ['Issue key format', el('code', { text: backend.issue_key_format })],
    ['Project key', el('code', { text: backend.project_key })],
    ['Capabilities', el('ul', { class: 'chip-list' }, backend.capabilities.map((cap) => el('li', {}, [badge(cap, 'neutral')])))],
  ];
  if (echo) {
    meta.push(['Echo', el('output', { id: 'backend-echo', text:
      `POST accepted at ${utcTime(echo.selected_at)}; round trip ${echo.clientMs} ms; trace ${shortText(echo.traceId, 'short_trace_chars')}` })]);
  }
  return section(`${backend.display_name}: how S1 to S6 would land`, 'stack', [
    sourceNotice(view.source_label, 'task-backend'),
    el('p', { class: 'muted', text: backend.description }),
    facts(meta),
    table(columns, rows),
  ]);
}

function optionButton(backend, selected, onPick) {
  const button = el('button', {
    class: 'backend-option', type: 'button', role: 'radio', 'data-backend': backend.id,
    'aria-checked': String(backend.id === selected), tabindex: backend.id === selected ? '0' : '-1',
  }, [el('b', { class: 'backend-name', text: backend.display_name }), el('code', { class: 'small muted', text: backend.issue_key_format })]);
  button.addEventListener('click', () => onPick(backend.id, button));
  return button;
}

export async function loadBackends(container) {
  const [{ data: list }, { data: current }] = await Promise.all([api('/api/task-backends'), api('/api/task-backend')]);
  const mappingBox = el('div', { id: 'backend-mapping' }, [mappingView(current, null)]);
  const group = el('div', { class: 'backend-grid', role: 'radiogroup', 'aria-label': 'Task backend' });
  const buttons = [];

  async function pick(backendId, button) {
    if (button.getAttribute('aria-busy') === 'true') return;
    button.setAttribute('aria-busy', 'true');
    try {
      const { data, clientMs, traceId } = await api('/api/task-backend', { method: 'POST', body: { backend: backendId } });
      for (const other of buttons) {
        const chosen = other.dataset.backend === data.selected;
        other.setAttribute('aria-checked', String(chosen));
        other.tabIndex = chosen ? 0 : -1;
      }
      clear(mappingBox).append(mappingView(data, { selected_at: data.selected_at, clientMs, traceId }));
    } catch (err) {
      failure(mappingBox, err);
    } finally {
      button.removeAttribute('aria-busy');
    }
  }

  for (const backend of list.backends) {
    const button = optionButton(backend, current.selected, pick);
    buttons.push(button);
    group.append(button);
  }
  group.addEventListener('keydown', (event) => {
    const index = buttons.indexOf(document.activeElement);
    if (index < 0) return;
    const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1
      : event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = buttons[(index + step + buttons.length) % buttons.length];
    next.focus();
    next.click();
  });
  clear(container).append(
    el('p', { class: 'small muted', text: `${list.backends.length} registered backends; default ${list.default_backend}.` }),
    group,
    mappingBox,
  );
}
