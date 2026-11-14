// Console bootstrap: settings from /api/ui-config, then lazy per-tab loading with explicit error states.
import { api, setTimeoutMs } from './api.js';
import { loadBackends } from './backends.js';
import { initDrawer } from './drawer.js';
import { initDreamDrawer, loadDreams } from './dreams.js';
import { failure, loading, setSettings } from './dom.js';
import { loadOverview } from './overview.js';
import { initPipelineControls } from './pipeline.js';
import { loadReceipts } from './receipts.js';
import { initTabs } from './tabs.js';
import { initTheme } from './theme.js';
import { loadWorkflow } from './workflow.js';

const byId = (id) => document.getElementById(id);
const loaded = new Set();
const pending = new Map();

const panels = {
  overview: { body: 'overview-body', label: 'Loading health and rollouts', load: (node) => loadOverview(node) },
  workflow: { body: 'workflow-body', label: 'Loading workflow stages', load: (node) => loadWorkflow(node, byId('stage-drawer')) },
  'task-backend': { body: 'backend-body', label: 'Loading task backends', load: (node) => loadBackends(node) },
  dreams: { body: 'dreams-body', label: 'Running the shadow replay', load: (node) => loadDreams(node) },
  receipts: { body: 'receipts-body', label: 'Loading audit receipts', load: (node) => loadReceipts(node, byId('chain-verify')) },
};

function ensureLoaded(name, force = false) {
  const panel = panels[name];
  if (!panel || (loaded.has(name) && !force) || pending.has(name)) return pending.get(name);
  const node = byId(panel.body);
  loading(node, panel.label);
  const job = panel.load(node)
    .then(() => loaded.add(name))
    .catch((err) => failure(node, err))
    .finally(() => pending.delete(name));
  pending.set(name, job);
  return job;
}

async function boot() {
  initTheme();
  initDrawer(byId('stage-drawer'));
  initDrawer(byId('dream-drawer'));
  let config;
  try {
    config = (await api('/api/ui-config')).data;
  } catch (err) {
    byId('service-meta').textContent = 'Console configuration unavailable';
    for (const panel of Object.values(panels)) failure(byId(panel.body), err);
    initTabs({ defaultTab: 'overview', onActivate: () => undefined });
    return;
  }
  setSettings(config.ui);
  setTimeoutMs(config.ui.fetch_timeout_ms);
  byId('console-title').textContent = config.console_title;
  byId('service-meta').textContent = `Service ${config.service_name}, Cloud Run service ${config.cloud_run_service}, revision ${config.revision}`;
  initDreamDrawer({
    dialog: byId('dream-drawer'), openButton: byId('dream-open'), runButton: byId('replay-run'),
    meta: byId('replay-meta'), summary: byId('dreams-body'),
  });
  byId('overview-refresh').addEventListener('click', () => ensureLoaded('overview', true));
  initPipelineControls({ button: byId('pipeline-refresh'), output: byId('pipeline-meta') });
  initTabs({ defaultTab: config.ui.default_tab, onActivate: (name) => ensureLoaded(name) });
  document.body.dataset.ready = 'true';
}

boot();
