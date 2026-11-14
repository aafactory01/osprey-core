// Workflow tab: hybrid stage timeline; each stage opens a drawer with owners, gates, and evidence.
// The release-gate and deploy-close stages (ids from /api/pipeline `stage_ids`) also carry live CI/CD panels.
import { api } from './api.js';
import { openDrawer } from './drawer.js';
import { badge, bulletList, clear, el, facts, icon, sourceNotice, stateBadge, text } from './dom.js';
import { guardNote, pipelineAlert, pipelineSlot, refreshPipeline, registerStages, routeIds, syncPipeline } from './pipeline.js';

const plain = (value) => (typeof value === 'string' || typeof value === 'number' ? text(value) : 'n/a');

function modeBadge(mode) {
  return mode === 'deterministic' ? badge('Deterministic', 'deterministic', 'lock') : badge('Agentic', 'agentic', 'agent');
}

function ownerList(owners, roster) {
  return bulletList(owners.map((owner) => {
    const agent = roster.get(owner);
    const detail = agent ? agent.responsibility : 'Not in the agent roster';
    return el('div', {}, [el('code', { text: owner }), el('p', { class: 'small muted', text: detail })]);
  }));
}

// Optional stage keys: `bounce_to` (stage ids to return to on failure) and `pipeline` (CI/CD entry point).
function stageRouting(stage) {
  const parts = [];
  const bounce = routeIds(stage.bounce_to);
  if (bounce.length) {
    parts.push(el('h3', { text: 'Bounces back to on failure' }), bulletList(bounce.map((id) => el('code', { text: id }))));
  }
  const pipeline = stage.pipeline && typeof stage.pipeline === 'object' ? stage.pipeline : null;
  if (pipeline) {
    parts.push(el('h3', { text: 'Pipeline' }), facts([
      ['Kind', plain(pipeline.kind)], ['Provider', plain(pipeline.provider)],
      ['Config file', el('code', { text: plain(pipeline.config_file) })],
      ['Command', el('code', { text: plain(pipeline.command) })],
    ]));
  }
  return parts;
}

function renderStageDrawer(dialog, stage, total, roster, workflow) {
  dialog.querySelector('#stage-drawer-title').textContent = `${stage.id}: ${stage.title}`;
  const body = clear(dialog.querySelector('#stage-drawer-body'));
  body.append(
    sourceNotice(workflow.source_label, 'workflow-stage'),
    el('div', { class: 'stage-row' }, [modeBadge(stage.mode), stateBadge(stage.status)]),
    el('p', { text: stage.summary }),
    facts([
      ['Position', `Stage ${stage.position} of ${total}`],
      ['Mode', stage.mode_description],
      ['Status', `${text(stage.status)} (${workflow.scenario} scenario)`],
    ]),
    pipelineSlot(stage, 'drawer'),
    el('h3', { text: 'Owners' }), ownerList(stage.owners, roster),
    el('h3', { text: 'Entry gates' }), bulletList(stage.entry_gates),
    el('h3', { text: 'Exit gates' }), bulletList(stage.exit_gates),
    el('h3', { text: 'Evidence' }), bulletList(stage.evidence),
    ...stageRouting(stage),
  );
  syncPipeline(body);
}

function stageButton(stage, onOpen) {
  const button = el('button', {
    class: 'stage', type: 'button', 'data-stage': stage.id, 'data-mode': stage.mode, 'data-status': stage.status,
    'aria-haspopup': 'dialog', 'aria-controls': 'stage-drawer',
    'aria-label': `${stage.id} ${stage.title}, ${stage.mode}, ${stage.status}. Open details`,
  }, [
    el('b', { class: 'stage-id', text: stage.id }),
    el('b', { class: 'stage-title', text: stage.title }),
    el('div', { class: 'stage-row' }, [modeBadge(stage.mode), stateBadge(stage.status)]),
    el('p', { class: 'small muted', text: `${stage.owners.length} owner(s)` }),
  ]);
  button.addEventListener('click', () => onOpen(stage, button));
  return button;
}

export async function loadWorkflow(container, dialog) {
  const live = refreshPipeline();
  const [{ data: workflow }, { data: roster }] = await Promise.all([api('/api/workflow'), api('/api/agents')]);
  await live;
  registerStages(workflow.stages);
  const agents = new Map(roster.agents.map((agent) => [agent.id, agent]));
  document.getElementById('workflow-mode').textContent = workflow.mode_label;
  const onOpen = (stage, button) => {
    renderStageDrawer(dialog, stage, workflow.stages.length, agents, workflow);
    openDrawer(dialog, button);
  };
  const legend = el('div', { class: 'legend' }, [
    badge(workflow.mode_label, 'info', 'flow'), modeBadge('agentic'), modeBadge('deterministic'),
    el('b', { text: `Scenario stage: ${workflow.current_stage} (${workflow.scenario})` }),
    el('b', { class: 'muted', text: `Workflow ${workflow.workflow_id} v${workflow.version}; ${roster.count} agents on the roster` }),
  ]);
  const timeline = el('ol', { class: 'timeline', 'aria-label': 'Workflow stages' },
    workflow.stages.map((stage) => el('li', {}, [stageButton(stage, onOpen), pipelineSlot(stage, 'card')])));
  clear(container).append(sourceNotice(workflow.source_label, 'workflow'), legend, timeline, pipelineAlert(),
    el('p', { class: 'small muted', 'data-guard-note': '' }, [icon('lock'), ` ${guardNote()}`,
      ' Live CI/CD panels read Cloud Build and Cloud Deploy for the serving revision.']));
  syncPipeline();
}
