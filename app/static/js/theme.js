// Theme toggle: persists in localStorage; the first paint honors prefers-color-scheme (theme-init.js).
const STORAGE_KEY = 'console-theme';

export function initTheme() {
  const root = document.documentElement;
  const button = document.getElementById('theme-toggle');
  const sync = () => {
    const dark = root.getAttribute('data-theme') === 'dark';
    button.setAttribute('aria-pressed', String(dark));
    button.setAttribute('title', dark ? 'Switch to light theme' : 'Switch to dark theme');
  };
  sync();
  button.addEventListener('click', () => {
    const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch (err) {
      console.warn('Theme preference could not be saved', err);
    }
    sync();
  });
}
