function mountPipelineAssistant(root, options) {
  'use strict';
  const {api, canEdit, getLocation, loadDraft} = options;
  const $ = id => root.querySelector(`[id="${id}"]`);
  let epoch = 0, disposed = false, busy = false, capabilities = null, conversation = null, timer;
  const storageKey = () => `pipeline-assistant:${capabilities?.owner}:${getLocation()}`;
  const storage = (key, value) => {
    try {if (value !== undefined) sessionStorage.setItem(key, value); else return sessionStorage.getItem(key);} catch (_) {}
  };
  const status = message => {$('ai-status').textContent = message;};
  function render() {
    $('ai-message').disabled = $('ai-send').disabled = !canEdit || !capabilities?.available || !getLocation() || busy;
    $('ai-new').disabled = busy;
    $('ai-history').replaceChildren();
    for (const item of conversation?.history || []) {
      const text = document.createElement('p'); text.textContent = (item.role === 'user' ? 'You: ' : 'Qwen: ') + item.content;
      $('ai-history').append(text);
    }
    $('ai-history').scrollTop = $('ai-history').scrollHeight;
    const proposal = conversation?.proposal;
    $('ai-proposal').hidden = !proposal;
    $('ai-load').disabled = !canEdit || busy || !proposal;
    $('ai-proposal-summary').textContent = proposal ? [
      proposal.definition.name,
      ...proposal.definition.steps.map((step, index) => `${index + 1}. ${step.operator_id} · ${step.version}`),
      ...(proposal.blocked || []).map(item => `Blocked: ${item.reason}`),
    ].join('\n') : '';
  }
  function fresh() {
    conversation = {conversation_id: crypto.randomUUID(), revision: 0, history: [], proposal: null};
    storage(storageKey(), conversation.conversation_id);
  }
  async function refresh(expected) {
    const updated = await api('/api/ai-assistant/conversations/' + conversation.conversation_id);
    if (disposed || expected !== epoch) return;
    conversation = updated; render();
    return updated;
  }
  async function poll(requestId, expected) {
    if (disposed || expected !== epoch) return;
    try {
      const result = await api('/api/ai-assistant/turns/' + requestId);
      if (disposed || expected !== epoch) return;
      if (result.status === 'running') {timer = setTimeout(() => poll(requestId, expected), 1500); return;}
      await refresh(expected);
      if (disposed || expected !== epoch) return;
      busy = false; render();
      status(result.error || 'Draft only. Review it in the editor before running.');
    } catch (error) {
      if (!disposed && expected === epoch) {busy = false; conversation = null; render(); status(error.message);}
    }
  }
  async function reset() {
    const expected = ++epoch; clearTimeout(timer); busy = false; conversation = null;
    $('ai-message').value = ''; render();
    status('Loading assistant…');
    try {
      const result = await api('/api/ai-assistant/capabilities');
      if (disposed || expected !== epoch) return;
      capabilities = result;
      status(result.reason || (getLocation() ? `${result.model} · ${result.hourly_limit} requests per hour` : 'Choose a dataset first.'));
      render();
      if (!canEdit || !result.available || !getLocation()) return;
      const saved = storage(storageKey());
      if (saved) {
        const restored = await api('/api/ai-assistant/conversations/' + encodeURIComponent(saved));
        if (disposed || expected !== epoch) return;
        if (restored.location_id !== getLocation()) throw new Error('Conversation dataset changed.');
        conversation = restored;
        if (restored.pending_request) {busy = true; status('Qwen is working…'); poll(restored.pending_request, expected);}
      } else fresh();
      render();
    } catch (error) {
      if (!disposed && expected === epoch) {fresh(); render(); status(error.message + ' Start a new chat if the dataset changed.');}
    }
  }
  $('ai-new').addEventListener('click', () => {
    epoch++; clearTimeout(timer); busy = false; fresh(); $('ai-message').value = ''; render();
    status(capabilities?.reason || 'New conversation. Describe the workflow you want.');
  });
  $('ai-send').addEventListener('click', async () => {
    const message = $('ai-message').value.trim();
    if (busy || !canEdit || !capabilities?.available || !getLocation() || !message) return;
    if (!conversation) fresh();
    const expected = epoch, requestId = crypto.randomUUID();
    const body = {request_id: requestId, conversation_id: conversation.conversation_id,
      expected_revision: conversation.revision, location_id: getLocation(), message};
    busy = true; render(); status('Qwen is working…');
    try {
      const result = await api('/api/ai-assistant/turns', 'POST', body);
      if (disposed || expected !== epoch) return;
      if (result.status === 'running') {poll(requestId, expected); return;}
      await refresh(expected);
      if (disposed || expected !== epoch) return;
      busy = false; if (!result.error) $('ai-message').value = ''; render();
      status(result.error || 'Draft only. Review it in the editor before running.');
    } catch (error) {
      if (disposed || expected !== epoch) return;
      status(error.message);
      // A disconnected HTTP response may still have completed on Server A. Recover by request ID.
      await poll(requestId, expected);
    }
  });
  $('ai-load').addEventListener('click', async () => {
    if (!canEdit || busy || !conversation?.proposal) return;
    const expected = epoch;
    try {
      await refresh(expected);
      if (disposed || expected !== epoch || !conversation?.proposal) return;
      await loadDraft(structuredClone(conversation.proposal.definition));
      if (!disposed && expected === epoch) status('Draft loaded. Check parameters, preview, then save a version.');
    } catch (error) {if (!disposed && expected === epoch) status(error.message);}
  });
  return {reset, destroy() {disposed = true; epoch++; clearTimeout(timer);}};
}
