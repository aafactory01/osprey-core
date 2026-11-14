// Accessible tabs: roving tabindex, arrow/Home/End keys, and URL-hash deep links (#workflow, ...).
export function initTabs({ defaultTab, onActivate }) {
  const tablist = document.getElementById('tablist');
  const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
  const byHash = new Map(tabs.map((tab) => [tab.dataset.hash, tab]));
  const hashName = () => window.location.hash.replace('#', '');

  function activate(tab, { focus = false, pushHash = false } = {}) {
    for (const other of tabs) {
      const selected = other === tab;
      other.setAttribute('aria-selected', String(selected));
      other.tabIndex = selected ? 0 : -1;
      document.getElementById(other.getAttribute('aria-controls')).hidden = !selected;
    }
    if (focus) tab.focus();
    if (pushHash && hashName() !== tab.dataset.hash) {
      window.history.pushState(null, '', `#${tab.dataset.hash}`);
    }
    onActivate(tab.dataset.hash);
  }

  tablist.addEventListener('click', (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (tab) activate(tab, { pushHash: true });
  });

  tablist.addEventListener('keydown', (event) => {
    const index = tabs.indexOf(document.activeElement);
    if (index < 0) return;
    let next = null;
    if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
    else if (event.key === 'ArrowLeft') next = tabs[(index - 1 + tabs.length) % tabs.length];
    else if (event.key === 'Home') next = tabs[0];
    else if (event.key === 'End') next = tabs[tabs.length - 1];
    if (next) {
      event.preventDefault();
      activate(next, { focus: true, pushHash: true });
    }
  });

  window.addEventListener('hashchange', () => {
    const tab = byHash.get(hashName());
    if (tab) activate(tab);
  });

  activate(byHash.get(hashName()) || byHash.get(defaultTab) || tabs[0]);
}
