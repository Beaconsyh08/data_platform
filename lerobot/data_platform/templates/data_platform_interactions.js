// Keep modal keyboard focus and return the user to the control that opened it.
window.syncConsoleDialog = (() => {
    const dialogs = new WeakMap();
    return (root, open) => {
        let state = dialogs.get(root);
        if (!open) {
            if (!state) return;
            root.removeEventListener('keydown', state.trap);
            document.body.style.overflow = state.overflow;
            if (state.trigger?.isConnected) state.trigger.focus({preventScroll: true});
            dialogs.delete(root);
            return;
        }
        if (state) return;
        const panel = root.querySelector('[role=dialog]');
        const controls = () => [...panel.querySelectorAll('button, a[href], input, select, textarea, [tabindex="0"]')]
            .filter(node => !node.disabled && node.getClientRects().length);
        state = {trigger: document.activeElement, overflow: document.body.style.overflow};
        state.trap = event => {
            if (event.key !== 'Tab') return;
            const items = controls(), first = items[0], last = items[items.length - 1];
            if (!first) { event.preventDefault(); panel.focus(); return; }
            if (event.shiftKey && (document.activeElement === first || !items.includes(document.activeElement))) {
                event.preventDefault(); last.focus();
            } else if (!event.shiftKey && (document.activeElement === last || !items.includes(document.activeElement))) {
                event.preventDefault(); first.focus();
            }
        };
        dialogs.set(root, state);
        document.body.style.overflow = 'hidden';
        root.addEventListener('keydown', state.trap);
        requestAnimationFrame(() => {
            if (dialogs.get(root) === state) (controls()[0] || panel).focus();
        });
    };
})();
