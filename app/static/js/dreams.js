// Night Dreaming tab + DP-001 drawer: proposal, computed scorecard, per-case replay rows, and re-run.
import { api } from './api.js';
import { openDrawer } from './drawer.js';
import { badge, bulletList, clear, el, facts, failure, icon, section, shortText, stateBadge, table, text, utcTime, verdictBadge } from './dom.js';

let latest = null;

function versions(proposal) {
  return el('b', { class: 'versions' }, [`v${proposal.from_version}`, icon('arrow'), `v${proposal.to_version}`]);
}

function scoreRow(label, side, good) {
  return el('div', { class: 'score', 'data-side': label.toLowerCase() }, [
    el('div', { class: 'score-row' }, [
      el('b', { text: `${label} v${side.version}` }),
      el('b', { class: 'kpi', text: side.pass_rate_label }),
    ]),
    el('progress', { class: good ? 'meter is-good' : 'meter', max: side.total, value: side.passed }),
    el('p', { class: 'small muted', text: `${side.passed}/${side.total} correct; ${side.false_allows} false allow(s), ${side.false_blocks} false block(s)` }),
  ]);
}

function scorecard(replay, context) {
  return section(`${replay.skill} scorecard`, 'gauge', [
    el('div', { class: 'kpi-row' }, [el('b', { class: 'kpi', id: `${context}-score-baseline`, text: replay.baseline.pass_rate_label }), icon('arrow'),
      el('b', { class: 'kpi', id: `${context}-score-candidate`, text: replay.candidate.pass_rate_label })]),
    scoreRow('Baseline', replay.baseline, false),
    scoreRow('Candidate', replay.candidate, replay.candidate.passed >= replay.baseline.passed),
    el('div', { class: 'stage-row' }, [el('b', { text: 'Gate' }), stateBadge(replay.gate.verdict), el('b', { text: 'Status' }), stateBadge(replay.status)]),
  ]);
}

function decisionCell(side) {
  return el('div', { class: 'stage-row' }, [stateBadge(side.decision), verdictBadge(side.pass), el('code', { class: 'small', text: side.rule_id })]);
}

function casesTable(replay) {
  const columns = [
    { key: 'id', label: 'Case' }, { key: 'title', label: 'Recorded tool call' }, { key: 'stage', label: 'Stage' },
    { key: 'expected', label: 'Expected' }, { key: 'baseline', label: `Baseline v${replay.baseline.version}` },
    { key: 'candidate', label: `Candidate v${replay.candidate.version}` },
  ];
  const rows = replay.cases.map((item) => ({
    attrs: { 'data-case': item.id },
    cells: {
      id: el('code', { text: item.id }),
      title: el('div', {}, [el('b', { text: item.title }), el('p', { class: 'small' }, [el('code', { text: `${text(item.tool_name)}: ${text(item.subject)}` })])]),
      stage: el('code', { text: text(item.stage) }),
      expected: stateBadge(item.expected),
      baseline: decisionCell(item.baseline),
      candidate: decisionCell(item.candidate),
    },
  }));
  return section('Per-case shadow replay', 'play', [table(columns, rows, `${replay.cases.length} recorded cases from ${text(replay.eval_set_id)}`)]);
}

function ruleDiff(diff) {
  const chips = (ids) => (ids.length ? el('ul', { class: 'chip-list' }, ids.map((id) => el('li', {}, [badge(id, 'neutral')]))) : 'none');
  return section('Rule diff', 'flow', [facts([['Added', chips(diff.added)], ['Modified', chips(diff.modified)], ['Removed', chips(diff.removed)]])]);
}

function nightlyLine(nightly) {
  if (!nightly.present) return `Nightly scorecard: ${nightly.detail}.`;
  if (!nightly.valid) return `Nightly scorecard ${nightly.file} is invalid: ${nightly.detail}.`;
  const agreement = nightly.consistent_with_live ? 'matches the live replay' : 'DIFFERS from the live replay';
  return `Nightly run ${utcTime(nightly.generated_at)} (${nightly.file}) ${agreement}.`;
}

function renderDrawer(dialog, payload) {
  const { proposal, replay } = payload;
  dialog.querySelector('#dream-drawer-title').textContent = `${proposal.id}: ${proposal.title}`;
  clear(dialog.querySelector('#dream-drawer-body')).append(
    section('Proposal', 'dream', [
      facts([
        ['Skill', el('div', {}, [el('code', { text: proposal.skill }), ' ', versions(proposal)])],
        ['Status', stateBadge(replay.status)],
        ['Active policy version', el('code', { text: replay.active_version })],
        ['Guard hook', el('code', { text: proposal.hook })],
        ['Policy file', el('code', { text: proposal.policy_file })],
        ['Human gate', proposal.human_gate],
      ]),
      el('h3', { text: 'Why' }), bulletList(proposal.rationale),
    ]),
    scorecard(replay, 'drawer'),
    section('Gate checks', 'check', [bulletList(replay.gate.checks.map((check) => el('div', { class: 'stage-row' },
      [verdictBadge(check.pass), el('code', { text: check.check }), el('b', { class: 'muted', text: check.detail })])))]),
    ruleDiff(replay.rule_diff),
    casesTable(replay),
    el('p', { class: 'small muted', text: nightlyLine(payload.nightly) }),
  );
}

function replayMeta(result) {
  const { data, clientMs, traceId } = result;
  return `Computed on the server at ${utcTime(data.generated_at)}: replay ${data.replay.replay_latency_us} us, `
    + `request ${data.latency_ms} ms server / ${clientMs} ms round trip, trace ${shortText(traceId, 'short_trace_chars')}`;
}

function renderSummary(container, result) {
  const { proposal, replay } = result.data;
  clear(container).append(el('div', { class: 'grid' }, [
    el('article', { class: 'card', id: 'dream-summary' }, [
      el('div', { class: 'card-head' }, [el('h3', { text: `${proposal.id}: ${proposal.title}` }), stateBadge(replay.status)]),
      el('p', {}, [el('code', { text: proposal.skill }), ' ', versions(proposal)]),
      el('p', { class: 'small muted', text: nightlyLine(result.data.nightly) }),
    ]),
    scorecard(replay, 'summary'),
  ]));
}

export async function loadDreams(container) {
  latest = await api('/api/dreams');
  renderSummary(container, latest);
}

export function initDreamDrawer({ dialog, openButton, runButton, meta, summary }) {
  openButton.addEventListener('click', async () => {
    try {
      if (!latest) latest = await api('/api/dreams');
      renderDrawer(dialog, latest.data);
      meta.textContent = replayMeta(latest);
      openDrawer(dialog, openButton);
    } catch (err) {
      failure(summary, err);
    }
  });
  runButton.addEventListener('click', async () => {
    runButton.setAttribute('aria-busy', 'true');
    meta.textContent = 'Re-running shadow replay on the server';
    try {
      latest = await api('/api/dreams/replay', { method: 'POST', body: {} });
      renderDrawer(dialog, latest.data);
      renderSummary(summary, latest);
      meta.textContent = replayMeta(latest);
    } catch (err) {
      meta.textContent = `Replay failed: ${err.message}`;
    } finally {
      runButton.removeAttribute('aria-busy');
    }
  });
}
