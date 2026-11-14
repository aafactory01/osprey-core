// Runs before first paint: apply the saved theme, else honor prefers-color-scheme.
(function applyInitialTheme() {
  'use strict';
  var saved = null;
  try {
    saved = window.localStorage.getItem('console-theme');
  } catch (err) {
    saved = null;
  }
  var prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  var theme = saved === 'dark' || saved === 'light' ? saved : (prefersDark ? 'dark' : 'light');
  document.documentElement.setAttribute('data-theme', theme);
}());
