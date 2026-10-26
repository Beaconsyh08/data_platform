// Shared keyboard/focus behavior for the workspace and standalone Pipeline sidebar.
function bindAssistantControls({panel, trigger, collapse, isOpen, setOpen, enabled = () => true}) {
  let previousFocus = null;
  const shortcut = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘ ⇧ Space' : 'Ctrl Shift Space';
  const refresh = () => {
    trigger.hidden = isOpen();
    trigger.setAttribute('aria-expanded', String(isOpen()));
  };
  const open = () => {
    if (!enabled()) return;
    if (!isOpen()) {
      previousFocus = document.activeElement;
      while (previousFocus?.shadowRoot?.activeElement) previousFocus = previousFocus.shadowRoot.activeElement;
    }
    setOpen(true); refresh();
    const input = panel.querySelector('#ai-message');
    (input && !input.disabled ? input : collapse).focus();
  };
  const close = () => {
    setOpen(false); refresh();
    const destination = previousFocus?.isConnected && previousFocus.getClientRects().length && !panel.contains(previousFocus) ? previousFocus : trigger;
    destination.focus({preventScroll:true});
  };
  const keydown = event => {
    if (!enabled() || event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
    const toggle = (event.code === 'Space' || event.key === ' ') && event.shiftKey && (event.ctrlKey !== event.metaKey) && !event.altKey;
    const escape = event.key === 'Escape' && isOpen() && event.composedPath().includes(panel) && event.composedPath()[0]?.tagName !== 'SELECT';
    if (!toggle && !escape) return;
    // Keep modal dialogs in charge of their own keyboard and focus.
    if ([...document.querySelectorAll('dialog[open], [aria-modal="true"]')].some(node => node.getClientRects().length)) return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (event.repeat) return;
    if (escape || isOpen()) close(); else open();
  };
  trigger.querySelector('kbd').textContent = shortcut;
  trigger.title = `Open AI assistant (${shortcut})`;
  trigger.setAttribute('aria-controls', panel.id);
  trigger.setAttribute('aria-keyshortcuts', 'Control+Shift+Space Meta+Shift+Space');
  collapse.title = `Collapse (${shortcut} or Esc inside assistant)`;
  trigger.onclick = open; collapse.onclick = close;
  window.addEventListener('keydown', keydown, true);
  refresh();
  return {open, close, refresh, destroy:() => window.removeEventListener('keydown', keydown, true)};
}
