// Modal drawers on <dialog>: focus trap, Esc / overlay / close button, focus returns to the opener.
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), '
  + 'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const openers = new WeakMap();

function focusables(dialog) {
  return Array.from(dialog.querySelectorAll(FOCUSABLE)).filter((node) => node.getClientRects().length > 0);
}

export function initDrawer(dialog) {
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close('overlay');
  });
  dialog.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const items = focusables(dialog);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  for (const button of dialog.querySelectorAll('[data-close]')) {
    button.addEventListener('click', () => dialog.close('button'));
  }
  dialog.addEventListener('close', () => {
    const opener = openers.get(dialog);
    if (opener && document.contains(opener)) opener.focus();
  });
}

export function openDrawer(dialog, opener) {
  openers.set(dialog, opener || document.activeElement);
  if (!dialog.open) dialog.showModal();
  const closeButton = dialog.querySelector('[data-close]');
  if (closeButton) closeButton.focus();
}
