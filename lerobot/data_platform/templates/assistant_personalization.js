function mountAssistantPersonalization(root, {api, getLocation, getConversation, navigate}) {
  const $ = id => root.querySelector(`[id="${id}"]`);
  let generation = 0, memoryRequest = 0, updateRequest = 0, disposed = false;
  let profile = null, editing = null, saving = false, updateTimer, activeConversation, allUpdates = false;
  const buttons = ['ai-memory-save', 'ai-memory-clear', 'ai-memory-settings'];
  const lock = () => buttons.forEach(id => {$(id).disabled = saving || !profile;});
  function cancelEdit() {editing = null; $('ai-memory-text').value = ''; $('ai-memory-cancel').hidden = true;}
  function renderMemory() {
    $('ai-memory-enabled').checked = profile.settings.enabled;
    $('ai-followups-enabled').checked = profile.settings.followups_enabled;
    $('ai-memory-list').replaceChildren();
    for (const note of profile.notes) {
      const row = document.createElement('div'); row.className = 'assistant-message'; row.dataset.memoryId = note.memory_id;
      const text = document.createElement('p'); text.textContent = `${note.scope === 'account' ? 'Account' : 'Dataset'} · ${note.text}`;
      const edit = document.createElement('button'); edit.textContent = 'Edit'; edit.disabled = saving;
      edit.onclick = () => {
        if (note.scope === 'dataset' && note.location_id !== getLocation()) {
          $('ai-memory-status').textContent = 'Select the saved dataset before editing this note.'; return;
        }
        editing = note.memory_id; $('ai-memory-text').value = note.text; $('ai-memory-scope').value = note.scope;
        $('ai-memory-cancel').hidden = false; $('ai-memory-text').focus();
      };
      const remove = document.createElement('button'); remove.textContent = 'Delete'; remove.disabled = saving;
      remove.onclick = () => change({operation:'delete', memory_id:note.memory_id});
      row.append(text, edit, remove); $('ai-memory-list').append(row);
    }
    lock();
  }
  async function load() {
    const expected = generation, request = ++memoryRequest; profile = null; lock();
    $('ai-memory-list').replaceChildren(); $('ai-memory-status').textContent = 'Loading…';
    try {
      const value = await api('/api/ai-assistant/memory');
      if (disposed || expected !== generation || request !== memoryRequest) return;
      profile = value; renderMemory(); $('ai-memory-status').textContent = value.notes.length ? '' : 'No saved notes.';
    } catch (error) {if (!disposed && expected === generation && request === memoryRequest) $('ai-memory-status').textContent = error.message;}
  }
  async function change(value) {
    if (!profile || saving) return;
    const expected = generation, request = ++memoryRequest; saving = true; lock();
    try {
      const result = await api('/api/ai-assistant/memory', 'PUT', {...value, expected_revision:profile.revision});
      if (disposed || expected !== generation || request !== memoryRequest) return;
      profile = result; cancelEdit(); renderMemory(); $('ai-memory-status').textContent = 'Saved. Applies to future messages.';
      refreshUpdates();
    } catch (error) {
      if (!disposed && expected === generation && request === memoryRequest) {
        profile = null; $('ai-memory-status').textContent = error.message + ' Reopen Memory to reload.';
      }
    } finally {if (!disposed && expected === generation) {saving = false; if (profile) renderMemory(); else lock();}}
  }
  $('ai-memory-toggle').onclick = () => {
    const open = $('ai-memory-panel').hidden; $('ai-memory-panel').hidden = !open;
    $('ai-memory-toggle').setAttribute('aria-expanded', String(open)); if (open) {cancelEdit(); load();}
  };
  $('ai-memory-cancel').onclick = cancelEdit;
  $('ai-memory-save').onclick = () => change({operation:'save', ...(editing ? {memory_id:editing} : {}),
    text:$('ai-memory-text').value, scope:$('ai-memory-scope').value,
    location_id:$('ai-memory-scope').value === 'dataset' ? getLocation() : null});
  $('ai-memory-clear').onclick = () => change({operation:'clear'});
  $('ai-memory-settings').onclick = () => change({operation:'settings', settings:{
    enabled:$('ai-memory-enabled').checked, followups_enabled:$('ai-followups-enabled').checked}});
  async function refreshUpdates() {
    clearTimeout(updateTimer);
    const expected = generation, request = ++updateRequest, identity = getConversation();
    if (!allUpdates && !identity) {$('ai-followups').replaceChildren(); $('ai-followups').hidden = true; return;}
    try {
      const value = await api('/api/ai-assistant/followups' + (allUpdates ? '' : '?conversation_id=' + encodeURIComponent(identity)));
      if (disposed || generation !== expected || request !== updateRequest || identity !== getConversation()) return;
      const panel = $('ai-followups'); panel.replaceChildren(); panel.hidden = !value.updates.length && !allUpdates;
      for (const item of value.updates) {
        const card = document.createElement('div'); card.className = 'assistant-message'; card.dataset.updateId = item.update_id;
        const label = document.createElement('small'); label.textContent = 'Task update · ' + new Date(item.completed_at * 1000).toLocaleString();
        const text = document.createElement('p'); text.textContent = item.message; card.append(label, text);
        for (const ref of item.links) {
          const url = new URL(ref.url, location.origin); if (url.origin !== location.origin) continue;
          const link = document.createElement('a'); link.href = url.href; link.textContent = ref.title; link.style.marginRight = '12px';
          if (navigate) link.onclick = event => {if (!event.ctrlKey && !event.metaKey && !event.shiftKey) {event.preventDefault(); navigate(link.href);}};
          card.append(link);
        }
        panel.append(card);
      }
      if (allUpdates && !value.updates.length) panel.textContent = value.pending ? 'Following confirmed tasks…' : 'No task updates.';
      if (value.pending) updateTimer = setTimeout(refreshUpdates, 5000);
    } catch (error) {
      if (!disposed && generation === expected && request === updateRequest) {
        $('ai-followups').replaceChildren(); $('ai-followups').hidden = !allUpdates;
        if (allUpdates) $('ai-followups').textContent = error.message;
        updateTimer = setTimeout(refreshUpdates, 10000);
      }
    }
  }
  $('ai-updates-toggle').onclick = () => {allUpdates = !allUpdates; $('ai-updates-toggle').setAttribute('aria-expanded', String(allUpdates)); refreshUpdates();};
  lock();
  return {
    refreshUpdates,
    showConversation() {const identity = getConversation(); if (identity !== activeConversation) {activeConversation = identity; refreshUpdates();}},
    reset() {
      generation++; memoryRequest++; updateRequest++; clearTimeout(updateTimer); profile = null; saving = false;
      activeConversation = undefined; allUpdates = false; cancelEdit(); lock();
      $('ai-memory-panel').hidden = true; $('ai-memory-list').replaceChildren(); $('ai-followups').replaceChildren(); $('ai-followups').hidden = true;
      $('ai-memory-toggle').setAttribute('aria-expanded', 'false'); $('ai-updates-toggle').setAttribute('aria-expanded', 'false');
    },
    destroy() {disposed = true; generation++; clearTimeout(updateTimer);},
  };
}
