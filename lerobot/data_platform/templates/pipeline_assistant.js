{% include "assistant_controls.js" %}
{% include "assistant_markdown.js" %}
{% include "assistant_personalization.js" %}
const pipelineSystemStep = step => step.operator_id === 'curation.snapshot';
const pipelineStepTitle = step => ({'curation.snapshot': 'Data preparation', 'curation.quality': 'Quality', 'viewer.prepare': 'Viewer'}[step.operator_id] || step.operator_id.split('.').pop().replaceAll('_', ' '));
const pipelineIssueText = issue => issue.code === 'missing_snapshot' ? 'Data preparation is required before this step.' : issue.reason.replace(/curation\.snapshot|Snapshot|snapshot/g, 'data preparation');
function mountPipelineAssistant(root, options) {
  'use strict';
  const {api, canEdit, getLocation, loadDraft} = options;
  const scope = () => options.getContext?.() || {type: 'pipeline', target_id: getLocation()};
  const $ = id => root.querySelector(`[id="${id}"]`);
  let epoch = 0, disposed = false, busy = false, capabilities = null, conversation = null, timer, actionTimer, pendingMessage = '';
  const actionRoot = $('ai-action'), cards = new Map(), expandedCards = new Map();
  const storageKey = () => `pipeline-assistant:${location.origin}:${capabilities?.owner}:${getLocation()}:${scope().type}:${scope().target_id}:${scope().page || ''}`;
  const storage = (key, value) => {
    try {if (value !== undefined) sessionStorage.setItem(key, value); else return sessionStorage.getItem(key);} catch (_) {}
  };
  const status = message => {$('ai-status').textContent = message;};
  const personalControls = mountAssistantPersonalization(root, {
    api, getLocation, getConversation: () => conversation?.revision ? conversation.conversation_id : null,
    navigate: options.navigate,
  });
  let historyCursor = null, historyEpoch = 0, preferencesEpoch = 0, personal = null;
  const navigate = event => {
    const link = event.target.closest('a');
    if (link && options.navigate && !event.ctrlKey && !event.metaKey && !event.shiftKey) {
      event.preventDefault(); options.navigate(link.href);
    }
  };
  $('ai-references').addEventListener('click', navigate);
  $('ai-history').addEventListener('click', navigate);
  async function restore(identity, datasetLabel) {
    const expected = ++epoch; clearTimeout(timer); clearTimeout(actionTimer); busy = false;
    conversation = null; pendingMessage = ''; $('ai-message').value = ''; render();
    try {
      const value = await api('/api/ai-assistant/conversations/' + encodeURIComponent(identity));
      if (disposed || expected !== epoch) return;
      if (options.selectContext && await options.selectContext({...value, dataset:datasetLabel}) === false) return;
      if (value.location_id !== getLocation()) throw new Error('Choose the conversation dataset first.');
      if (disposed || expected !== epoch) return;
      conversation = value; storage(storageKey(), value.conversation_id); render();
      status('Conversation restored. Target remains bound to this conversation.');
      if (value.pending_request) {busy = true; render(); poll(value.pending_request, expected);}
    } catch (error) {if (!disposed && expected === epoch) {render(); status(error.message);}}
  }
  async function listChats(more = false) {
    const expected = epoch, request = ++historyEpoch;
    if (!more) {$('ai-chats').replaceChildren(); historyCursor = null;}
    $('ai-chats-status').textContent = 'Loading…';
    try {
      const result = await api('/api/ai-assistant/conversations?limit=20' + (more && historyCursor ? '&cursor=' + encodeURIComponent(historyCursor) : ''));
      if (disposed || expected !== epoch || request !== historyEpoch) return;
      for (const item of result.conversations) {
        const button = document.createElement('button'); button.type = 'button'; button.style.cssText = 'display:block;width:100%;text-align:left;margin:6px 0;overflow-wrap:anywhere';
        button.textContent = `${item.title} · ${item.dataset || item.context.type} · ${new Date(item.updated_at * 1000).toLocaleString()}`;
        button.dataset.conversationId = item.conversation_id; button.disabled = !item.available; button.title = item.reason || 'Restore conversation';
        button.onclick = () => restore(item.conversation_id, item.dataset); $('ai-chats').append(button);
      }
      historyCursor = result.next_cursor; $('ai-chats-more').hidden = !historyCursor;
      $('ai-chats-status').textContent = $('ai-chats').children.length ? 'Only conversations you can currently access.' : 'No accessible conversations on this page.';
    } catch (error) {if (!disposed && expected === epoch && request === historyEpoch) $('ai-chats-status').textContent = error.message;}
  }
  function renderPreferences() {
    const settings = personal.settings;
    $('ai-pref-enabled').checked = settings.enabled; $('ai-pref-language').value = settings.language;
    $('ai-pref-detail').value = settings.detail;
    for (const option of $('ai-pref-favorites').options) option.selected = settings.favorite_operators.includes(option.value);
  }
  async function loadPreferences() {
    const expected = epoch, request = ++preferencesEpoch;
    $('ai-prefs-save').disabled = $('ai-prefs-clear').disabled = true;
    try {
      const result = await api('/api/ai-assistant/preferences');
      const catalog = await api('/api/operators');
      if (disposed || expected !== epoch || request !== preferencesEpoch) return;
      personal = result; $('ai-pref-favorites').replaceChildren();
      for (const op of catalog.operators.filter(op => op.pipeline_allowed && !pipelineSystemStep(op))) {
        const option = document.createElement('option'); option.value = op.operator_id; option.textContent = op.presentation?.title || op.operator_id; $('ai-pref-favorites').append(option);
      }
      renderPreferences(); $('ai-prefs-save').disabled = $('ai-prefs-clear').disabled = false;
      $('ai-prefs-status').textContent = 'Shared by web and CLI in this environment.';
    } catch (error) {if (!disposed && expected === epoch && request === preferencesEpoch) $('ai-prefs-status').textContent = error.message;}
  }
  async function savePreferences(clear = false) {
    if (!personal || $('ai-prefs-save').disabled) return;
    const expected = epoch, request = ++preferencesEpoch;
    $('ai-prefs-save').disabled = $('ai-prefs-clear').disabled = true;
    const body = {expected_revision: personal.revision};
    if (!clear) body.settings = {enabled:$('ai-pref-enabled').checked, language:$('ai-pref-language').value,
      detail:$('ai-pref-detail').value, favorite_operators:[...$('ai-pref-favorites').selectedOptions].map(o => o.value)};
    try {
      const result = await api('/api/ai-assistant/preferences', clear ? 'DELETE' : 'PUT', body);
      if (disposed || expected !== epoch || request !== preferencesEpoch) return;
      personal = result; renderPreferences(); $('ai-prefs-status').textContent = clear ? 'Preferences cleared.' : 'Preferences saved.';
    } catch (error) {if (!disposed && expected === epoch && request === preferencesEpoch) $('ai-prefs-status').textContent = error.message + ' Close and reopen Preferences to reload.';}
    finally {if (!disposed && expected === epoch && request === preferencesEpoch) $('ai-prefs-save').disabled = $('ai-prefs-clear').disabled = false;}
  }
  $('ai-chats-toggle').onclick = () => {const open = $('ai-chats-panel').hidden; $('ai-chats-panel').hidden = !open; $('ai-chats-toggle').setAttribute('aria-expanded', String(open)); if (open) listChats();};
  $('ai-chats-more').onclick = () => listChats(true);
  $('ai-prefs-toggle').onclick = () => {const open = $('ai-prefs-panel').hidden; $('ai-prefs-panel').hidden = !open; $('ai-prefs-toggle').setAttribute('aria-expanded', String(open)); if (open) loadPreferences();};
  $('ai-prefs-save').onclick = () => savePreferences(); $('ai-prefs-clear').onclick = () => savePreferences(true);
  function render() {
    personalControls.showConversation();
    $('ai-message').disabled = $('ai-send').disabled = !capabilities?.available || !getLocation() || busy;
    $('ai-new').disabled = busy;
    actionRoot.remove(); cards.clear(); $('ai-history').replaceChildren();
    const history = [...(conversation?.history || [])];
    if (pendingMessage) history.push({role:'user', content:pendingMessage});
    if (busy) history.push({role:'assistant', content:'Working…'});
    for (const item of history) {
      const text = document.createElement('div'); text.className = 'assistant-message ' + (item.role === 'user' ? 'user' : 'assistant');
      const label = document.createElement('small'); label.textContent = item.role === 'user' ? 'You' : 'Assistant';
      text.append(label, item.role === 'user' ? document.createTextNode(item.content) : assistantFormattedText(item.content)); $('ai-history').append(text);
      if (item.action) {
        const latest = item.action.action_id === conversation?.action?.action_id;
        const action = latest ? conversation.action : item.action;
        const box = latest ? actionRoot : document.createElement('div');
        box.className = 'assistant-action'; box.tabIndex = -1; box.setAttribute('role', 'region'); box.setAttribute('aria-label', 'Prepared operation');
        box.dataset.actionId = action.action_id; text.append(box);
        cards.set(action.action_id, {box, action, item, observation: latest ? conversation.operation : item.operation});
      }
    }
    renderAction();
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
  function renderAction() {
    clearTimeout(actionTimer);
    const running = [];
    for (const [id, card] of cards) {
      const {box, action} = card, observation = card.observation;
      box.replaceChildren(); box.hidden = false;
      const state = observation?.state || (action.execution ? 'running' : 'new');
      const active = state === 'running';
      if (active) running.push(id);
      const title = document.createElement('strong'); title.textContent = action.title;
      const badge = document.createElement('small'); badge.textContent = active ? `Task ${observation?.status || 'running'}` : ({ready:'Result available',history:'Previous result',failed:'Previous attempt failed'}[state] || (action.is_current === false ? 'Earlier request' : 'Awaiting confirmation'));
      box.append(title, badge);
      for (const ref of observation?.links || []) {
        const link = document.createElement('a'); link.href = ref.url; link.textContent = ref.title; link.className = 'assistant-primary-link'; box.append(link);
      }
      const compact = state !== 'new' || action.is_current === false || !!action.execution;
      let body = box;
      if (compact) {
        body = document.createElement('details'); body.open = expandedCards.get(id) || false;
        const summary = document.createElement('summary'); summary.textContent = 'Operation details'; body.append(summary);
        body.ontoggle = () => {if (body.isConnected) expandedCards.set(id, body.open);}; box.append(body);
      }
      const target = document.createElement('p'); target.textContent = `Dataset: ${action.dataset} · Node: ${action.node}`;
      const note = document.createElement('p'); note.textContent = observation?.message || 'Review parameters before confirming.'; body.append(target, note);
      if (observation?.updated_at) {const time = document.createElement('small'); time.textContent = `Updated: ${observation.updated_at}`; body.append(time);}
      if (observation?.coverage_notice) {const coverage = document.createElement('p'); coverage.textContent = observation.coverage_notice; body.append(coverage);}
      const parameters = document.createElement('pre'); parameters.textContent = Object.keys(action.parameters).length ? JSON.stringify(action.parameters, null, 2) : 'Parameters: defaults';
      let controls = body;
      if (!active && !action.execution && action.is_current !== false && observation?.requires_rerun) {
        const details = document.createElement('details');
        const summary = document.createElement('summary'); summary.textContent = action.operator_id === 'viewer.prepare' ? 'Rebuild Viewer' : 'Run again'; details.append(summary); body.append(details); controls = details;
      }
      controls.append(parameters);
      if (!active && !action.execution && action.is_current !== false) {
        const issue = document.createElement('p'); issue.textContent = (action.blocked || []).map(pipelineIssueText).join(' · ');
        const button = document.createElement('button'); button.textContent = 'Confirm and run';
        button.disabled = busy || !capabilities?.can_edit || !action.can_execute || observation?.can_confirm === false;
        button.onclick = () => confirmAction(action, !!observation?.requires_rerun); controls.append(issue, button);
      }
      if (action.execution?.url && !observation?.links?.some(ref => ref.url === action.execution.url)) {const link = document.createElement('a'); link.href = action.execution.url; link.textContent = 'Open run'; body.append(link);}
      if (observation?.jobs_url && !observation?.links?.some(ref => ref.url === observation.jobs_url)) {const link = document.createElement('a'); link.href = observation.jobs_url; link.textContent = 'Open Jobs'; body.append(link);}
      const adjust = document.createElement('button'); adjust.textContent = action.is_current === false || action.execution ? 'Prepare another run' : 'Change settings in chat'; adjust.disabled = busy || !capabilities?.can_edit;
      adjust.onclick = () => {$('ai-message').value = `Prepare ${action.operator_id} with these changes: `; $('ai-message').focus();}; body.append(adjust);
      const refresh = document.createElement('button'); refresh.textContent = 'Refresh status'; refresh.disabled = busy; refresh.onclick = () => pollAction(id, epoch); body.append(refresh);
    }
    if (!actionRoot.isConnected) {actionRoot.hidden = true; $('ai-history').append(actionRoot);}
    if (running.length) actionTimer = setTimeout(async () => {const expected = epoch; for (const id of running) await pollAction(id, expected);}, 2000);
  }
  async function pollAction(actionId, expected) {
    if (disposed || epoch !== expected || !cards.has(actionId)) return;
    try {
      const receipt = await api('/api/ai-assistant/actions/' + encodeURIComponent(actionId));
      if (disposed || epoch !== expected || !cards.has(actionId)) return;
      const card = cards.get(actionId); card.observation = card.item.operation = receipt;
      if (conversation.action?.action_id === actionId) conversation.operation = receipt;
      if (card.action.execution && receipt.run_id) card.action.execution = receipt;
      renderAction();
    } catch (_) {
      if (!disposed && epoch === expected && cards.has(actionId)) {
        const notice = document.createElement('small'); notice.textContent = 'Status unavailable. Refresh status or check Jobs.'; cards.get(actionId).box.append(notice);
      }
    }
  }
  async function confirmAction(action, rerun = false) {
    if (busy || !capabilities?.can_edit) return;
    const expected = epoch; busy = true; render(); status('Submitting operation…');
    try {
      const receipt = await api('/api/ai-assistant/actions/' + encodeURIComponent(action.action_id) + '/confirm', 'POST', {revision:action.revision, rerun});
      if (disposed || expected !== epoch) return;
      if (receipt.run_id) action.execution = receipt;
      conversation.operation = receipt.state ? receipt : {...receipt, state:'running'};
      const item = conversation.history.find(item => item.action?.action_id === action.action_id); if (item) {item.action = action; item.operation = conversation.operation;}
      busy = false; render(); status(receipt.reused ? 'Existing task found. Follow progress here or in Jobs.' : 'Task submitted. Follow progress here or in Jobs.');
      personalControls.refreshUpdates();
      pollAction(action.action_id, expected);
    } catch (error) {
      if (disposed || expected !== epoch) return;
      busy = false; render(); status(error.message + ' Refresh status before retrying.');
      pollAction(action.action_id, expected);
    }
  }
  function fresh() {
    conversation = {conversation_id: crypto.randomUUID(), revision: 0, history: [], proposal: null};
    storage(storageKey(), conversation.conversation_id);
  }
  async function refresh(expected) {
    const updated = await api('/api/ai-assistant/conversations/' + conversation.conversation_id);
    if (disposed || expected !== epoch) return;
    conversation = updated; pendingMessage = ''; render();
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
      busy = false; pendingMessage = ''; if (!result.error) $('ai-message').value = ''; render();
      if (result.action && !result.error) {
        $('ai-action').scrollIntoView({block:'nearest'}); $('ai-action').focus({preventScroll:true});
      } else $('ai-message').focus();
      status(result.error || (result.security ? 'Request restricted. See the assistant response.' : result.proposal ? 'Draft only. Review it in the editor before running.' : result.action ? (conversation.operation?.message || 'Review the operation card below, then confirm to run.') : 'Response ready.'));
    } catch (error) {
      if (!disposed && expected === epoch) {busy = false; pendingMessage = ''; conversation = null; render(); status(error.message);}
    }
  }
  async function reset() {
    personalControls.reset();
    const expected = ++epoch; clearTimeout(timer); clearTimeout(actionTimer); busy = false; conversation = null; capabilities = null;
    pendingMessage = ''; $('ai-message').value = ''; $('ai-chats-panel').hidden = $('ai-prefs-panel').hidden = true; $('ai-chats-toggle').setAttribute('aria-expanded', 'false'); $('ai-prefs-toggle').setAttribute('aria-expanded', 'false'); render();
    status('Loading assistant…');
    try {
      const result = await api('/api/ai-assistant/capabilities');
      if (disposed || expected !== epoch) return;
      capabilities = result;
      status(result.reason || (getLocation() ? `${result.model}${result.hourly_limit == null ? '' : ` · ${result.hourly_limit} requests per hour`}${result.can_edit ? '' : ' · Read-only explanations'}` : 'Choose a dataset first.'));
      render();
      if (!getLocation()) return;
      const saved = options.conversationId?.() || new URLSearchParams(location.search).get('assistant_conversation') || storage(storageKey());
      if (saved) {
        const restored = await api('/api/ai-assistant/conversations/' + encodeURIComponent(saved));
        if (disposed || expected !== epoch) return;
        if (restored.location_id !== getLocation() || ((restored.context || scope()).type !== scope().type || (restored.context || scope()).target_id !== scope().target_id || ((restored.context || scope()).page || '') !== (scope().page || ''))) throw new Error('Conversation context changed.');
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
    epoch++; clearTimeout(timer); clearTimeout(actionTimer); busy = false; fresh(); $('ai-message').value = ''; render();
    status(capabilities?.reason || 'New conversation. Describe the workflow you want.');
  });
  $('ai-message').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault(); if (!event.repeat) $('ai-send').click();
    }
  });
  $('ai-message').addEventListener('input', () => {
    $('ai-message').style.height = 'auto';
    $('ai-message').style.height = Math.min(200, $('ai-message').scrollHeight) + 'px';
  });
  $('ai-send').addEventListener('click', async () => {
    const message = $('ai-message').value.trim();
    if (busy || !capabilities?.available || !getLocation() || !message) return;
    if (!conversation) fresh();
    const expected = epoch, requestId = crypto.randomUUID();
    const body = {request_id: requestId, conversation_id: conversation.conversation_id,
      expected_revision: conversation.revision, location_id: getLocation(), context: conversation.context || scope(), message};
    const editorDraft = options.getDraft?.(); if (capabilities?.can_edit && editorDraft?.steps.length) body.draft = structuredClone(editorDraft);
    pendingMessage = message; busy = true; render(); status('Qwen is working…');
    try {
      const result = await api('/api/ai-assistant/turns', 'POST', body);
      if (disposed || expected !== epoch) return;
      if (result.status === 'running') {poll(requestId, expected); return;}
      await refresh(expected);
      if (disposed || expected !== epoch) return;
      busy = false; pendingMessage = ''; if (!result.error) $('ai-message').value = ''; render();
      if (result.action && !result.error) {
        $('ai-action').scrollIntoView({block:'nearest'}); $('ai-action').focus({preventScroll:true});
      } else $('ai-message').focus();
      status(result.error || (result.security ? 'Request restricted. See the assistant response.' : result.proposal ? 'Draft only. Review it in the editor before running.' : result.action ? (conversation.operation?.message || 'Review the operation card below, then confirm to run.') : 'Response ready.'));
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
  return {reset, restore, destroy() {personalControls.destroy(); disposed = true; epoch++; clearTimeout(timer); clearTimeout(actionTimer);}};
}
