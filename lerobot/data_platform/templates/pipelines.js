{% include "pipeline_assistant.js" %}
function mountPipelineEditor(root, options) {
  'use strict';
  options = options || {};
  const $ = id => root.querySelector(`[id="${id}"]`);
  const canEdit = root.querySelector('main').dataset.canEdit === 'true';
  const state = {locations: [], reviewOptions: new Map(), savedCursor: null, runsCursor: null, savedEpoch: 0, runsEpoch: 0, operators: [], steps: [], pipeline: null, run: null, preview: null, epoch: 0, edit: 0, invalid: new Set(), dirty: false, selected: null, undo: null, added: [], runKey: null, runBusy: false};
  let timer;
  let disposed = false;
  const controllers = new Set();
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
    if (disposed) throw new Error('Pipeline editor closed');
    const controller = new AbortController(); controllers.add(controller);
    try {
    const response = await fetch(path, {signal: controller.signal, method, credentials: 'same-origin', headers: {'Content-Type': 'application/json', ...headers}, body: value === undefined ? undefined : JSON.stringify(value)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
    if (disposed) throw new Error("Pipeline editor closed");
    return result;
    } finally {controllers.delete(controller);}
  };
  const definition = () => ({name: $('name').value, steps: state.steps});
  const operator = step => state.operators.find(value => value.operator_id === step.operator_id);
  const invalidate = () => {state.edit++; state.preview = null; state.dirty = true; state.runKey = null; $('run').hidden = true; $('run').disabled = true; $('preview-panel').hidden = true; feedback('Unsaved changes · validate before running.'); updateNodeStatus();};
  const guard = fn => async (...args) => {
    try {await fn(...args);} catch (error) {if (!disposed) feedback(error.message, true);}
  };
  const assistant = mountPipelineAssistant(root, {api, canEdit, getLocation: () => $('location').value, getDraft: definition,
    undoDraft: () => {if (state.undo) {state.steps = state.undo.steps; $('name').value = state.undo.name; state.undo = null; state.added = []; invalidate(); render();}},
    loadDraft: async (draft, added = []) => {
      state.undo = structuredClone(definition()); state.added = added; state.selected = draft.steps[0]?.step_id;
      state.pipeline = null; $('saved').value = ''; state.steps = draft.steps; $('name').value = draft.name;
      state.invalid.clear(); invalidate(); render(); await preview();
    }});
  $('location').addEventListener('change', () => assistant.reset());
  const button = (text, action, disabled = false) => {
    const value = el('button', text, {type: 'button', disabled: !canEdit || disabled});
    value.addEventListener('click', guard(action));
    return value;
  };
  function render() {
    const root = $('steps'); root.replaceChildren(); $('step-editor').replaceChildren();
    if (!state.steps.some(step => step.step_id === state.selected)) state.selected = state.steps[0]?.step_id;
    if (!state.steps.length) root.append(el('p', 'Describe a workflow above or add a step.'));
    state.steps.forEach((step, index) => {
      const node = el('article', undefined, {tabIndex: 0, draggable: canEdit}); node.dataset.stepId = step.step_id; node.setAttribute('aria-current', String(state.selected === step.step_id));
      node.addEventListener('click', event => {if (event.target === node || !event.target.closest('button')) {state.selected = step.step_id; render();}});
      node.addEventListener('keydown', guard(event => {if (event.target !== node) return; if (event.altKey && ['ArrowUp','ArrowDown'].includes(event.key)) {event.preventDefault(); move(index, event.key === 'ArrowUp' ? -1 : 1);} else if (event.key === 'Enter') {state.selected = step.step_id; render();}}));
      node.addEventListener('dragstart', event => {event.dataTransfer.setData('text/plain', step.step_id);});
      node.addEventListener('dragover', event => event.preventDefault());
      node.addEventListener('drop', guard(event => {event.preventDefault(); const from = state.steps.findIndex(item => item.step_id === event.dataTransfer.getData('text/plain')); if (from >= 0) move(from, index - from);}));
      const card = el('section');
      const header = el('div', undefined, {className: 'row step-title'});
      header.append(el('strong', `${index + 1}. ${operator(step)?.presentation?.title || step.operator_id}`));
      const actions = el('div', undefined, {className: 'row'});
      actions.append(button('↑', () => move(index, -1), index === 0), button('↓', () => move(index, 1), index === state.steps.length - 1), button('Remove', () => {const users = state.steps.filter(item => item.input === step.step_id || (item.artifacts || []).includes(step.step_id)); if (users.length) throw new Error('Update references in: ' + users.map(item => item.step_id).join(', ')); state.steps.splice(index, 1); state.invalid = new Set([...state.invalid].filter(key => !key.startsWith(step.step_id + ':'))); invalidate(); render();}));
      actions.append(button('+', () => addStep(index + 1)));
      header.append(actions); node.append(header);
      node.append(el('p', Object.entries(step.parameters || {}).filter(([k,v]) => JSON.stringify(v) !== JSON.stringify(operator(step)?.parameters?.properties?.[k]?.default)).map(([k,v]) => `${k}: ${JSON.stringify(v)}`).join(' · ') || 'Default parameters', {className: 'step-summary'}));
      node.append(el('small', `Data: ${step.input || 'source'} · Evidence: ${(step.artifacts || []).join(', ') || 'none'}`));
      if (step.review_before || operator(step)?.review_required) node.append(el('p', 'Human review required'));
      if (state.added.includes(step.step_id)) node.append(el('small', 'AI added · required prerequisite'));
      const status = el('p', '', {className: 'step-status', role: 'status'}); node.append(status);
      root.append(node);
      if (state.selected !== step.step_id) return;
      card.append(el('h2', operator(step)?.presentation?.title || step.operator_id));
      card.append(el('small', `${step.step_id} · ${step.operator_id} · ${step.version}`));
      const selection = el('select', undefined, {disabled: !canEdit, 'ariaLabel': 'Operator'});
      state.operators.filter(value => value.pipeline_allowed || value.operator_id === step.operator_id).forEach(value => {
        const option = el('option', (value.presentation?.title || value.operator_id) + (value.reason ? ` · ${value.reason}` : ''), {value: value.operator_id, disabled: !value.available});
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
      const extraFields = el('details'); extraFields.append(el('summary', 'Advanced parameters'));
      const common = new Set(spec?.presentation?.common_parameters || []);
      const required = new Set(spec?.parameters.required || []);
      Object.entries(spec?.parameters.properties || {}).forEach(([key, schema]) => {
        const label = el('label', (schema.title || key.replaceAll('_', ' ')) + (required.has(key) ? ' *' : ''));
        const types = Array.isArray(schema.type) ? schema.type : [schema.type];
        const kind = types.includes('string') ? 'string' : types.find(value => value !== 'null');
        const stored = Object.hasOwn(step.parameters, key) ? step.parameters[key] : schema.default;
        if (kind === 'array' && ['string', 'integer', 'number', 'boolean'].includes(schema.items?.type)) {
          const group = el('fieldset'); group.append(el('legend', schema.title || key.replaceAll('_', ' ')));
          const values = Array.isArray(stored) ? stored : [];
          const update = value => {step.parameters[key] = value; raw.value = JSON.stringify(step.parameters); invalidate();};
          values.forEach((value, itemIndex) => {
            const row = el('div', undefined, {className: 'row'});
            const input = el('input', undefined, {type: schema.items.type === 'boolean' ? 'checkbox' : schema.items.type === 'string' ? 'text' : 'number', value, checked: !!value, disabled: !canEdit, ariaLabel: `${key} ${itemIndex + 1}`});
            if (schema.items.minimum !== undefined) input.min = schema.items.minimum;
            if (schema.items.maximum !== undefined) input.max = schema.items.maximum;
            input.step = schema.items.type === 'integer' ? '1' : 'any';
            input.addEventListener('input', () => {invalidate(); const invalidKey = `${step.step_id}:${key}:${itemIndex}`; if (!input.checkValidity() || (input.type === 'number' && input.value === '')) {state.invalid.add(invalidKey); return;} state.invalid.delete(invalidKey); const next = [...values]; next[itemIndex] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value; values[itemIndex] = next[itemIndex]; update(next);});
            row.append(input, button('Remove item', () => {update(values.filter((_, i) => i !== itemIndex)); state.invalid = new Set([...state.invalid].filter(k => !k.startsWith(`${step.step_id}:${key}:`))); render();})); group.append(row);
          });
          group.append(button('Add item', () => {update([...values, schema.items.type === 'string' ? '' : schema.items.type === 'boolean' ? false : schema.items.minimum || 0]); render();}));
          (common.has(key) || required.has(key) ? fields : extraFields).append(group);
          return;
        }
        const jsonValue = !kind || ['array', 'object'].includes(kind);
        let control;
        if (schema.enum) {
          control = el('select', undefined, {disabled: !canEdit});
          control.append(el('option', 'Default', {value: ''}));
          schema.enum.forEach(value => control.append(el('option', String(value), {value: JSON.stringify(value)})));
          control.value = stored === undefined ? '' : JSON.stringify(stored);
        }
        else if (jsonValue) control = el('textarea', undefined, {value: stored === undefined ? '' : JSON.stringify(stored), disabled: !canEdit});
        else control = el('input', undefined, {type: kind === 'boolean' ? 'checkbox' : ['number', 'integer'].includes(kind) ? 'number' : 'text', disabled: !canEdit});
        control.setAttribute('aria-label', key);
        if (schema.minimum !== undefined) control.min = schema.minimum;
        if (schema.maximum !== undefined) control.max = schema.maximum;
        if (kind === 'integer') control.step = '1';
        else if (kind === 'number') control.step = 'any';
        if (!jsonValue && !schema.enum) {
          if (kind === 'boolean') control.checked = !!stored;
          else control.value = stored ?? '';
        }
        control.addEventListener('input', () => {invalidate();});
        control.addEventListener('change', guard(() => {
          invalidate(); state.invalid.delete(step.step_id + ':' + key);
          let value;
          if (schema.enum && control.value) value = JSON.parse(control.value);
          else if (kind === 'boolean') value = control.checked;
          else if (!control.value.trim()) {delete step.parameters[key]; raw.value = JSON.stringify(step.parameters); invalidate(); return;}
          else if (jsonValue) {try {value = JSON.parse(control.value);} catch (error) {state.invalid.add(step.step_id + ':' + key); throw error;}}
          else if (['number', 'integer'].includes(kind)) value = Number(control.value);
          else value = control.value;
          if (!control.checkValidity()) {state.invalid.add(step.step_id + ':' + key); throw new Error(`${key}: ${control.validationMessage}`);}
          step.parameters[key] = value; invalidate();
          raw.value = JSON.stringify(step.parameters);
        }));
        label.append(control); if (schema.description) label.append(el('small', schema.description));
        (common.has(key) || required.has(key) ? fields : extraFields).append(label);
      });
      card.append(fields, extraFields);
      if (step.operator_id === 'preprocess.merge') {
        const label = el('label', 'Additional datasets');
        const selected = new Set(step.options?.source_location_ids || []);
        const nodeId = state.locations.find(item => item.location_id === $('location').value)?.node_id;
        const choices = el('select', undefined, {multiple: true, disabled: !canEdit, ariaLabel: 'Merge datasets'});
        state.locations.filter(item => item.node_id === nodeId && item.location_id !== $('location').value).forEach(item => {
          choices.append(el('option', `${item.dataset_key} · ${item.metadata?.codebase_version || 'dataset'}`, {value: item.location_id, selected: selected.has(item.location_id)}));
          selected.delete(item.location_id);
        });
        selected.forEach(id => choices.append(el('option', `${id} · unavailable`, {value: id, selected: true, disabled: true})));
        choices.addEventListener('change', () => {step.options = {...step.options, source_location_ids: [...choices.selectedOptions].map(item => item.value)}; invalidate(); render();});
        label.append(choices); card.append(label);
      }
      const advanced = el('details'); advanced.append(el('summary', 'Input and review options'));
      const options = el('textarea', undefined, {value: JSON.stringify(step.options || {}), disabled: !canEdit});
      options.setAttribute('aria-label', 'Registered Merge inputs, workspace or manifest options');
      options.addEventListener('input', invalidate);
      options.addEventListener('change', guard(() => {invalidate(); state.invalid.delete(step.step_id + ':options'); try {step.options = JSON.parse(options.value);} catch (error) {state.invalid.add(step.step_id + ':options'); throw error;}}));
      advanced.append(options);
      const raw = el('textarea', undefined, {value: JSON.stringify(step.parameters), disabled: !canEdit, ariaLabel: 'Parameter JSON'});
      raw.addEventListener('input', invalidate);
      raw.addEventListener('change', guard(() => {
        state.invalid.add(step.step_id + ':json');
        const value = JSON.parse(raw.value);
        if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('Parameters must be a JSON object');
        step.parameters = value;
        state.invalid = new Set([...state.invalid].filter(key => !key.startsWith(step.step_id + ':') || key.endsWith(':options')));
        invalidate(); render();
      }));
      advanced.append(el('label', 'Parameter JSON'), raw); card.append(advanced); $('step-editor').append(card);
    });
    updateNodeStatus(); requestAnimationFrame(drawDependencies);
  }
  function updateNodeStatus() {
    for (const node of $('steps').querySelectorAll('article')) {
      const issues = (state.preview?.blocked || []).filter(item => item.step_id === node.dataset.stepId);
      const step = state.steps.find(item => item.step_id === node.dataset.stepId);
      if (step) {
        node.querySelector('.step-summary').textContent = Object.entries(step.parameters || {}).filter(([k,v]) => JSON.stringify(v) !== JSON.stringify(operator(step)?.parameters?.properties?.[k]?.default)).map(([k,v]) => `${k}: ${JSON.stringify(v)}`).join(' · ') || 'Default parameters';
        node.querySelector('small').textContent = `Data: ${step.input || 'source'} · Evidence: ${(step.artifacts || []).join(', ') || 'none'}`;
      }
      const run = !state.dirty && state.run?.steps.find(item => item.step_id === node.dataset.stepId);
      const status = node.querySelector('.step-status');
      status.textContent = issues.length ? issues.map(item => item.reason).join(' · ') : run ? run.status : state.preview?.can_execute ? 'Validated' : 'Not validated';
      status.className = 'step-status ' + (issues.length ? 'error' : 'muted');
    }
  }
  function drawDependencies() {
    if (disposed) return;
    $('steps').querySelector('svg')?.remove();
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'); svg.setAttribute('class', 'dependency-svg'); svg.setAttribute('aria-hidden', 'true');
    const nodes = new Map([...$('steps').querySelectorAll('article')].map(node => [node.dataset.stepId,node]));
    state.steps.forEach(step => {for (const [ref, evidence] of [[step.input,false], ...(step.artifacts || []).map(id => [id,true])]) {
      const from = nodes.get(ref), to = nodes.get(step.step_id); if (!from || !to) continue;
      const y1 = from.offsetTop + from.offsetHeight/2, y2 = to.offsetTop + to.offsetHeight/2;
      const path = document.createElementNS(ns,'path'); path.setAttribute('d', `M 30 ${y1} H ${evidence ? 8 : 20} V ${y2} H 30`); path.setAttribute('fill','none'); path.setAttribute('stroke',evidence ? '#c084fc' : '#38bdf8'); if (evidence) path.setAttribute('stroke-dasharray','4 3'); svg.append(path);
    }}); $('steps').prepend(svg);
  }
  function move(index, delta) {
    const other = index + delta; if (!canEdit || other < 0 || other >= state.steps.length) return;
    const next = [...state.steps]; next.splice(other, 0, next.splice(index, 1)[0]);
    const seen = new Set(['source']);
    for (const step of next) {if (!seen.has(step.input || 'source') || (step.artifacts || []).some(id => !seen.has(id))) throw new Error('Move the dependent steps first; inputs must precede their users.'); seen.add(step.step_id);}
    state.steps = next; invalidate(); render();
  }
  async function loadOperators() {
    const epoch = ++state.epoch;
    const data = await api('/api/operators?location_id=' + encodeURIComponent($('location').value));
    if (epoch !== state.epoch) return;
    state.operators = data.operators; $('add').disabled = !canEdit || !state.operators.some(value => value.available && value.pipeline_allowed); render();
    $('review-link').href = '/curation?location_id=' + encodeURIComponent($('location').value);
  }
  async function loadSaved(append = false) {
    const epoch = state.epoch, request = ++state.savedEpoch;
    const params = new URLSearchParams({page_size: '50', search: $('saved-search').value});
    if (append && state.savedCursor) params.set('cursor', state.savedCursor);
    const data = await api('/api/pipelines?' + params);
    if (epoch !== state.epoch || request !== state.savedEpoch) return;
    const selected = $('saved').value;
    if (!append) $('saved').replaceChildren(el('option', 'New pipeline', {value: ''}));
    data.pipelines.forEach(item => {if (![...$('saved').options].some(option => option.value === item.pipeline_id)) $('saved').append(el('option', `${item.name} · v${item.revision}`, {value: item.pipeline_id}));});
    $('saved').value = selected;
    state.savedCursor = data.next_cursor; $('saved-more').hidden = !data.has_more;
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
    feedback(result.can_execute ? 'Plan is ready.' : 'Resolve the blocked steps before running.', !result.can_execute); updateNodeStatus(); return result;
  }
  async function poll(runId, epoch) {
    if (disposed || epoch !== state.epoch) return;
    if (document.hidden) {timer = setTimeout(() => poll(runId, epoch), 3000); return;}
    try {
      const value = await api('/api/pipeline-runs/' + runId);
      if (epoch !== state.epoch || state.run?.run_id !== runId) return;
      state.run = value; renderRun(); updateNodeStatus();
      if (['queued', 'running', 'waiting_review', 'cancel_requested', 'retry_requested'].includes(value.status)) timer = setTimeout(() => poll(runId, epoch), 3000);
    } catch (error) {if (epoch === state.epoch && state.run?.run_id === runId) {feedback(error.message, true); timer = setTimeout(() => poll(runId, epoch), 5000);}}
  }
  function renderRun() {
    const drafts = new Map(Array.from($('run-steps').querySelectorAll('input[id],select[id]')).map(input => [input.id, input.value]));
    const value = state.run; $('run-panel').hidden = !value; if (!value) return;
    $('run-status').textContent = `${value.status}${value.error ? ': ' + value.error : ''}`;
    $('run-steps').replaceChildren();
    value.steps.forEach(step => {
      const row = el('div', undefined, {className: 'row'}); row.append(el('p', `${step.step_id}: ${step.status}`));
      if (step.job_id) row.append(el('a', 'Open Jobs', {href: '/?page=runs&job_id=' + encodeURIComponent(step.job_id)}));
      if (step.status === 'waiting_review') {
        row.append(el('a', 'Review evidence', {href: '/curation?location_id=' + encodeURIComponent(step.input_location_id || value.location_id)}));
        if (step.review_required) {
          const cacheKey = `${value.run_id}:${step.step_id}:${value.revision}`;
          const workspace = el('select', undefined, {id: 'review-workspace-' + step.step_id, disabled: !canEdit, ariaLabel: 'Review draft'});
          workspace.append(el('option', 'Choose a reviewed draft', {value: ''}));
          const populate = items => {
            items.forEach(item => workspace.append(el('option', `${item.workspace_id.slice(0, 8)} · revision ${item.revision}`, {value: `${item.workspace_id}:${item.revision}`})));
            workspace.value = drafts.get(workspace.id) || '';
          };
          if (state.reviewOptions.has(cacheKey)) populate(state.reviewOptions.get(cacheKey));
          else {
            const epoch = state.epoch;
            api(`/api/pipeline-runs/${value.run_id}/review-options?step_id=${encodeURIComponent(step.step_id)}`).then(result => {
              if (epoch !== state.epoch || state.run?.revision !== result.run_revision || state.run?.run_id !== value.run_id) return;
              state.reviewOptions.set(cacheKey, result.workspaces); populate(result.workspaces);
            }).catch(error => {if (!disposed && epoch === state.epoch) feedback(error.message, true);});
          }
          row.append(workspace);
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
    const workspace = $('review-workspace-' + stepId);
    const [workspaceId, revision] = (workspace?.value || '').split(':');
    state.reviewOptions.clear();
    const value = await api(`/api/pipeline-runs/${runId}/decisions`, 'POST', {step_id: stepId, decision, expected_revision: state.run.revision, workspace_id: workspaceId, workspace_revision: revision !== undefined ? Number(revision) : undefined});
    if (epoch !== state.epoch) return;
    state.run = value; renderRun(); clearTimeout(timer); poll(runId, epoch);
  }
  $('location').addEventListener('change', guard(async () => {clearTimeout(timer); state.run = null; renderRun(); invalidate(); await Promise.all([loadOperators(), loadRuns()]);}));
  $('name').addEventListener('input', invalidate);
  function addStep(position = state.steps.length) {
    const available = state.operators.filter(value => value.available && value.pipeline_allowed);
    const spec = available.find(value => value.operator_id === 'curation.snapshot') || available[0];
    if (!spec) throw new Error('Choose a dataset with an available execution node.');
    const step = {step_id: 'step_' + crypto.randomUUID().slice(0, 8), operator_id: spec.operator_id, version: spec.version, parameters: {}, input: 'source', artifacts: [], review_before: !!spec.review_required, options: {}}; state.steps.splice(position, 0, step); state.selected = step.step_id;
    invalidate(); render();
  }
  $('add').addEventListener('click', guard(() => addStep()));
  $('saved').addEventListener('change', guard(async () => {
    const epoch = ++state.epoch;
    const value = $('saved').value ? await api('/api/pipelines/' + $('saved').value) : null;
    if (epoch !== state.epoch) return;
    state.run = null; renderRun();
    state.pipeline = value; state.steps = value?.definition.steps || []; $('name').value = value?.definition.name || 'New pipeline';
    invalidate(); state.invalid.clear(); state.dirty = false; render();
  }));
  $('preview').addEventListener('click', guard(preview));
  async function saveVersion() {
    if (state.invalid.size) throw new Error('Correct the invalid parameter fields.');
    const epoch = state.epoch, edit = state.edit, current = state.pipeline;
    const saved = await api(current ? '/api/pipelines/' + current.pipeline_id : '/api/pipelines', current ? 'PUT' : 'POST', {definition: definition(), expected_revision: current?.revision});
    if (epoch !== state.epoch) return;
    state.pipeline = saved;
    if (edit !== state.edit) {await loadSaved(); $('saved').value = saved.pipeline_id; feedback('Saved the submitted version. Your newer edits are unsaved.'); return;}
    state.steps = saved.definition.steps; state.dirty = false;
    await loadSaved(); $('saved').value = saved.pipeline_id; render(); await preview();
  }
  $('save').addEventListener('click', guard(saveVersion));
  $('check-run').addEventListener('click', guard(async () => {
    const result = await preview(); if (!result?.can_execute) return;
    $('preview-result').textContent += '\nServer: ' + (state.locations.find(item => item.location_id === $('location').value)?.node_name || '') + '\n' + result.steps.map(step => `${step.step_id}: ${step.output.type} ${step.output.path_template || ''}${step.review_before ? ' · Human review' : ''}`).join('\n');
    $('run').hidden = false; $('run').disabled = false;
  }));
  $('run').addEventListener('click', guard(async () => {
    if (state.runBusy) return; state.runBusy = true; $('run').disabled = true;
    const edit = state.edit;
    try {
    const checked = await preview(); if (!checked?.can_execute || edit !== state.edit) return;
    if (!state.pipeline || state.dirty) await saveVersion();
    if (edit !== state.edit || state.dirty) throw new Error('The plan changed. Validate again.');
    const epoch = state.epoch;
    const value = await api('/api/pipelines/' + state.pipeline.pipeline_id + '/runs', 'POST', {revision: state.pipeline.revision, location_id: $('location').value}, {'Idempotency-Key': state.runKey ||= crypto.randomUUID()});
    if (epoch !== state.epoch) return;
    state.run = value; renderRun(); await loadRuns(); $('runs').value = value.run_id;
    if (!options.embedded) history.replaceState(null, '', '?location_id=' + encodeURIComponent(value.location_id) + '&run_id=' + encodeURIComponent(value.run_id)); clearTimeout(timer); poll(value.run_id, epoch); $('run-history').open = true; $('run').hidden = true;
    } finally {state.runBusy = false; $('run').disabled = !state.preview?.can_execute;}
  }));
  for (const action of ['cancel', 'retry']) $(action).addEventListener('click', guard(async () => {
    const epoch = state.epoch, runId = state.run.run_id;
    const value = await api(`/api/pipeline-runs/${runId}/${action}`, 'POST', {expected_revision: state.run.revision});
    if (epoch !== state.epoch) return;
    state.run = value; renderRun(); clearTimeout(timer); poll(runId, epoch);
  }));
  async function loadRuns(append = false) {
    const epoch = state.epoch, locationId = $('location').value, request = ++state.runsEpoch;
    const params = new URLSearchParams({location_id: locationId, page_size: '50'});
    if ($('run-filter').value) params.set('status', $('run-filter').value);
    if (append && state.runsCursor) params.set('cursor', state.runsCursor);
    const result = await api('/api/pipeline-runs?' + params);
    if (epoch !== state.epoch || locationId !== $('location').value || request !== state.runsEpoch) return;
    const selected = state.run?.run_id || $('runs').value;
    if (!append) $('runs').replaceChildren(el('option', 'Choose a run', {value: ''}));
    result.runs.forEach(value => {if (![...$('runs').options].some(option => option.value === value.run_id)) $('runs').append(el('option', `${value.status} · ${value.run_id.slice(0, 8)} · ${new Date(value.updated_at * 1000).toLocaleString()}`, {value: value.run_id}));});
    $('runs').value = selected;
    state.runsCursor = result.next_cursor; $('runs-more').hidden = !result.has_more;
  }
  async function openRun(runId) {
    clearTimeout(timer);
    const epoch = state.epoch;
    const result = await api('/api/pipeline-runs/' + encodeURIComponent(runId));
    if (epoch !== state.epoch) return;
    state.run = result; $('run-history').open = true; renderRun(); $('runs').value = runId;
    if (!options.embedded) history.replaceState(null, '', '?location_id=' + encodeURIComponent(result.location_id) + '&run_id=' + encodeURIComponent(runId));
    poll(runId, epoch);
  }
  $('saved-search').addEventListener('input', guard(() => loadSaved()));
  $('saved-more').addEventListener('click', guard(() => loadSaved(true)));
  $('runs-more').addEventListener('click', guard(() => loadRuns(true)));
  $('run-filter').addEventListener('change', guard(() => loadRuns()));
  $('use-template').addEventListener('click', guard(() => {
    const ids = {quality: ['curation.snapshot', 'curation.quality'], subset: ['curation.snapshot', 'preprocess.split', 'curation.quality'], viewer: ['curation.snapshot', 'viewer.prepare']}[$('template').value];
    if (!canEdit || !ids) return;
    if (ids.some(id => !state.operators.some(spec => spec.operator_id === id && spec.available))) throw new Error('This template requires operators unavailable on the selected node.');
    state.steps = ids.map((id, i) => ({step_id: 'step_' + (i + 1), operator_id: id, version: state.operators.find(spec => spec.operator_id === id).version, parameters: {}, options: {}, input: i > 1 && ids[i - 1] === 'preprocess.split' ? 'step_' + i : 'source', artifacts: [], review_before: false}));
    state.pipeline = null; $('saved').value = ''; state.invalid.clear(); invalidate(); render();
  }));
  $('runs').addEventListener('change', guard(async () => {if ($('runs').value) await openRun($('runs').value);}));
  const destroy = () => {disposed = true; assistant.destroy(); state.epoch++; clearTimeout(timer); for (const controller of controllers) controller.abort(); window.removeEventListener('pagehide', destroy);};
  window.addEventListener('pagehide', destroy);
  for (const id of ['add', 'preview', 'save', 'name', 'use-template', 'check-run']) $(id).disabled = !canEdit || id === 'add';
  guard(async () => {
    const locations = await api('/api/control/locations?summary=1');
    state.locations = locations.locations;
    locations.locations.forEach(value => $('location').append(el('option', `${value.node_name || ''} · ${value.dataset_key}`, {value: value.location_id})));
    const requested = options.locationId || new URLSearchParams(location.search).get('location_id'); if (requested) $('location').value = requested;
    assistant.reset();
    await Promise.all([loadOperators(), loadSaved(), loadRuns()]);
    const runId = options.embedded ? null : new URLSearchParams(location.search).get('run_id'); if (runId) await openRun(runId);
  })();
  return {destroy};
}
