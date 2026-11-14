// Workflow tab: hybrid S1..S6 stage timeline; each stage opens a drawer with owners, gates, and evidence.
import { api } from './api.js';
import { openDrawer } from './drawer.js';
import { badge, bulletList, clear, el, facts, icon, stateBadge, text } from './dom.js';

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

function renderStageDrawer(dialog, stage, total, roster) {
  dialog.querySelector('#stage-drawer-title').textContent = `${stage.id}: ${stage.title}`;
  const body = clear(dialog.querySelector('#stage-drawer-body'));
  body.append(
    el('div', { class: 'stage-row' }, [modeBadge(stage.mode), stateBadge(stage.status)]),
    el('p', { text: stage.summary }),
    facts([
      ['Position', `Stage ${stage.position} of ${total}`],
      ['Mode', stage.mode_description],
      ['Status', text(stage.status)],
    ]),
    el('h3', { text: 'Owners' }), ownerList(stage.owners, roster),
    el('h3', { text: 'Entry gates' }), bulletList(stage.entry_gates),
    el('h3', { text: 'Exit gates' }), bulletList(stage.exit_gates),
    el('h3', { text: 'Evidence' }), bulletList(stage.evidence),
  );
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
  const [{ data: workflow }, { data: roster }] = await Promise.all([api('/api/workflow'), api('/api/agents')]);
  const agents = new Map(roster.agents.map((agent) => [agent.id, agent]));
  document.getElementById('workflow-mode').textContent = workflow.mode_label;
  const onOpen = (stage, button) => {
    renderStageDrawer(dialog, stage, workflow.stages.length, agents);
    openDrawer(dialog, button);
  };
  const legend = el('div', { class: 'legend' }, [
    badge(workflow.mode_label, 'info', 'flow'), modeBadge('agentic'), modeBadge('deterministic'),
    el('b', { text: `Current stage: ${workflow.current_stage}` }),
    el('b', { class: 'muted', text: `Workflow ${workflow.workflow_id} v${workflow.version}; ${roster.count} agents on the roster` }),
  ]);
  const timeline = el('ol', { class: 'timeline', 'aria-label': 'Workflow stages' },
    workflow.stages.map((stage) => el('li', {}, [stageButton(stage, onOpen)])));
  clear(container).append(legend, timeline, el('p', { class: 'small muted' }, [icon('lock'),
    ' Deterministic stages run fixed scripts; the DP-001 guard blocks S6 commands before S5_RELEASE_GATE.']));
}
