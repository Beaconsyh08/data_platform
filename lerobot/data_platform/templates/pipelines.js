(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const canEdit = document.querySelector('main').dataset.canEdit === 'true';
  const state = {operators: [], steps: [], pipeline: null, run: null, preview: null, epoch: 0, edit: 0, invalid: new Set(), dirty: false};
  let timer;
  const el = (tag, text, attrs = {}) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    Object.assign(node, attrs);
    return node;
  };
  const feedback = (message, error = false) => {
    $('feedback').textContent = message;
    $('feedback').className = error ? 'error' : 'ok';
  };
  const api = async (path, method = 'GET', value, headers = {}) => {
    const response = await fetch(path, {method, credentials: 'same-origin', headers: {'Content-Type': 'application/json', ...headers}, body: value === undefined ? undefined : JSON.stringify(value)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
    return result;
  };
  const definition = () => ({name: $('name').value, steps: state.steps});
  const operator = step => state.operators.find(value => value.operator_id === step.operator_id);
  const invalidate = () => {state.edit++; state.preview = null; state.dirty = true; $('run').disabled = true; $('preview-panel').hidden = true;};
  const guard = fn => async (...args) => {
    try {await fn(...args);} catch (error) {feedback(error.message, true);}
  };
  const button = (text, action, disabled = false) => {
    const value = el('button', text, {type: 'button', disabled: !canEdit || disabled});
    value.addEventListener('click', guard(action));
    return value;
  };
  function render() {
    const root = $('steps'); root.replaceChildren();
    state.steps.forEach((step, index) => {
      const card = el('article');
      const header = el('div', undefined, {className: 'row step-title'});
      header.append(el('strong', `${index + 1}. ${step.step_id}`));
      const actions = el('div', undefined, {className: 'row'});
      actions.append(button('↑', () => move(index, -1), index === 0), button('↓', () => move(index, 1), index === state.steps.length - 1), button('Remove', () => {state.steps.splice(index, 1); state.invalid = new Set([...state.invalid].filter(key => !key.startsWith(step.step_id + ':'))); invalidate(); render();}));
      header.append(actions); card.append(header);
      const selection = el('select', undefined, {disabled: !canEdit, 'ariaLabel': 'Operator'});
      state.operators.filter(value => value.pipeline_allowed || value.operator_id === step.operator_id).forEach(value => {
        const option = el('option', value.operator_id + (value.reason ? ` · ${value.reason}` : ''), {value: value.operator_id, disabled: !value.available});
        selection.append(option);
      });
      selection.value = step.operator_id;
      selection.addEventListener('change', () => {
        const chosen = state.operators.find(value => value.operator_id === selection.value);
        step.operator_id = chosen.operator_id; step.version = chosen.version;
        step.parameters = {}; step.review_before = !!chosen.review_required;
        invalidate(); render();
      });
      card.append(selection);
      const spec = operator(step);
      card.append(el('p', spec?.description || 'Operator version unavailable', {className: 'muted'}));
      const inputs = el('div', undefined, {className: 'row'});
      const sourceLabel = el('label', 'Dataset input');
      const source = el('select', undefined, {disabled: !canEdit, ariaLabel: 'Dataset input'});
      source.append(el('option', 'Original dataset', {value: 'source'}));
      state.steps.slice(0, index).filter(item => operator(item)?.output_type === 'dataset').forEach(item => source.append(el('option', `${item.step_id} output`, {value: item.step_id})));
      if (!Array.from(source.options).some(item => item.value === step.input)) source.append(el('option', `${step.input} · invalid reference`, {value: step.input, disabled: true}));
      source.value = step.input || 'source';
      source.addEventListener('change', () => {step.input = source.value; invalidate();});
      sourceLabel.append(source); inputs.append(sourceLabel);
      const review = el('input', undefined, {type: 'checkbox', checked: !!step.review_before, disabled: !canEdit || !!spec?.review_required});
      review.addEventListener('change', () => {step.review_before = review.checked; invalidate();});
      const reviewLabel = el('label', 'Review before this step'); reviewLabel.append(review); inputs.append(reviewLabel);
      card.append(inputs);
      if (index) {
        const dependencies = el('div', 'Evidence: ', {className: 'row'});
        state.steps.slice(0, index).forEach(item => {
          const checked = (step.artifacts || []).includes(item.step_id);
          const box = el('input', undefined, {type: 'checkbox', checked, disabled: !canEdit});
          box.addEventListener('change', () => {step.artifacts = (step.artifacts || []).filter(value => value !== item.step_id); if (box.checked) step.artifacts.push(item.step_id); invalidate();});
          const label = el('label', item.step_id); label.append(box); dependencies.append(label);
        });
        card.append(dependencies);
      }
      const fields = el('div', undefined, {className: 'parameters'});
      Object.entries(spec?.parameters.properties || {}).forEach(([key, schema]) => {
        const label = el('label', key);
        const types = Array.isArray(schema.type) ? schema.type : [schema.type];
        const kind = types.includes('string') ? 'string' : types.find(value => value !== 'null');
        const stored = Object.hasOwn(step.parameters, key) ? step.parameters[key] : schema.default;
        const jsonValue = !kind || ['array', 'object'].includes(kind);
        let control;
        if (jsonValue) control = el('textarea', undefined, {value: stored === undefined ? '' : JSON.stringify(stored), disabled: !canEdit});
        else control = el('input', undefined, {type: kind === 'boolean' ? 'checkbox' : ['number', 'integer'].includes(kind) ? 'number' : 'text', disabled: !canEdit});
        if (!jsonValue) {
          if (kind === 'boolean') control.checked = !!stored;
          else control.value = stored ?? '';
        }
        control.addEventListener('input', () => {invalidate();});
        control.addEventListener('change', guard(() => {
          invalidate(); state.invalid.delete(step.step_id + ':' + key);
          let value;
          if (kind === 'boolean') value = control.checked;
          else if (!control.value.trim()) {delete step.parameters[key]; invalidate(); return;}
          else if (jsonValue) {try {value = JSON.parse(control.value);} catch (error) {state.invalid.add(step.step_id + ':' + key); throw error;}}
          else if (['number', 'integer'].includes(kind)) value = Number(control.value);
          else value = control.value;
          step.parameters[key] = value; invalidate();
        }));
        label.append(control); fields.append(label);
      });
      card.append(fields);
      const advanced = el('details'); advanced.append(el('summary', 'Input and review options'));
      const options = el('textarea', undefined, {value: JSON.stringify(step.options || {}), disabled: !canEdit});
      options.setAttribute('aria-label', 'Registered Merge inputs, workspace or manifest options');
      options.addEventListener('input', invalidate);
      options.addEventListener('change', guard(() => {invalidate(); state.invalid.delete(step.step_id + ':options'); try {step.options = JSON.parse(options.value);} catch (error) {state.invalid.add(step.step_id + ':options'); throw error;}}));
      advanced.append(options); card.append(advanced); root.append(card);
    });
  }
  function move(index, delta) {const other = index + delta; [state.steps[index], state.steps[other]] = [state.steps[other], state.steps[index]]; invalidate(); render();}
  async function loadOperators() {
    const epoch = ++state.epoch;
    const data = await api('/api/operators?location_id=' + encodeURIComponent($('location').value));
    if (epoch !== state.epoch) return;
    state.operators = data.operators; $('add').disabled = !canEdit || !state.operators.some(value => value.available && value.pipeline_allowed); render();
    $('review-link').href = '/curation?location_id=' + encodeURIComponent($('location').value);
  }
  async function loadSaved() {
    const data = await api('/api/pipelines');
    const selected = $('saved').value;
    $('saved').replaceChildren(el('option', 'New pipeline', {value: ''}));
    data.pipelines.forEach(item => $('saved').append(el('option', `${item.name} · v${item.revision}`, {value: item.pipeline_id})));
    $('saved').value = selected;
  }
  async function preview() {
    if (state.invalid.size) throw new Error('Correct the invalid parameter fields.');
    const epoch = state.epoch, edit = state.edit;
    const result = await api('/api/processing/preview', 'POST', {location_id: $('location').value, definition: definition()});
    if (epoch !== state.epoch) return;
    if (edit !== state.edit) return;
    state.preview = result;
    $('preview-panel').hidden = false;
    $('preview-result').textContent = result.blocked.length ? result.blocked.map(item => `${item.step_id}: ${item.reason}`).join('\n') : result.definition.steps.map(item => `${item.step_id}: ${item.operator_id}@${item.version} · input ${item.input}${item.review_before ? ' · review required' : ''}`).join('\n');
    $('run').disabled = !canEdit || !result.can_execute || !state.pipeline || state.dirty;
    feedback(result.can_execute ? 'Plan is ready.' : 'Resolve the blocked steps before running.', !result.can_execute);
  }
  async function poll(runId, epoch) {
    if (document.hidden) {timer = setTimeout(() => poll(runId, epoch), 3000); return;}
    try {
      const value = await api('/api/pipeline-runs/' + runId);
      if (epoch !== state.epoch || state.run?.run_id !== runId) return;
      state.run = value; renderRun();
      if (['queued', 'running', 'waiting_review', 'cancel_requested', 'retry_requested'].includes(value.status)) timer = setTimeout(() => poll(runId, epoch), 3000);
    } catch (error) {if (epoch === state.epoch && state.run?.run_id === runId) {feedback(error.message, true); timer = setTimeout(() => poll(runId, epoch), 5000);}}
  }
  function renderRun() {
    const drafts = new Map(Array.from($('run-steps').querySelectorAll('input[id]')).map(input => [input.id, input.value]));
    const value = state.run; $('run-panel').hidden = !value; if (!value) return;
    $('run-status').textContent = `${value.status}${value.error ? ': ' + value.error : ''}`;
    $('run-steps').replaceChildren();
    value.steps.forEach(step => {
      const row = el('div', undefined, {className: 'row'}); row.append(el('p', `${step.step_id}: ${step.status}`));
      if (step.job_id) row.append(el('a', 'Open Jobs', {href: '/tasks?job_id=' + encodeURIComponent(step.job_id)}));
      if (step.status === 'waiting_review') {
        row.append(el('a', 'Review evidence', {href: '/curation?location_id=' + encodeURIComponent(step.input_location_id || value.location_id)}));
        if (step.review_required) {
          const workspace = el('input', undefined, {placeholder: 'Curation workspace ID', id: 'review-workspace-' + step.step_id, disabled: !canEdit});
          const revision = el('input', undefined, {type: 'number', placeholder: 'Draft revision', min: 0, id: 'review-revision-' + step.step_id, disabled: !canEdit});
          row.append(workspace, revision);
        }
        row.append(button('Approve', () => decide(step.step_id, 'approve')), button('Skip', () => decide(step.step_id, 'skip')));
      }
      $('run-steps').append(row);
    });
    drafts.forEach((value, id) => {if ($(id)) $(id).value = value;});
    $('cancel').disabled = !canEdit || !['queued', 'running', 'waiting_review', 'error'].includes(value.status);
    $('retry').disabled = !canEdit || value.status !== 'error';
  }
  async function decide(stepId, decision) {
    const epoch = state.epoch, runId = state.run.run_id;
    const workspace = $('review-workspace-' + stepId), revision = $('review-revision-' + stepId);
    const value = await api(`/api/pipeline-runs/${runId}/decisions`, 'POST', {step_id: stepId, decision, expected_revision: state.run.revision, workspace_id: workspace?.value, workspace_revision: revision ? Number(revision.value) : undefined});
    if (epoch !== state.epoch) return;
    state.run = value; renderRun(); clearTimeout(timer); poll(runId, epoch);
  }
  $('location').addEventListener('change', guard(async () => {clearTimeout(timer); state.run = null; renderRun(); invalidate(); await Promise.all([loadOperators(), loadRuns()]);}));
  $('name').addEventListener('input', invalidate);
  $('add').addEventListener('click', guard(() => {
    const available = state.operators.filter(value => value.available && value.pipeline_allowed);
    const spec = available.find(value => value.operator_id === 'curation.snapshot') || available[0];
    if (!spec) throw new Error('Choose a dataset with an available execution node.');
    state.steps.push({step_id: 'step_' + crypto.randomUUID().slice(0, 8), operator_id: spec.operator_id, version: spec.version, parameters: {}, input: 'source', artifacts: [], review_before: !!spec.review_required, options: {}});
    invalidate(); render();
  }));
  $('saved').addEventListener('change', guard(async () => {
    const epoch = ++state.epoch;
    const value = $('saved').value ? await api('/api/pipelines/' + $('saved').value) : null;
    if (epoch !== state.epoch) return;
    state.run = null; renderRun();
    state.pipeline = value; state.steps = value?.definition.steps || []; $('name').value = value?.definition.name || 'New pipeline';
    invalidate(); state.invalid.clear(); state.dirty = false; render();
  }));
  $('preview').addEventListener('click', guard(preview));
  $('save').addEventListener('click', guard(async () => {
    if (state.invalid.size) throw new Error('Correct the invalid parameter fields.');
    const epoch = state.epoch, edit = state.edit, current = state.pipeline;
    const saved = await api(current ? '/api/pipelines/' + current.pipeline_id : '/api/pipelines', current ? 'PUT' : 'POST', {definition: definition(), expected_revision: current?.revision});
    if (epoch !== state.epoch) return;
    state.pipeline = saved;
    if (edit !== state.edit) {await loadSaved(); $('saved').value = saved.pipeline_id; feedback('Saved the submitted version. Your newer edits are unsaved.'); return;}
    state.steps = saved.definition.steps; state.dirty = false;
    await loadSaved(); $('saved').value = saved.pipeline_id; render(); await preview();
  }));
  $('run').addEventListener('click', guard(async () => {
    const epoch = state.epoch;
    const value = await api('/api/pipelines/' + state.pipeline.pipeline_id + '/runs', 'POST', {revision: state.pipeline.revision, location_id: $('location').value}, {'Idempotency-Key': crypto.randomUUID()});
    if (epoch !== state.epoch) return;
    state.run = value; renderRun(); await loadRuns(); $('runs').value = value.run_id;
    history.replaceState(null, '', '?location_id=' + encodeURIComponent(value.location_id) + '&run_id=' + encodeURIComponent(value.run_id)); clearTimeout(timer); poll(value.run_id, epoch);
  }));
  for (const action of ['cancel', 'retry']) $(action).addEventListener('click', guard(async () => {
    const epoch = state.epoch, runId = state.run.run_id;
    const value = await api(`/api/pipeline-runs/${runId}/${action}`, 'POST', {expected_revision: state.run.revision});
    if (epoch !== state.epoch) return;
    state.run = value; renderRun(); clearTimeout(timer); poll(runId, epoch);
  }));
  async function loadRuns() {
    const epoch = state.epoch, locationId = $('location').value;
    const result = await api('/api/pipeline-runs?location_id=' + encodeURIComponent(locationId));
    if (epoch !== state.epoch || locationId !== $('location').value) return;
    const selected = state.run?.run_id || $('runs').value;
    $('runs').replaceChildren(el('option', 'Choose a run', {value: ''}));
    result.runs.forEach(value => $('runs').append(el('option', `${value.status} · ${value.run_id.slice(0, 8)} · ${new Date(value.updated_at * 1000).toLocaleString()}`, {value: value.run_id})));
    $('runs').value = selected;
  }
  async function openRun(runId) {
    clearTimeout(timer);
    const epoch = state.epoch;
    const result = await api('/api/pipeline-runs/' + encodeURIComponent(runId));
    if (epoch !== state.epoch) return;
    state.run = result; renderRun(); $('runs').value = runId;
    history.replaceState(null, '', '?location_id=' + encodeURIComponent(result.location_id) + '&run_id=' + encodeURIComponent(runId));
    poll(runId, epoch);
  }
  $('runs').addEventListener('change', guard(async () => {if ($('runs').value) await openRun($('runs').value);}));
  window.addEventListener('pagehide', () => {state.epoch++; clearTimeout(timer);});
  for (const id of ['add', 'preview', 'save', 'name']) $(id).disabled = !canEdit || id === 'add';
  guard(async () => {
    const locations = await api('/api/control/locations?summary=1');
    locations.locations.forEach(value => $('location').append(el('option', `${value.node_name || ''} · ${value.dataset_key}`, {value: value.location_id})));
    const requested = new URLSearchParams(location.search).get('location_id'); if (requested) $('location').value = requested;
    await Promise.all([loadOperators(), loadSaved(), loadRuns()]);
    const runId = new URLSearchParams(location.search).get('run_id'); if (runId) await openRun(runId);
  })();
})();
