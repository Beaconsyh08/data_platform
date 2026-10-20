const pipelineSystemStep = step => step.operator_id === 'curation.snapshot';
const pipelineStepTitle = step => ({'curation.snapshot': 'Data preparation', 'curation.quality': 'Quality', 'viewer.prepare': 'Viewer'}[step.operator_id] || step.operator_id.split('.').pop().replaceAll('_', ' '));
const pipelineIssueText = issue => issue.code === 'missing_snapshot' ? 'Data preparation is required before this step.' : issue.reason.replace(/curation\.snapshot|Snapshot|snapshot/g, 'data preparation');
function mountPipelineAssistant(root, options) {
  'use strict';
  const {api, canEdit, getLocation, loadDraft} = options;
  const scope = () => options.getContext?.() || {type: 'pipeline', target_id: getLocation()};
  const $ = id => root.querySelector(`[id="${id}"]`);
  let epoch = 0, disposed = false, busy = false, capabilities = null, conversation = null, timer;
  const storageKey = () => `pipeline-assistant:${location.origin}:${capabilities?.owner}:${getLocation()}:${scope().type}:${scope().target_id}`;
  const storage = (key, value) => {
    try {if (value !== undefined) sessionStorage.setItem(key, value); else return sessionStorage.getItem(key);} catch (_) {}
  };
  const status = message => {$('ai-status').textContent = message;};
  function render() {
    $('ai-message').disabled = $('ai-send').disabled = !capabilities?.available || !getLocation() || busy;
    $('ai-new').disabled = busy;
    $('ai-history').replaceChildren();
    for (const item of conversation?.history || []) {
      const text = document.createElement('p'); text.textContent = (item.role === 'user' ? 'You: ' : 'Qwen: ') + item.content;
      $('ai-history').append(text);
    }
    $('ai-history').scrollTop = $('ai-history').scrollHeight;
    const proposal = conversation?.proposal;
    $('ai-proposal').hidden = !proposal;
    $('ai-load').disabled = !canEdit || !capabilities?.can_edit || busy || !proposal;
    $('ai-undo').hidden = !options.undoDraft;
    $('ai-undo').disabled = busy || !canEdit;
    $('ai-validation').textContent = proposal ? (proposal.can_execute ? 'Validated draft' : 'Blocked draft · not ready to run') : '';
    $('ai-references').replaceChildren();
    for (const item of conversation?.references || []) {const link = document.createElement('a'); link.textContent = item.title; link.href = item.url; $('ai-references').append(link);}
    $('ai-proposal-summary').replaceChildren();
    for (const [index, step] of (proposal?.definition.steps || []).filter(step => !pipelineSystemStep(step)).entries()) {
      const node = document.createElement('div'); node.textContent = `${index + 1}. ${pipelineStepTitle(step)}`;
      if ((proposal.added_steps || []).includes(step.step_id)) node.append(document.createTextNode(' · AI added'));
      for (const issue of (proposal.blocked || []).filter(item => item.step_id === step.step_id)) {
        const text = document.createElement('p'); text.className = 'error'; text.textContent = `Step ${index + 1} · ${pipelineStepTitle(step)}: ${pipelineIssueText(issue)}`; node.append(text);
      }
      $('ai-proposal-summary').append(node);
    }
    for (const issue of proposal?.blocked || []) {
      if (!(proposal.definition.steps || []).some(step => pipelineSystemStep(step) && step.step_id === issue.step_id)) continue;
      const text = document.createElement('p'); text.className = 'error'; text.textContent = 'Data preparation: ' + pipelineIssueText(issue); $('ai-proposal-summary').append(text);
    }
    for (const item of conversation?.suggestions || []) {
      if (item.type !== 'prepare_draft' || !capabilities?.can_edit) continue;
      const button = document.createElement('button'); button.textContent = item.title; button.disabled = busy;
      button.onclick = () => {$('ai-message').value = item.message; $('ai-message').focus();}; $('ai-references').append(button);
    }
    const current = options.getDraft?.();
    const changes = [];
    if (proposal && current?.steps.length) {
      const nextSteps = proposal.definition.steps.filter(step => !pipelineSystemStep(step)), oldSteps = current.steps.filter(step => !pipelineSystemStep(step));
      const next = new Map(nextSteps.map(step => [step.step_id, step]));
      const old = new Map(oldSteps.map(step => [step.step_id, step]));
      for (const step of nextSteps) {
        if (!old.has(step.step_id)) changes.push(`+ ${pipelineStepTitle(step)}`);
        else if (JSON.stringify(step) !== JSON.stringify(old.get(step.step_id))) changes.push(`Change ${step.step_id}: ${JSON.stringify(step.parameters)} · input ${step.input || 'source'}`);
      }
      for (const step of oldSteps) if (!next.has(step.step_id)) changes.push(`− ${pipelineStepTitle(step)}`);
      if (oldSteps.map(s => s.step_id).join() !== nextSteps.map(s => s.step_id).join()) changes.push('Step order changes');
    }
    $('ai-diff').textContent = changes.length ? 'Changes when applied:\n' + changes.join('\n') : '';

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
      status(result.error || (result.security ? 'Request restricted. See the assistant response.' : result.proposal ? 'Draft only. Review it in the editor before running.' : 'Response ready.'));
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
      status(result.reason || (getLocation() ? `${result.model} · ${result.hourly_limit} requests per hour${result.can_edit ? '' : ' · Read-only explanations'}` : 'Choose a dataset first.'));
      render();
      if (!result.available || !getLocation()) return;
      const saved = new URLSearchParams(location.search).get('assistant_conversation') || storage(storageKey());
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
    const url = new URL(location.href); url.searchParams.delete('assistant_conversation'); history.replaceState(null, '', url);
    epoch++; clearTimeout(timer); busy = false; fresh(); $('ai-message').value = ''; render();
    status(capabilities?.reason || 'New conversation. Describe the workflow you want.');
  });
  $('ai-send').addEventListener('click', async () => {
    const message = $('ai-message').value.trim();
    if (busy || !capabilities?.available || !getLocation() || !message) return;
    if (!conversation) fresh();
    const expected = epoch, requestId = crypto.randomUUID();
    const body = {request_id: requestId, conversation_id: conversation.conversation_id,
      expected_revision: conversation.revision, location_id: getLocation(), context: conversation.context || scope(), message};
    const editorDraft = options.getDraft?.(); if (capabilities?.can_edit && editorDraft?.steps.length) body.draft = structuredClone(editorDraft);
    busy = true; render(); status('Qwen is working…');
    try {
      const result = await api('/api/ai-assistant/turns', 'POST', body);
      if (disposed || expected !== epoch) return;
      if (result.status === 'running') {poll(requestId, expected); return;}
      await refresh(expected);
      if (disposed || expected !== epoch) return;
      busy = false; if (!result.error) $('ai-message').value = ''; render();
      status(result.error || (result.security ? 'Request restricted. See the assistant response.' : result.proposal ? 'Draft only. Review it in the editor before running.' : 'Response ready.'));
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
      await loadDraft(structuredClone(conversation.proposal.definition), conversation.proposal.added_steps || [], conversation.conversation_id);
      if (!disposed && expected === epoch) status('Draft loaded. Check parameters, preview, then save a version.');
    } catch (error) {if (!disposed && expected === epoch) status(error.message);}
  });
  $('ai-undo').addEventListener('click', () => {options.undoDraft?.(); render();});
  return {reset, destroy() {disposed = true; epoch++; clearTimeout(timer);}};
}
