// Safe DOM helpers: data is only ever written with textContent, and missing values render as "n/a".
const SVG_NS = 'http://www.w3.org/2000/svg';
let settings = null;

export function setSettings(value) {
  settings = value;
}

export function uiSetting(name) {
  if (!settings || !Object.prototype.hasOwnProperty.call(settings, name)) {
    throw new Error(`UI setting "${name}" is missing from /api/ui-config`);
  }
  return settings[name];
}

export function text(value) {
  if (value === null || value === undefined) return 'n/a';
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : 'n/a';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (typeof value === 'object') throw new TypeError('refusing to render an object as text');
  return String(value);
}

export function icon(name, extraClass = '') {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', `icon ${extraClass}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = text(value);
    else if (key === 'onclick') node.addEventListener('click', value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : text(child));
  }
  return node;
}

export function badge(label, state, iconName = null) {
  return el('b', { class: 'badge', 'data-state': state }, [iconName ? icon(iconName) : null, label]);
}

export function stateBadge(state) {
  return badge(text(state).replaceAll('_', ' '), state);
}

export function verdictBadge(passed) {
  return passed ? badge('PASS', 'PASS', 'check') : badge('FAIL', 'FAIL', 'cross');
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function loading(container, label) {
  clear(container).append(el('div', { class: 'state state-loading', role: 'status' }, [label]));
}

export function failure(container, err) {
  const message = err && err.message ? err.message : 'unexpected error';
  clear(container).append(el('div', { class: 'state state-error', role: 'alert' }, [
    icon('alert'),
    el('div', {}, [el('strong', { text: 'Could not load this section. ' }), message]),
  ]));
}

export function percent(fraction, digitsSetting = 'percent_fraction_digits') {
  if (!Number.isFinite(fraction)) return 'n/a';
  const digits = uiSetting(digitsSetting);
  return new Intl.NumberFormat('en-US', { style: 'percent', maximumFractionDigits: digits }).format(fraction);
}

export function ratio(fraction) {
  return percent(fraction, 'ratio_fraction_digits');
}

export function utcTime(iso) {
  if (typeof iso !== 'string' || !iso) return 'n/a';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const formatted = new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC' }).format(date);
  return `${formatted} UTC`;
}

export function shortText(value, setting) {
  return text(value).slice(0, uiSetting(setting));
}

export function facts(pairs) {
  const list = el('dl', { class: 'facts' });
  for (const [label, value] of pairs) {
    list.append(el('dt', { text: label }), el('dd', {}, [value]));
  }
  return list;
}

export function bulletList(items) {
  return el('ul', { class: 'plain' }, items.map((item) => el('li', {}, [item])));
}

export function table(columns, rows, caption) {
  const head = el('thead', {}, [el('tr', {}, columns.map((col) => el('th', { scope: 'col', text: col.label })))]);
  const body = el('tbody', {}, rows.map((row) => el('tr', row.attrs || {},
    columns.map((col) => el('td', { 'data-label': col.label }, [row.cells[col.key]])))));
  return el('table', { class: 'table' }, [caption ? el('caption', { text: caption }) : null, head, body]);
}

export function section(title, iconName, children) {
  return el('section', { class: 'card' }, [el('h3', { class: 'section-title' }, [icon(iconName), title]), ...children]);
}
