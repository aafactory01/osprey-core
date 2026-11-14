// Live CI/CD panels for the deterministic stages, fed only by GET /api/pipeline. Stage ids come from the
// API (`stage_ids`), never from this file; tones and labels come from config via the API.
import { api } from './api.js';
import { clear, el, facts, icon, shortText, sourceNotice, text, uiSetting, utcTime } from './dom.js';

const TONE_ICONS = { ok: 'check', warn: 'refresh', crit: 'cross', neutral: 'info' };
const SOURCE_ICONS = { live: 'check', unavailable: 'alert', not_configured: 'info' };
const PANEL_ICONS = { ci: 'build', cd: 'deploy' };
const state = { result: null, error: null, failedAt: null, pending: null };
const stages = new Map();
const controls = { button: null, output: null };
const clock = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC', hourCycle: 'h23', hour: '2-digit', minute: '2-digit', second: '2-digit',
});

const current = () => (state.result ? state.result.data : null);
const plain = (value) => (typeof value === 'string' || typeof value === 'number' ? text(value) : 'n/a');
const code = (value) => ((typeof value === 'string' && value) || typeof value === 'number' ? el('code', { text: text(value) }) : 'n/a');
const toneOf = (value) => (Object.prototype.hasOwnProperty.call(TONE_ICONS, value) ? value : 'neutral');
const duration = (ms) => (Number.isFinite(ms) && ms >= 0 ? clock.format(new Date(ms)) : 'n/a');
const latency = (ms) => (Number.isFinite(ms) ? `${ms} ms` : 'n/a');
const compact = (items) => items.filter((item) => item !== null && item !== undefined && item !== false);

export function liveRole(stageId) {
  const data = current();
  if (!data || !data.stage_ids) return null;
  if (stageId === data.stage_ids.release_gate) return 'ci';
  if (stageId === data.stage_ids.deploy_close) return 'cd';
  return null;
}

function chip(status, tone) {
  const name = toneOf(tone);
  return el('b', { class: 'badge chip', 'data-tone': name, 'data-status': plain(status) },
    [icon(TONE_ICONS[name]), plain(status).replaceAll('_', ' ')]);
}

function shortDigest(value) {
  if (typeof value !== 'string' || !value) return 'n/a';
  const cut = value.indexOf(':');
  const hex = value.slice(cut + 1, cut + 1 + uiSetting('short_signature_chars'));
  return cut < 0 ? hex : `${value.slice(0, cut)}:${hex}`;
}

// Live value when present, else the serving revision's provenance value, marked as such; else n/a.
function origin(liveValue, revisionValue, render) {
  if (typeof liveValue === 'string' && liveValue) return render(liveValue);
  if (typeof revisionValue !== 'string' || !revisionValue) return 'n/a';
  return el('div', { class: 'origin' }, [render(revisionValue), el('small', { class: 'muted', text: 'from revision env' })]);
}

const tagCode = (value) => code(`:${value}`);
const shortBuild = (value) => code(shortText(value, 'short_trace_chars'));
const shortDigestCode = (value) => code(shortDigest(value));

function statusContent(block, role) {
  const value = role === 'ci' ? block.status : block.rollout_state;
  const note = block.error ? el('code', { class: 'small', text: plain(block.error.taxonomy) })
    : block.source === 'not_configured' ? el('b', { class: 'small muted', text: 'not configured' }) : null;
  return compact([chip(value, block.tone), note]);
}

function sourceLine(block, role) {
  return el('p', { class: 'pipeline-source', 'data-pipeline-source': role, 'data-source': plain(block.source) },
    [icon(SOURCE_ICONS[block.source] || 'info'), el('b', { text: plain(block.source_label) })]);
}

function errorLine(error) {
  if (!error) return null;
  const status = Number.isFinite(error.http_status) ? ` (HTTP ${error.http_status})` : '';
  return el('p', { class: 'small muted pipeline-error' }, [el('code', { text: plain(error.taxonomy) }), `${status}: ${plain(error.detail)}`]);
}

function consoleLink(url, label) {
  if (typeof url !== 'string' || !url.startsWith('https:')) return null;
  return el('p', { class: 'small' }, [el('a', { href: url, rel: 'noopener noreferrer' }, [label])]);
}

function routeLine(label, ids, joiner) {
  const list = Array.isArray(ids) ? ids.filter((id) => typeof id === 'string') : [];
  return el('p', { class: 'small pipeline-route' }, [el('b', { text: `${label}: ` }), list.length ? list.join(joiner) : 'n/a']);
}

function stepList(block) {
  const steps = Array.isArray(block.steps) ? block.steps : [];
  if (!steps.length) {
    return el('p', { class: 'small muted', text: block.source === 'live' ? 'The build reported no steps.' : 'No live build steps.' });
  }
  return el('ol', { class: 'pipeline-steps', 'aria-label': 'Cloud Build steps' }, steps.map((step) => el('li', {}, [
    el('code', { text: plain(step.id) }), chip(step.status, step.tone), el('b', { class: 'small muted', text: duration(step.duration_ms) }),
  ])));
}

function bounceTargets(stage, data) {
  const own = stage ? routeIds(stage.bounce_to) : [];
  return own.length ? own : data.ci.bounce_to;
}

// Stage `bounce_to` may be a list of stage ids or a map (id -> reason, or reason -> id list); render it as given.
export function routeIds(value) {
  if (Array.isArray(value)) return value.filter((id) => typeof id === 'string');
  if (!value || typeof value !== 'object') return [];
  return Object.entries(value).map(([key, item]) => {
    if (typeof item === 'string') return `${key}: ${item}`;
    return Array.isArray(item) ? `${key}: ${item.filter((id) => typeof id === 'string').join(', ')}` : key;
  });
}

export function guardNote() {
  const data = current();
  if (!data) return 'Deterministic stages run fixed scripts; the DP-001 guard blocks CI/CD commands outside their stage.';
  return `Deterministic stages run fixed scripts; the DP-001 guard blocks ${plain(data.ci.provider)} before `
    + `${plain(data.stage_ids.release_gate)} and ${plain(data.cd.provider)} before ${plain(data.stage_ids.deploy_close)}.`;
}

function ciBody(data, kind, stage) {
  const { ci, provenance } = data;
  const finished = ci.finish_time ? `${utcTime(ci.finish_time)} (${duration(ci.duration_ms)})` : 'n/a';
  const rows = kind === 'drawer' ? [
    ['Stage', code(ci.stage)], ['Build id', origin(ci.build_id, provenance.build_id, code)],
    ['Image tag', origin(ci.image_tag, provenance.short_sha, tagCode)],
    ['Image digest', origin(ci.image_digest, provenance.image_digest, code)],
    ['Digest matches revision', typeof ci.digest_matches_provenance === 'boolean' ? text(ci.digest_matches_provenance) : 'n/a'],
    ['Created', utcTime(ci.create_time)], ['Finished', finished], ['Status detail', plain(ci.status_detail)],
    ['API latency', latency(ci.latency_ms)],
  ] : [
    ['Build', origin(ci.build_id, provenance.build_id, shortBuild)],
    ['Image tag', origin(ci.image_tag, provenance.short_sha, tagCode)],
    ['Digest', origin(ci.image_digest, provenance.image_digest, shortDigestCode)], ['Finished', finished],
  ];
  return [facts(rows), stepList(ci), routeLine('On failure, bounce to', bounceTargets(stage, data), ' / ')];
}

function cdBody(data, kind) {
  const { cd, provenance } = data;
  const deployMs = Date.parse(cd.deploy_end_time) - Date.parse(cd.deploy_start_time);
  const rows = [
    ['Pipeline', code(`${plain(cd.pipeline)} -> ${plain(cd.target)}`)], ['Release', code(cd.release)],
    ['Rollout', code(cd.rollout)], ['Serving revision', code(provenance.revision)],
    ['Deploy started', utcTime(cd.deploy_start_time)],
    ['Deploy finished', cd.deploy_end_time ? `${utcTime(cd.deploy_end_time)} (${duration(deployMs)})` : 'n/a'],
  ];
  if (kind === 'drawer') {
    rows.push(['Stage', code(cd.stage)], ['Failure reason', plain(cd.failure_reason)], ['Service', code(provenance.service)],
      ['API latency', latency(cd.latency_ms)]);
  }
  return [facts(rows), routeLine('Control loop', cd.control_loop, ' -> ')];
}

function provenanceFacts(provenance) {
  const rows = Object.entries(provenance).map(([key, value]) => [key.replaceAll('_', ' '),
    typeof value === 'boolean' ? text(value) : code(value)]);
  return [el('h4', { text: 'Serving revision provenance (environment)' }), facts(rows)];
}

function freshness(data, kind) {
  const stale = state.error ? el('p', { class: 'small pipeline-stale', role: 'alert' },
    [`Refresh failed at ${utcTime(state.failedAt)}: ${state.error.message}. Showing the previous result.`]) : null;
  const age = `Read ${utcTime(data.fetched_at)}; server cache age ${plain(data.cache_age_ms)} ms`;
  return compact([stale, kind === 'drawer' ? el('p', { class: 'small muted', text: age }) : null]);
}

function buildFrame(kind, role, stage) {
  const block = current()[role];
  const title = `${plain(block.provider)} ${role.toUpperCase()}`;
  const titleId = `live-${kind}-${role}-title`;
  const head = el('div', { class: 'pipeline-head' }, [el('h3', { id: titleId }, [icon(PANEL_ICONS[role]), title])]);
  const configFile = stage && stage.pipeline && typeof stage.pipeline.config_file === 'string' ? stage.pipeline.config_file : null;
  if (configFile) head.append(el('code', { class: 'small muted', text: configFile }));
  if (kind === 'drawer') {
    head.append(el('button', { class: 'btn btn-small', type: 'button', 'data-pipeline-refresh': role,
      'aria-label': `Refresh ${title} status`, onclick: () => refreshPipeline() }, [icon('refresh'), 'Refresh']));
  }
  return el('section', { class: `pipeline-panel pipeline-${kind}`, 'data-pipeline-role': role, 'aria-labelledby': titleId }, [
    head,
    el('div', { class: 'pipeline-status', role: 'status', 'aria-live': 'polite', 'data-pipeline-status': role }),
    el('div', { class: 'pipeline-body', 'data-pipeline-body': role }),
  ]);
}

function fill(section, kind, role, stage) {
  const data = current();
  const block = data[role];
  clear(section.querySelector('[data-pipeline-status]')).append(...statusContent(block, role));
  const drawer = kind === 'drawer';
  const body = role === 'ci' ? ciBody(data, kind, stage) : cdBody(data, kind);
  const link = consoleLink(block.console_url, role === 'ci' ? 'Open this build in the Cloud console' : 'Open this release in the Cloud console');
  clear(section.querySelector('[data-pipeline-body]')).append(...compact([
    drawer ? sourceNotice(data.source_label, `pipeline-${role}-drawer`) : null, ...body, sourceLine(block, role),
    errorLine(block.error), link, ...(drawer ? provenanceFacts(data.provenance) : []), ...freshness(data, kind),
  ]));
}

function syncSlot(slot) {
  const stage = stages.get(slot.dataset.pipelineSlot);
  const role = liveRole(slot.dataset.pipelineSlot);
  const kind = slot.dataset.pipelineKind;
  let section = slot.firstElementChild;
  if (!role) {
    clear(slot);
  } else {
    if (!section || section.dataset.pipelineRole !== role) {
      section = buildFrame(kind, role, stage);
      slot.replaceChildren(section);
    }
    fill(section, kind, role, stage);
  }
  const item = kind === 'card' ? slot.closest('li') : null;
  if (item) {
    item.classList.toggle('has-pipeline', Boolean(role));
    if (role) item.dataset.liveRole = role;
    else delete item.dataset.liveRole;
  }
  return Boolean(role);
}

function overviewContent() {
  const data = current();
  if (!data) {
    const status = state.error ? el('p', { class: 'small', role: 'alert', text: `Live CI/CD status unavailable: ${state.error.message}` })
      : el('div', { class: 'state state-loading', role: 'status' }, ['Reading live CI/CD status']);
    return [el('h3', {}, [icon('flow'), 'Delivery pipeline']), status];
  }
  const row = (label, block, value) => el('div', { class: 'pipeline-overview-row' }, [el('b', { text: label }), chip(value, block.tone),
    el('code', { class: 'small muted', text: plain(block.stage) })]);
  const labels = [...new Set([plain(data.ci.source_label), plain(data.cd.source_label)])].join('; ');
  return [
    el('div', { class: 'card-head' }, [el('h3', {}, [icon('flow'), 'Delivery pipeline']),
      el('a', { class: 'small', href: '#workflow' }, ['Open workflow stages'])]),
    row(`${plain(data.ci.provider)} CI`, data.ci, data.ci.status), row(`${plain(data.cd.provider)} CD`, data.cd, data.cd.rollout_state),
    facts([['Serving revision', code(data.provenance.revision)], ['Release', code(data.cd.release)]]),
    el('p', { class: 'small muted', 'data-pipeline-source': 'overview', text: labels }),
  ];
}

function alertContent(matched) {
  const data = current();
  if (!data && state.error) return [el('div', { class: 'state state-error', role: 'alert' }, [icon('alert'), `Live CI/CD status unavailable: ${state.error.message}`])];
  if (data && !matched) {
    const ids = `${plain(data.stage_ids.release_gate)} / ${plain(data.stage_ids.deploy_close)}`;
    return [el('div', { class: 'state state-error', role: 'alert' }, [icon('alert'), `Live CI/CD stage ids ${ids} match no workflow stage.`])];
  }
  return [];
}

function metaText() {
  const data = current();
  if (!data) return state.error ? `Live CI/CD unavailable: ${state.error.message}` : '';
  const { clientMs, traceId } = state.result;
  const prefix = state.error ? `Refresh failed (${state.error.message}); showing data` : 'Live CI/CD';
  return `${prefix} read ${utcTime(data.fetched_at)}, cache age ${plain(data.cache_age_ms)} ms, round trip ${clientMs} ms, `
    + `trace ${shortText(traceId, 'short_trace_chars')}`;
}

export function syncPipeline(root = document) {
  let matched = false;
  for (const slot of root.querySelectorAll('[data-pipeline-slot]')) {
    const shown = syncSlot(slot);
    matched = matched || (shown && slot.dataset.pipelineKind === 'card');
  }
  if (root !== document) return;
  for (const timeline of document.querySelectorAll('.timeline')) {
    timeline.classList.toggle('with-pipeline', Boolean(timeline.querySelector('li.has-pipeline')));
  }
  const cards = document.querySelectorAll('[data-pipeline-slot][data-pipeline-kind="card"]').length;
  for (const box of document.querySelectorAll('[data-pipeline-alert]')) clear(box).append(...(cards ? alertContent(matched) : []));
  for (const box of document.querySelectorAll('[data-pipeline-overview]')) clear(box).append(...overviewContent());
  if (controls.output) controls.output.textContent = metaText();
}

function setBusy(busy) {
  for (const node of [controls.button, ...document.querySelectorAll('[data-pipeline-role], [data-pipeline-refresh]')]) {
    if (node) node.setAttribute('aria-busy', String(busy));
  }
}

export function refreshPipeline() {
  if (state.pending) return state.pending;
  setBusy(true);
  state.pending = api('/api/pipeline')
    .then((result) => { state.result = result; state.error = null; })
    .catch((err) => { state.error = err; state.failedAt = new Date().toISOString(); })
    .finally(() => { state.pending = null; setBusy(false); syncPipeline(); });
  return state.pending;
}

export function registerStages(list) {
  stages.clear();
  for (const stage of list) stages.set(stage.id, stage);
}

export function pipelineSlot(stage, kind) {
  stages.set(stage.id, stage);
  return el('div', { class: 'pipeline-slot', 'data-pipeline-slot': stage.id, 'data-pipeline-kind': kind });
}

export const pipelineAlert = () => el('div', { class: 'pipeline-alert', 'data-pipeline-alert': '' });
export const pipelineOverview = () => el('article', { class: 'card pipeline-overview', id: 'overview-pipeline', 'data-pipeline-overview': '' });

export function initPipelineControls({ button, output }) {
  controls.button = button;
  controls.output = output;
  button.addEventListener('click', () => refreshPipeline());
}
