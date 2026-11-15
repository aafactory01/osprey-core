// Overview tab: SLA/SLO health from live telemetry, live delivery pipeline, rollout ledger, and recent telemetry.
import { api } from './api.js';
import {
  badge, clear, el, failure, loading, percent, ratio, section, shortText, sourceNotice, stateBadge, table, text, uiSetting, utcTime,
} from './dom.js';
import { pipelineOverview, refreshPipeline, syncPipeline } from './pipeline.js';

function sloCard(slo) {
  const head = el('div', { class: 'card-head' }, [el('h3', { text: slo.name }), stateBadge(slo.state)]);
  const lines = [head, el('p', { class: 'muted small', text: slo.description })];
  if (slo.state === 'INSUFFICIENT_DATA') {
    lines.push(el('p', { text: `Waiting for data: ${slo.sample_size} of ${slo.min_samples} required samples.` }));
    return el('article', { class: 'card', 'data-slo': slo.id }, lines);
  }
  if (slo.kind === 'success_ratio') {
    lines.push(el('div', { class: 'kpi-row' }, [el('p', { class: 'kpi', text: ratio(slo.measured) }),
      el('p', { class: 'muted', text: `target ${ratio(slo.target)}` })]));
    lines.push(el('p', { class: 'small', text: `${slo.failures} server error(s) in ${slo.sample_size} requests; error budget left ${percent(slo.error_budget_remaining)}` }));
  } else {
    lines.push(el('div', { class: 'kpi-row' }, [el('p', { class: 'kpi', text: `${text(slo.measured_ms)} ms` }),
      el('p', { class: 'muted', text: `target ${text(slo.target_ms)} ms at the ${percent(slo.percentile)} percentile` })]));
    lines.push(el('p', { class: 'small', text: `${slo.sample_size} measured requests` }));
  }
  return el('article', { class: 'card', 'data-slo': slo.id }, lines);
}

function healthView(health) {
  const status = el('article', { class: 'card', id: 'health-status' }, [
    el('div', { class: 'card-head' }, [el('h3', { text: 'Service health' }), stateBadge(health.status)]),
    el('p', { class: 'kpi', text: health.service }),
    el('p', { class: 'small muted', text: `${health.source_label}; ${health.sample_size} sampled requests` }),
  ]);
  const sla = el('article', { class: 'card' }, [
    el('div', { class: 'card-head' }, [el('h3', { text: health.sla.name }), badge('SLA', 'info')]),
    el('p', { class: 'kpi', text: ratio(health.sla.target) }),
    el('p', { class: 'small muted', text: health.sla.window_label }),
  ]);
  return el('div', { class: 'grid' }, [status, sla, ...health.slos.map(sloCard)]);
}

function rolloutsView(data) {
  const rows = data.rollouts.map((item) => ({
    attrs: { 'data-rollout': item.id },
    cells: {
      id: el('code', { text: item.id }),
      strategy: text(item.strategy),
      revision: el('code', { text: item.revision }),
      status: stateBadge(item.status),
      traffic: el('div', { class: 'score' }, [
        el('progress', { class: 'meter is-good', max: data.traffic_total_percent, value: item.traffic_percent, 'aria-label': `${item.id} traffic share` }),
        el('b', { text: `${item.traffic_percent}%` }),
      ]),
      owner: text(item.owner),
      started: utcTime(item.started_at),
      gate: text(item.health_gate),
    },
  }));
  const columns = [
    { key: 'id', label: 'Rollout' }, { key: 'strategy', label: 'Strategy' }, { key: 'revision', label: 'Revision' },
    { key: 'status', label: 'Status' }, { key: 'traffic', label: 'Traffic' }, { key: 'owner', label: 'Owner' },
    { key: 'started', label: 'Started' }, { key: 'gate', label: 'Health gate' },
  ];
  return section('Rollouts', 'flow', [
    sourceNotice(data.source_label, 'rollouts'),
    el('p', { class: 'small muted', text: `Active traffic in this ${data.scenario} ledger: ${data.active_traffic_percent}%` }),
    table(columns, rows),
  ]);
}

function telemetryView(data) {
  const rows = data.events.map((event) => ({
    cells: {
      time: utcTime(event.ts),
      op: el('code', { text: text(event.op) }),
      status: event.kind === 'http_request' ? text(event.http_status) : text(event.status),
      latency: `${text(event.latency_ms)} ms`,
      trace: el('code', { text: shortText(event.trace_id, 'short_trace_chars') }),
    },
  }));
  const columns = [
    { key: 'time', label: 'Time' }, { key: 'op', label: 'Operation' }, { key: 'status', label: 'Status' },
    { key: 'latency', label: 'Latency' }, { key: 'trace', label: 'Trace' },
  ];
  return section('Recent telemetry', 'gauge', [
    el('p', { class: 'small muted', text: `Last ${data.count} of ${text(data.capacity)} buffered events (measured with perf_counter).` }),
    table(columns, rows),
  ]);
}

async function settle(container, request, render) {
  try {
    const { data } = await request;
    clear(container).append(render(data));
  } catch (err) {
    failure(container, err);
  }
}

export async function loadOverview(container) {
  const healthBox = el('div', { id: 'overview-health' });
  const pipelineBox = pipelineOverview();
  const rolloutBox = el('div', { id: 'overview-rollouts' });
  const telemetryBox = el('div', { id: 'overview-telemetry' });
  clear(container).append(healthBox, pipelineBox, rolloutBox, telemetryBox);
  loading(healthBox, 'Loading SLA and SLO health');
  loading(rolloutBox, 'Loading rollouts');
  loading(telemetryBox, 'Loading telemetry');
  syncPipeline();
  await Promise.all([
    settle(healthBox, api('/api/health'), healthView),
    refreshPipeline(),
    settle(rolloutBox, api('/api/rollouts'), rolloutsView),
    settle(telemetryBox, api(`/api/telemetry?limit=${uiSetting('telemetry_rows')}`), telemetryView),
  ]);
}
