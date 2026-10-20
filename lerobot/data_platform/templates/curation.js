/* Shared Curation client: execution location is a property of the selected target. */
window.createCurationWorkspace = (root, initialTarget) => {
  'use strict';
  const $ = id => root.querySelector(`[data-curation-id="${id}"]`);
  const target = {...initialTarget};
  let destroyed = false;
  let context = null, result = null, busy = false, jobs = [], page = new URLSearchParams(location.search).get('page') || 'annotation';
  let inputRuns = new Set(), captionRuns = [], timer = null, captionController = null, reviewRevision = null;
  let savedReview = null, reviewSelection = null;
  let activeTab = '', resultRequest = 0;
  const pending = JSON.parse(sessionStorage.getItem('curation-deliveries') || '{}');
  const panelHomes = new Map();
  root.querySelectorAll('[data-curation-panel]').forEach(panel => {
    const home = document.createComment('Shared curation panel'); panel.before(home); panelHomes.set(panel, home);
  });
  const text = (tag, value, className) => { const node = document.createElement(tag); node.textContent = value; if (className) node.className = className; return node; };
  const notice = (message, error = false) => { $('notice').hidden = !message; $('notice').textContent = message; $('notice').className = error ? 'error' : ''; };
  const query = () => new URLSearchParams(Object.entries(target).filter(([, value]) => value != null && value !== '')).toString();
  const draft = () => context?.workspaces.find(item => item.workspace_id === $('workspace').value);
  const episode = () => Number($('episode').value);
  const reviewValue = () => JSON.stringify([...$('review-form').querySelectorAll('input,select,textarea')]
    .map(input => [input.name, input.type === 'checkbox' ? input.checked : input.value]));
  const reviewDirty = () => savedReview !== null && savedReview !== reviewValue();
  function reviewStatus() {
    $('review-save-state').textContent = reviewDirty() ? 'Unsaved changes' : 'No unsaved changes';
    $('review-save-state').className = reviewDirty() ? 'review-unsaved' : 'muted';
  }
  function allowReviewChange() {
    if (busy) { notice('Wait for the current request to finish before changing the review.', true); return false; }
    return !reviewDirty() || window.confirm('Discard unsaved episode review changes? Choose Cancel to keep editing, then save your review.');
  }
  function beforeDatasetChange(event) { if (!allowReviewChange()) event.preventDefault(); }
  function beforeUnload(event) {
    if (!reviewDirty() && !busy) return;
    event.preventDefault(); event.returnValue = '';
  }
  function reviewAction(fn) { if (allowReviewChange()) return action(fn); }
  const operationNames = {snapshot:'Synchronize version', validate_source:'Validate source', stage:'Compute stages', quality:'Detect quality issues', labeling:'Run object labeling', tagging:'Run auto-tagging', embedding:'Compute embeddings', project:'Project embeddings', compare_summary:'Prepare comparison summary', construction_preview:'Preview construction', construction:'Generate data', materialize:'Build dataset'};
  const resultOperations = {
    quality_flags: ['quality'], quality_report: ['quality'], stage_subtask: ['stage'],
    labeling: ['labeling'], tagging: ['tagging','labeling'], embedding: ['embedding','project'],
    construction: ['construction_preview','construction','labeling','tagging']
  };
  const inputOperations = {
    quality: ['quality'], labeling: ['labeling'], tagging: ['labeling','tagging'], project: ['embedding','project'],
    construction_preview: ['labeling','tagging'], construction: ['labeling','tagging','construction_preview']
  };
  const tabOperations = {quality_flags:['quality'], stage_subtask:['stage'], labeling:['labeling'], tagging:['tagging'],
    embedding:['embedding','project'], construction:['construction_preview','construction']};
  const qualityNames = {legacy:'Control & annotation checks (previous version)', control:'Control signal consistency', annotations:'Task & stage annotations', s1:'Joint motion', eef:'End-effector motion', s3:'Numeric validity', c3:'Image & video quality'};
  const fields = {
    stage: [['episodes','Episodes','episodes'],['prepare_workers','Workers','number',4],['fallback_stage_count','Fallback stage count','number',5]],
    quality: [['episodes','Episodes','episodes'],['workers','Workers','number',4],['overwrite','Recompute automatic flags','checkbox',false]],
    labeling: [['episodes','Episodes','episodes'],['backend','Backend','select',['qwen_dashscope','grounding_dino','qwen_remote']],['run_mode','Run mode','select',['missing','full']],['workers','Workers','number',1],['save_vis','Save visual previews','checkbox',true],['model_id','Local model','text'],['qwen_model','VLM model','text'],['endpoint','Model endpoint','text'],['devices','Devices','text'],['box_threshold','Box threshold','number',0.3],['text_threshold','Text threshold','number',0.25],['min_pixels','Minimum pixels','number',1024],['max_pixels','Maximum pixels','number',9800]],
    tagging: [['episodes','Episodes','episodes'],['selected_tags','Tag names (comma separated)','list'],['vlm_backend','VLM backend','select',['qwen_dashscope','qwen_remote']],['vlm_model','VLM model','text'],['vlm_endpoint','Model endpoint','text'],['workers','Workers','number',4]],
    embedding: [['episodes','Episodes','episodes'],['ckpt_path','Checkpoint on execution node','text'],['layer_hook','Layer','text','pi_prefix'],['openpi_config','Model configuration','text'],['workers','Workers','number',1],['devices','Devices','text'],['refit','Refit embeddings','checkbox',false]],
    project: [['method','Projection','select',['auto','umap','pca']],['seed','Seed','number',42],['n_neighbors','Neighbors','number',15],['min_dist','Minimum distance','number',0.1],['metric','Metric','text','euclidean']],
    compare_summary: [],
    construction_preview: [['uncertainty_threshold','Uncertainty threshold','number',50],['allow_pick_to_give','Allow pick-to-give examples','checkbox',false]],
    construction: [['uncertainty_threshold','Uncertainty threshold','number',50],['oversample_factor','Oversample factor','number',1],['include_positives','Include positive examples','checkbox',false],['allow_pick_to_give','Allow pick-to-give examples','checkbox',false]]
  };
  for (const kind of ['labeling','tagging']) fields[kind].push(['trial','Trial sample','checkbox',false],['trial_per_type','Episodes per task type','number',20],['trial_seed','Trial seed','number',0]);
  function indices(value) {
    if (!value.trim()) return null;
    const result = new Set();
    for (const part of value.split(',')) {
      const match = part.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
      if (!match) throw new Error('Use episode numbers or ranges such as 3,7,17-19.');
      const start = Number(match[1]), end = Number(match[2] || match[1]);
      if (end < start || end - start > 100000) throw new Error('Invalid episode range.');
      for (let n = start; n <= end; n++) result.add(n);
    }
    return [...result].sort((a,b) => a-b);
  }
  async function api(url, body, method = body ? 'POST' : 'GET') {
    const mutation = !['GET','HEAD'].includes(method);
    const signature = mutation ? JSON.stringify([url, method, body]) : '';
    const headers = body ? {'Content-Type':'application/json'} : {};
    if (mutation) {
      pending[signature] ||= crypto.randomUUID();
      sessionStorage.setItem('curation-deliveries', JSON.stringify(pending));
      headers['Idempotency-Key'] = pending[signature];
    }
    let response;
    try { response = await fetch(url, {method, headers, body: body ? JSON.stringify(body) : undefined}); }
    catch (error) { throw new Error(mutation ? 'Submission could not be confirmed. Check Tasks before retrying; retrying the same request keeps its delivery key.' : 'Connection unavailable. Refresh to reconnect.'); }
    const data = await response.json();
    if (destroyed) throw new Error('Dataset selection changed.');
    if (mutation) { delete pending[signature]; sessionStorage.setItem('curation-deliveries', JSON.stringify(pending)); }
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
    return data;
  }
  async function action(fn) {
    if (busy) return;
    busy = true; buttons(); notice('Submitting…');
    try { await fn(); }
    catch (error) { notice(error.message, true); }
    finally { busy = false; buttons(); }
  }
  function options(select, items, value, label, empty) {
    const previous = select.value;
    select.replaceChildren();
    if (empty) { const option = text('option', empty); option.value = ''; select.append(option); }
    for (const item of items) { const option = text('option', label(item)); option.value = value(item); select.append(option); }
    if ([...select.options].some(item => item.value === previous)) select.value = previous;
  }
  function buttons() {
    if (!context) return;
    root.querySelectorAll('[data-command]').forEach(button => {
      button.closest('form').querySelectorAll('input,select,textarea').forEach(input => {
        input.disabled = busy || !context.can_edit;
      });
      const capability = context.operations[`curation.${button.dataset.command}`];
      let blocked = capability?.reason || '';
      if (button.dataset.command === 'quality') {
        const form = button.closest('form');
        form.querySelectorAll('[name=checks]').forEach(input => {
          const ready = context.quality_checks?.[input.value];
          const available = ready?.available;
          input.disabled = busy || !context.can_edit || !available;
          const reason = input.closest('label').querySelector('.quality-check-reason');
          reason.textContent = !context.can_edit ? 'Read-only access' : available ? 'Available' : ready?.reason || 'Unavailable';
        });
        if (!form.dataset.qualityInitialized) {
          form.querySelectorAll('[name=checks]:disabled').forEach(input => { input.checked = false; });
          if (context.quality_checks?.eef?.available && context.can_edit) {
            form.querySelector('[name=checks][value=eef]').checked = true;
            if (context.quality_checks?.s3?.available) form.querySelector('[name=checks][value=s3]').checked = true;
          }
          form.dataset.qualityInitialized = 'true';
        }
        const selected = [...form.querySelectorAll('[name=checks]:checked')];
        if (!blocked && (!selected.length || selected.some(input => input.disabled && !busy))) blocked = 'Select supported quality checks.';
        form.elements.quality_profile.disabled = busy || !context.can_edit || !context.quality_checks?.control?.available;
        form.elements.quality_profile.closest('label').hidden = !context.quality_checks?.control?.available;
        const video = selected.some(input => input.value === 'c3');
        form.querySelector('.quality-video-settings').hidden = !video;
        form.elements.video_workers.closest('label').hidden = !video;
        for (const name of ['video_mode','video_workers']) form.elements[name].disabled = busy || !context.can_edit || !video;
        form.elements.qc_config.disabled = busy || !context.can_edit || context.quality_protocol !== 1;
      }
      if (button.dataset.command === 'labeling' && !blocked) {
        const backend = button.closest('form').elements.backend.value;
        const ready = context.backends?.labeling?.backends?.[backend];
        if (!ready?.available) blocked = ready?.error || 'This labeling backend is unavailable on the execution node.';
      }
      button.disabled = busy || !capability?.can_execute || !!blocked;
      button.title = blocked;
      const reason = button.parentElement.querySelector('.operation-reason');
      if (reason) reason.textContent = blocked;
    });
    for (const id of ['new-draft','profile']) $(id).disabled = busy || !context.can_edit || !target.dataset_version_id;
    for (const id of ['publish','accept-caption','import-sidecars']) $(id).disabled = busy || !context.can_edit || !draft() || draft().status !== 'open';
    $('accept-caption').disabled ||= !$('caption-run').value;
    $('review-form').querySelectorAll('input,select,textarea,button').forEach(input => {
      input.disabled = !context.can_edit || !draft() || draft().status !== 'open';
    });
    $('review-form').querySelector('button[type=submit]').disabled = busy || !context.can_edit || !draft() || draft().status !== 'open';
    $('review-form').inert = busy;
    $('review-form').setAttribute('aria-busy', String(busy));
    for (const id of ['workspace','episode','reload-review']) $(id).disabled = busy;
    $('build-form').querySelector('button[type=submit]').disabled = busy || !context.operations['curation.materialize']?.can_execute || !$('manifest').value;
    $('recipe-form').querySelector('button[type=submit]').disabled = busy || !context.can_edit || !target.dataset_version_id;
    $('sync').disabled = busy || !context.operations['curation.snapshot']?.can_execute;
    $('sync').title = context.operations['curation.snapshot']?.reason || '';
    $('draft-state').textContent = draft() ? `${draft().status} · revision ${draft().revision}` : 'Select or create a draft';
    $('review-content').hidden = !draft();
    $('review-empty').hidden = !!draft();
    $('review-empty').textContent = !context.can_edit ? 'Read-only access. Open a workspace to view saved decisions.' : 'Create or select a draft above to review episodes.';
    $('compare').disabled = busy || !$('compare-a').value || !$('compare-b').value || $('compare-a').value === $('compare-b').value;
    updateResultVisibility();
  }
  function qualityDetector(issue) {
    if (issue.detector && issue.detector !== 'legacy') return issue.detector;
    if (['early_gripper_transition','stuck_closed_gripper_no_action','state_gripper_transition_without_action','joint_zero_reset_spike'].includes(issue.reason)) return 'control';
    if (['missing_subtask_state','unsegmented_subtask_state','multi_sentence_prompt','multiple_task_assignments','prompt_action_mismatch'].includes(issue.reason)) return 'annotations';
    return 'legacy';
  }
  function drawQualityControls(form) {
    form.className = 'quality-form';
    const scope = form.querySelector('.fields'); scope.classList.add('quality-scope');
    const advanced = document.createElement('details'); advanced.className = 'quality-advanced'; advanced.append(text('summary','Advanced settings'));
    const execution = document.createElement('div'); execution.className = 'fields';
    for (const name of ['workers','overwrite']) execution.append(form.elements[name].closest('label'));
    advanced.append(execution);
    scope.prepend(text('h3','Episode range'));
    scope.append(text('small','Leave blank to check all episodes. Use numbers or ranges, such as 3,7,17–19.'));
    form.append(text('h3','Choose checks'));
    const checks = document.createElement('div'); checks.className = 'fields quality-checks';
    for (const [key,description] of [
      ['control','Gripper command/state consistency, early gripper changes and brief joint resets to zero.'],
      ['annotations','Task assignments, task-text format and existing stage labels. Stages are checked only when declared; flags need human review.'],
      ['s1','Sudden joint jumps and excessive speed in joint angles measured in radians.'],
      ['eef','Position and orientation jump candidates, invalid poses and timing. Supports UMI and GenRobot pose signals; no joint angles required.'],
      ['s3','Missing, non-finite or malformed numeric values. Custom bounds are optional.'],
      ['c3','Missing or unreadable media, black frames, and blur or freeze candidates.']
    ]) {
      const field = document.createElement('label'), input = document.createElement('input'), copy = document.createElement('span');
      input.type = 'checkbox'; input.name = 'checks'; input.value = key; input.checked = ['control','annotations'].includes(key); input.onchange = buttons;
      copy.append(text('strong',qualityNames[key]),text('span',description,'quality-check-description'),text('small','','quality-check-reason'));
      field.append(input,copy); checks.append(field);
    }
    form.append(checks);
    const grid = document.createElement('div'); grid.className = 'fields';
    for (const [name,label,values] of [['quality_profile','Processing profile',['','DVT2','DVT1']],['video_mode','Video coverage',['sampled','full']]]) {
      const field = text('label',label), select = document.createElement('select'); select.name = name;
      values.forEach(value => { const label = name === 'video_mode' ? value === 'sampled' ? 'Sampled frames' : 'Every frame' : value || 'Default (DVT2)'; const option = text('option',label); option.value = value; select.append(option); });
      select.onchange = buttons; field.append(select); grid.append(field);
    }
    execution.append(grid.firstElementChild);
    grid.classList.add('quality-video-settings'); grid.hidden = true;
    const workers = text('label','Video workers (1–16)'), input = document.createElement('input'); input.type = 'number'; input.name = 'video_workers'; input.min = '1'; input.max = '16'; input.value = '2'; workers.append(input); execution.append(workers);
    const config = text('label','Thresholds and bounds (JSON)'), editor = document.createElement('textarea'); editor.name = 'qc_config'; editor.rows = 5; editor.placeholder = '{"schema_version":1,"s1":{"max_step":0.5,"max_speed":20},"s3":{"bounds":[]},"c3":{"sample_every_seconds":1}}'; config.append(editor);
    form.append(grid);
    advanced.append(config,text('p','Leave JSON blank to use default thresholds. S1 uses radians and radians/second; S3 bounds must match metadata units.','muted'));
    form.append(advanced);
  }
  function drawResultOptions() {
    if (!context) return;
    const allowed = (resultOperations[activeTab] || []).map(kind => `curation.${kind}`);
    const runs = context.runs.filter(item => allowed.includes(item.operation)).sort((a,b) => new Date(b.created_at) - new Date(a.created_at));
    options($('result'), runs, item => item.run, item => `${operationNames[item.operation.split('.')[1]]} · ${new Date(item.created_at).toLocaleString()}`, runs.length ? 'Select a result' : 'No saved results yet');
    updateResultVisibility();
  }
  function updateResultVisibility() {
    const visible = !!result && result.run === $('result').value && (page !== 'quality' || !!result.quality);
    $('result-body').hidden = !visible; $('artifact-downloads').hidden = !visible;
    $('result-empty').hidden = visible;
    $('load-result').disabled = busy || !$('result').value;
    $('result-empty').textContent = $('result').options.length > 1 ? 'Select a saved result, then open it.' : 'No saved results for this view yet.';
    const kind = result?.operation?.split('.')[1];
    const acceptsAnnotations = visible && ['stage','labeling','tagging'].includes(kind);
    $('accept-result').disabled = busy || !context?.can_edit || draft()?.status !== 'open' || !acceptsAnnotations;
    $('accept-result').title = acceptsAnnotations ? '' : 'Open an annotation result for the selected episode first.';
    const allowedInputs = new Set((tabOperations[activeTab] || []).flatMap(operation => inputOperations[operation] || []));
    $('use-result').closest('label').hidden = !allowedInputs.size;
    $('use-result').disabled = busy || !context?.can_edit || !visible || !allowedInputs.has(kind);
    $('use-result').checked = inputRuns.has($('result').value);
    $('input-description').textContent = inputRuns.size ? `${inputRuns.size} input result(s) selected for this view` : '';
  }
  function selectPage(value, tab) {
    const previousPage = page;
    if (activeTab !== tab) { inputRuns.clear(); resultRequest++; }
    activeTab = tab;
    page = ['explore','quality','annotation','dataset_build'].includes(value) ? value : 'annotation';
    root.dataset.curationPage = page;
    for (const [panel,home] of panelHomes) {
      if (page === 'quality') root.querySelector(`[data-quality-slot="${panel.dataset.curationPanel}"]`).append(panel);
      else home.after(panel);
      if (panel.dataset.curationPanel === 'jobs' && (page === 'quality' || previousPage === 'quality')) panel.open = page === 'quality';
    }
    root.querySelector('[data-curation-heading=results]').textContent = tab === 'quality_report' ? 'Data report' : page === 'quality' ? '2. Inspect results' : 'Generated results';
    root.querySelector('.quality-task-panel').hidden = tab === 'quality_report';
    root.querySelector('.quality-review').hidden = tab === 'quality_report';
    $('jobs').querySelectorAll('[data-curation-operation]').forEach(row => { row.hidden = page === 'quality' && row.dataset.curationOperation !== 'curation.quality'; });
    drawResultOptions();
    root.querySelectorAll('[data-section]').forEach(item => { item.hidden = item.dataset.section !== page; });
    root.querySelectorAll('[data-page]').forEach(item => item.setAttribute('aria-selected', item.dataset.page === page));
    root.querySelectorAll('[data-curation-tab]').forEach(item => { item.hidden = item.dataset.curationTab !== tab; });
    const panels = {
      quality_flags: ['workspace','results','review','jobs'], quality_report: ['results'],
      stage_subtask: ['workspace','results','review','jobs'], labeling: ['workspace','results','review','jobs'],
      tagging: ['workspace','results','review','jobs'], embedding: ['results','jobs'], compare: ['jobs'],
      construction: ['workspace','results','jobs'], curation_manifest: ['workspace','review','jobs']
    };
    root.querySelectorAll('[data-curation-general]').forEach(item => {
      item.hidden = !(panels[tab] || []).includes(item.dataset.curationPanel);
    });
    root.querySelectorAll('[data-curation-version]').forEach(item => { item.hidden = tab === 'temporal_caption'; });
    $('report-viewer').hidden = !context?.viewer_url || !['quality_flags','quality_report'].includes(tab);
    $('prepare-viewer').hidden = !!context?.viewer_url || !context?.can_edit || !['quality_flags','quality_report'].includes(tab);
    openLatestReport().catch(error => notice(error.message, true));
  }
  async function openLatestReport() {
    if (activeTab !== 'quality_report' || !context) return;
    if (!$('result').value) $('result').value = $('result').options[1]?.value || '';
    if ($('result').value && result?.run !== $('result').value) await loadResult();
  }
  function drawForms() {
    root.querySelectorAll('[data-operation]').forEach(container => {
      const operation = container.dataset.operation, form = document.createElement('form'), grid = document.createElement('div'); grid.className = 'fields';
      for (const [key,label,type,initial] of fields[operation]) {
        const field = text('label',label), input = document.createElement(type === 'select' ? 'select' : 'input'); input.name = key;
        if (type === 'select') initial.forEach(value => { const option = text('option', value.replaceAll('_',' ')); option.value = value; input.append(option); });
        else { input.type = ['number','checkbox'].includes(type) ? type : 'text'; if (type === 'number') input.step = 'any'; if (type === 'checkbox') input.checked = !!initial; else input.value = initial ?? ''; }
        if (type === 'episodes') input.placeholder = 'All episodes, or 3,7,17-19';
        field.append(input); grid.append(field);
      }
      if (operation === 'construction') { const label = text('label','Examples per scenario'); const rows = document.createElement('div'); rows.dataset.curationId = 'construction-counts'; label.append(rows); grid.append(label); }
      form.append(grid);
      if (operation === 'quality') drawQualityControls(form);
      else {
        const basic = new Set(['episodes','backend','selected_tags','ckpt_path','method','uncertainty_threshold']);
        const advanced = document.createElement('details'), settings = document.createElement('div'); settings.className = 'fields';
        for (const [key] of fields[operation]) if (!basic.has(key)) settings.append(form.elements[key].closest('label'));
        if (settings.children.length) { advanced.append(text('summary','Advanced settings'),settings); form.append(advanced); }
      }
      const row = document.createElement('div'); row.className = 'row'; const button = text('button', operationNames[operation]); button.type = 'submit'; button.dataset.command = operation; row.append(button, text('small','','operation-reason')); form.append(row); container.append(form);
      if (operation === 'labeling') form.elements.backend.onchange = buttons;
      form.addEventListener('submit', event => { event.preventDefault(); action(async () => {
        const parameters = {};
        for (const [key,,type] of fields[operation]) {
          const input = form.elements.namedItem(key), value = input.value.trim();
          if (type === 'checkbox') parameters[key] = input.checked;
          else if (value) parameters[key] = type === 'episodes' ? indices(value) : type === 'list' ? value.split(',').map(item => item.trim()).filter(Boolean) : type === 'number' ? Number(value) : value;
        }
        if (operation === 'construction') { parameters.per_scenario_counts = {}; $('construction-counts').querySelectorAll('input').forEach(input => { parameters.per_scenario_counts[input.name] = Number(input.value); }); }
        if (operation === 'quality') {
          const selected = [...form.querySelectorAll('[name=checks]:checked')].map(input => input.value);
          if (!selected.length) throw Error('Select at least one quality check.');
          if (context.quality_protocol === 1) {
            parameters.checks = selected;
            if (form.elements.qc_config.value.trim()) {
              try { parameters.qc_config = JSON.parse(form.elements.qc_config.value); }
              catch { throw Error('Thresholds and bounds must be valid JSON.'); }
            }
            if (selected.includes('c3')) { parameters.video_mode = form.elements.video_mode.value; parameters.video_workers = Number(form.elements.video_workers.value); }
          }
          if (form.elements.quality_profile.value) parameters.data_version = form.elements.quality_profile.value;
        }
        await submit(operation, parameters);
      }); });
    });
  }
  async function submit(operation, parameters = {}, extra = {}) {
    const allowed = (inputOperations[operation] || []).map(kind => `curation.${kind}`);
    const inputs = [...inputRuns].filter(run => context.runs.some(item => item.run === run && allowed.includes(item.operation)));
    const body = {target, operation:`curation.${operation}`, parameters, input_runs:inputs, ...extra};
    if (['construction','construction_preview'].includes(operation) && draft()) { body.workspace_id = draft().workspace_id; body.expected_revision = draft().revision; }
    const data = await api('/api/curation/jobs', body);
    notice(`Task submitted: ${operationNames[operation]}. Track progress in Jobs.`);
    if (operation === 'quality') root.querySelector('.quality-task-panel').open = true;
    await refreshJobs(); return data;
  }
  async function refresh() {
    const previous = target.dataset_version_id;
    const data = await api(`/api/curation/context?${query()}`);
    context = data; Object.assign(target, data.target);
    $('version').textContent = target.dataset_version_id ? `${data.episodes.length} episodes · Version ${target.dataset_version_id.slice(-8)}` : 'Register a version to run checks, review episodes or build datasets.';
    $('version').title = target.dataset_version_id || '';
    $('sync').textContent = target.dataset_version_id ? 'Update dataset version' : 'Register dataset version';
    $('overview').textContent = `${data.info.total_frames || 0} frames · ${data.info.fps || '—'} fps · ${data.info.codebase_version || 'Unknown format'}`;
    $('viewer').hidden = !data.viewer_url; if (data.viewer_url) $('viewer').href = data.viewer_url;
    $('report-viewer').hidden = !data.viewer_url || !['quality_flags','quality_report'].includes(activeTab); if (data.viewer_url) $('report-viewer').href = data.viewer_url;
    $('prepare-viewer').hidden = !!data.viewer_url || !data.can_edit || !['quality_flags','quality_report'].includes(activeTab);
    $('prepare-viewer').href = '/?' + new URLSearchParams({select:target.location_id ? `remote:${target.location_id}` : target.dataset_key,page:'preprocessing',tab:'cache'});
    const captionBase = target.location_id ? `/remote/${encodeURIComponent(target.location_id)}` : `/${target.dataset_key}`;
    $('analysis').href = captionBase + '/analysis';
    options($('workspace'), data.workspaces, item => item.workspace_id, item => `${item.owner} · ${item.status} · ${item.workspace_id.slice(-8)}`, 'Select a workspace');
    options($('episode'), data.episodes, item => String(item.episode_index), item => `Episode ${item.episode_index}`);
    drawResultOptions();
    options($('manifest'), data.manifests.filter(item => item.status === 'published'), item => item.manifest_id, item => `Version ${item.manifest_version} · ${item.reviewer || ''}`, 'Select a published manifest');
    $('episodes').replaceChildren();
    for (const row of data.episodes) {
      const tr = document.createElement('tr');
      const button = text('button',row.episode_index); button.onclick = () => { $('episode').value = String(row.episode_index); loadReview(); $('review-form').scrollIntoView({behavior:'smooth'}); };
      const td = document.createElement('td'); td.append(button); tr.append(td,text('td',(row.tasks || []).join(', ')),text('td',row.length),text('td',reviewFor(row.episode_index)?.decision || 'Undecided')); $('episodes').append(tr);
    }
    if (previous !== target.dataset_version_id) inputRuns = new Set();
    buttons();
    const summaries = await api('/api/curation/comparison-candidates');
    for (const id of ['compare-a','compare-b']) options($(id), summaries.runs, item => item.run, item => `${item.target.dataset_key} · ${new Date(item.created_at).toLocaleString()}`, 'Select a summary');
    buttons();
    await openLatestReport();
  }
  function reviewFor(index) { const uid = context?.episode_refs?.find(row => row.episode_index === index)?.episode_uid; return draft()?.decisions.find(item => item.episode_ref.episode_uid === uid); }
  function addPair(container, left, right, type = 'text') {
    const row = document.createElement('div'); row.className = 'tag-row';
    for (const value of [left,right]) { const input = document.createElement('input'); input.type = type; if (type === 'number') { input.step = 'any'; input.min = '0'; } input.value = value ?? ''; row.append(input); }
    const remove = text('button','Remove'); remove.type = 'button'; remove.onclick = () => { row.remove(); reviewStatus(); }; row.append(remove); $(container).append(row);
  }
  function loadReview(force = false) {
    if (!force && !allowReviewChange()) {
      if (reviewSelection) { $('workspace').value = reviewSelection.workspace; $('episode').value = reviewSelection.episode; }
      return false;
    }
    reviewRevision = draft()?.revision ?? null;
    const viewer = episodeViewerUrl(episode());
    $('review-viewer').hidden = !viewer; if (viewer) $('review-viewer').href = viewer;
    const form = $('review-form'); form.reset(); $('transitions').replaceChildren(); $('tags').replaceChildren();
    const uid = context?.episode_refs?.find(row => row.episode_index === episode())?.episode_uid;
    const decision = reviewFor(episode());
    form.elements.decision.value = decision?.decision || ''; form.elements.reason.value = decision?.reason || '';
    const recipe = draft()?.repair_recipes.find(item => item.op === 'pose_interpolate' && item.episode_refs.some(ref => ref.episode_uid === uid));
    if (recipe) {
      const repairs = recipe.params.repairs.map(({episode_uid, ...value}) => value).filter(value => value.episode_index === episode());
      form.elements.repair.value = 'pose_interpolate';
      form.elements.pose_repair.value = JSON.stringify(repairs);
      if (repairs.length) {form.elements.start_frame.value = repairs[0].start_frame; form.elements.end_frame.value = repairs[0].end_frame;}
    }
    const fields = draft()?.annotation_patches.find(item => item.episode_ref.episode_uid === uid)?.fields || {};
    form.elements.task.value = fields.task || '';
    (fields.subtask_transitions || []).forEach(item => addPair('transitions',item.time,item.state,'number'));
    Object.entries(fields.tags || {}).forEach(([key,value]) => addPair('tags',key,typeof value === 'object' ? JSON.stringify(value) : value));
    const box = fields.first_frame_bbox?.selected?.bbox;
    if (box) ['left','top','right','bottom'].forEach(key => { form.elements[key].value = box[key]; });
    [...$('episodes').children].forEach((row,index) => { row.lastElementChild.textContent = reviewFor(context.episodes[index].episode_index)?.decision || 'Undecided'; });
    savedReview = reviewValue();
    reviewSelection = {workspace: $('workspace').value, episode: $('episode').value};
    reviewStatus();
    buttons();
    return true;
  }
  async function saveReview(event) {
    event.preventDefault(); await action(async () => {
      const form = $('review-form'), data = new FormData(form), fields = {}, body = {expected_revision:reviewRevision, decision:data.get('decision'), reason:data.get('reason')};
      if (data.get('reason').trim() && !data.get('decision')) throw new Error('Choose a review decision to save a reason, or clear the reason.');
      if (data.get('task').trim()) fields.task = data.get('task').trim();
      const transitions = [...$('transitions').children].map(row => ({time:Number(row.children[0].value),state:Number(row.children[1].value)}));
      if (transitions.length) fields.subtask_transitions = transitions;
      if ($('tags').children.length) { fields.tags = {}; for (const row of $('tags').children) { const key = row.children[0].value.trim(), raw = row.children[1].value; if (!key) throw new Error('Tag name is required.'); try { fields.tags[key] = JSON.parse(raw); } catch { fields.tags[key] = raw; } } }
      if (data.get('left') !== '') { const bbox = Object.fromEntries(['left','top','right','bottom'].map(key => [key,Number(data.get(key))])); fields.first_frame_bbox = {selected:{bbox},selected_target:{bbox},source:'reviewed'}; }
      body.fields = fields; body.clear_fields = ['task','tags','first_frame_bbox','subtask_transitions'].filter(key => !(key in fields));
      if (data.get('repair') === 'trim') body.repair = {op:'trim',params:{start_frame:Number(data.get('start_frame')),end_frame:Number(data.get('end_frame'))}};
      if (data.get('repair') === 'value_edit') body.repair = {op:'value_edit',params:{edits:[{field:data.get('signal'),dimension:Number(data.get('dimension')),value:Number(data.get('value'))}]}};
      if (data.get('repair') === 'pose_interpolate') {
        const pose = JSON.parse(data.get('pose_repair') || '{}');
        const intervals = Array.isArray(pose) ? pose : [pose];
        body.repair = {op:'pose_interpolate',params:{repairs:intervals.map(value => ({...value,episode_index:Number(episode()),start_frame:value.start_frame ?? Number(data.get('start_frame')),end_frame:value.end_frame ?? Number(data.get('end_frame'))}))}};
      }
      if (data.get('repair') === 'clear') body.repair = null;
      await api(`/api/curation/drafts/${draft().workspace_id}/episodes/${episode()}`,body,'PATCH');
      savedReview = reviewValue(); reviewStatus();
      await refresh(); loadReview(true); notice('Episode review saved.');
    });
  }
  async function loadResult() {
    if (!$('result').value) return;
    const run = $('result').value, request = ++resultRequest;
    const loaded = await api(`/api/curation/runs/${run}`);
    if (request !== resultRequest || $('result').value !== run) return;
    result = loaded;
    $('result-body').replaceChildren(); $('artifacts').replaceChildren();
    if (result?.operation === 'curation.quality' && target.location_id) {
      const link = text('a','Ask AI about this report');
      link.href = '/assistant?' + new URLSearchParams({type:'quality',target_id:result.run,location_id:target.location_id});
      link.onclick = event => {if (window.openPlatformAssistant) {event.preventDefault(); window.openPlatformAssistant({type:'quality',targetId:result.run,locationId:target.location_id});}};
      $('result-body').append(link);
    }
    const rows = Object.values(result.labels || {}).map(item => [item.episode_index,item.task || '',item.selected ? 'Target selected' : 'No target selected']);
    Object.values(result.tags || {}).forEach(item => rows.push([item.episode_index,'Tags',Object.entries(item.tags || {}).map(([key,value]) => `${key}: ${JSON.stringify(value)}`).join(', ')]));
    if (result.operation === 'curation.quality') drawQualityResult();
    Object.entries(result.result?.transitions || {}).forEach(([index,items]) => rows.push([index,'Stages',items.map(item => `${item.time}s → ${item.state}`).join(', ')]));
    if (rows.length) { const table = document.createElement('table'); for (const values of rows) { const tr = document.createElement('tr'); values.forEach(value => tr.append(text('td',value))); table.append(tr); } $('result-body').append(table); }
    for (const filename of result.artifacts) {
      const url = `/api/curation/runs/${result.run}/artifacts/${filename.split('/').map(encodeURIComponent).join('/')}`;
      const link = text('a',filename.split('/').pop()); link.href = url; link.target = '_blank'; link.rel = 'noopener'; $('artifacts').append(link);
      if (/\.(png|jpg|jpeg)$/i.test(filename)) { const image = document.createElement('img'); image.src = url; image.alt = 'Labeling or tagging preview'; image.className = 'result-media'; $('result-body').append(image); }
    }
    if (result.result?.skipped_episodes?.length) $('result-body').append(text('p',`No stages generated for episodes: ${result.result.skipped_episodes.join(', ')}. Check episode duration and the selected stage policy.`));
    if (result.points?.length) drawEmbedding(result.points);
    if (result.result?.scenarios) {
      $('construction-counts').replaceChildren();
      for (const [name,item] of Object.entries(result.result.scenarios)) { const label = text('label',`${name.replaceAll('_',' ')} (${item.candidate_count} candidates)`), input = document.createElement('input'); input.name = name; input.type = 'number'; input.min = '0'; input.value = '0'; label.append(input); $('construction-counts').append(label); }
    }
    updateResultVisibility(); notice('Result loaded.');
  }
  function episodeViewerUrl(index, frame = 0, time = null) {
    if (!context?.viewer_url) return '';
    try {
      const url = new URL(context.viewer_url, window.location.origin);
      if (url.origin !== window.location.origin || !/\/episode_\d+\/?$/.test(url.pathname)) return '';
      url.pathname = url.pathname.replace(/\/episode_\d+\/?$/, `/episode_${index}`);
      url.search = ''; url.hash = '';
      const fps = Number(context.info?.fps);
      const seconds = time != null && Number.isFinite(Number(time)) ? Number(time) : fps > 0 ? frame / fps : 0;
      url.searchParams.set('t', String(Math.max(0, seconds)));
      url.searchParams.set('quality_review', '1');
      return url.pathname + url.search;
    } catch { return ''; }
  }
  function drawQualityResult() {
    const container = document.createElement('div'), filters = document.createElement('div'); filters.className = 'row';
    const issues = Object.entries(result.quality.flag_reasons || {}).flatMap(([index,reasons]) => reasons.map(issue => ({...issue,episode:Number(index),detector:qualityDetector(issue)})));
    const coverage = result.quality.coverage || [];
    drawQualityReport(issues, coverage);
    const selectors = {};
    for (const [key,label,values] of [
      ['detector','Check',[...issues,...coverage].map(item => item.detector)],
      ['episode','Episode',[...issues,...coverage].map(item => String(item.episode))],
      ['stream','Signal / camera',issues.map(item => item.camera || item.field).concat(coverage.map(item => item.camera))]
    ]) {
      const field = text('label',label), select = document.createElement('select'); select.dataset.qualityFilter = key;
      options(select,[...new Set(values.filter(value => value != null))].sort(),value => value,value => key === 'detector' ? qualityNames[value] || value : value,{detector:'All checks',episode:'All episodes',stream:'All signals / cameras'}[key]); selectors[key] = select; field.append(select); filters.append(field);
    }
    const body = document.createElement('div');
    const matches = item => (!selectors.detector.value || selectors.detector.value === item.detector) && (!selectors.episode.value || selectors.episode.value === String(item.episode)) && (!selectors.stream.value || selectors.stream.value === (item.camera || item.field));
    const draw = () => {
      body.replaceChildren();
      const table = document.createElement('table'), heading = document.createElement('tr');
      ['Episode','Check','Signal / camera','Severity','Frame range','Evidence','Review'].forEach(label => heading.append(text('th',label))); table.append(heading);
      for (const item of issues.filter(matches)) {
        const row = document.createElement('tr'), cell = document.createElement('td'), button = text('button',`Episode ${item.episode}`);
        button.type = 'button'; button.onclick = () => {
          if (!allowReviewChange()) return;
          if (activeTab === 'quality_report') root.dispatchEvent(new CustomEvent('curation-review-episode', {bubbles:true}));
          $('episode').value = String(item.episode); loadReview(true);
          requestAnimationFrame(() => root.querySelector('.quality-review').scrollIntoView({behavior:'smooth'}));
        }; cell.append(button); row.append(cell);
        [qualityNames[item.detector] || item.detector,`${item.camera || item.field || '—'}${item.dimension != null ? '['+item.dimension+']' : ''}`,item.severity || 'issue',JSON.stringify(item.frame_intervals || item.frames || [])].forEach(value => row.append(text('td',value)));
        const evidence = text('td',item.message || item.reason || item.code || 'Quality issue'), details = document.createElement('details');
        details.append(text('summary','Measurements'),text('pre',JSON.stringify({metrics:item.metrics || {},time_intervals:item.time_intervals || []},null,2))); evidence.append(details); row.append(evidence);
        const review = document.createElement('td'), intervals = item.frame_intervals?.length ? item.frame_intervals : (item.frames || []).map(frame => [frame,frame+1]);
        const ranges = intervals.length ? intervals : [[0,1]], first = ranges[0][0];
        const url = episodeViewerUrl(item.episode, first, item.time_intervals?.length === ranges.length ? item.time_intervals[0][0] : null);
        if (url) {
          const link = text('a','Open in Viewer'); link.href = url; link.target = '_blank'; link.rel = 'noopener';
          if (ranges.length > 1) {
            const select = document.createElement('select'); select.setAttribute('aria-label', `Candidate range for episode ${item.episode}`);
            ranges.forEach(([start,end],index) => { const option = text('option',`Frames ${start}–${end-1}`); option.value = index; select.append(option); });
            select.onchange = () => { const i = Number(select.value); link.href = episodeViewerUrl(item.episode,ranges[i][0],item.time_intervals?.length === ranges.length ? item.time_intervals[i][0] : null); };
            review.append(select);
          }
          review.append(link);
        } else review.append(text('span','Prepare Viewer cache to inspect frames.','muted'));
        row.append(review); table.append(row);
      }
      const issueScroll = document.createElement('div'); issueScroll.className = 'scroll'; issueScroll.append(table);
      body.append(text('h3','Detected issues'),text('p','Candidates need human review; they are not automatic exclusions. Open the frame in Viewer, then select the episode here and save Keep, Exclude or Needs review in a draft. Frame intervals use [start, end): the end frame is excluded.','muted'),issueScroll);
      if (table.rows.length === 1) body.append(text('p','No detected issues match these filters. Check media coverage below for incomplete checks.','muted'));
      if (!coverage.length) return;
      const media = document.createElement('table'), mediaHeading = document.createElement('tr');
      ['Episode','Camera','Status','Coverage','Checked frame ranges'].forEach(label => mediaHeading.append(text('th',label))); media.append(mediaHeading);
      for (const item of coverage.filter(matches)) {
        const row = document.createElement('tr');
        [item.episode,item.camera,item.status,`${item.mode}: ${item.checked_frames}/${item.expected_frames} frames (${(item.coverage_fraction*100).toFixed(1)}%)`,JSON.stringify(item.checked_intervals || [])].forEach(value => row.append(text('td',value))); media.append(row);
      }
      const mediaScroll = document.createElement('div'); mediaScroll.className = 'scroll'; mediaScroll.append(media);
      body.append(text('h3','Media coverage'),text('p','Sampled coverage leaves unchecked frames. Incomplete or failed checks do not mean the media passed.','muted'),mediaScroll);
      if (media.rows.length === 1) body.append(text('p','No media coverage matches these filters.','muted'));
    };
    Object.values(selectors).forEach(select => { select.onchange = draw; });
    container.append(text('p',`${new Set(issues.map(item => item.episode)).size} flagged episodes · ${issues.length} detected issues · ${coverage.length} camera coverage records`,'quality-summary'),filters,body); $('result-body').append(container); draw();
  }
  function drawQualityReport(issues, coverage) {
    const summary = result.result?.summary || result.quality.summary || {};
    const info = context.info, report = document.createElement('section'); report.className = 'data-report';
    const metrics = document.createElement('div'); metrics.className = 'report-metrics';
    const flagged = new Set(issues.map(item => item.episode)).size;
    const measured = coverage.reduce((sum,item) => sum + (item.checked_frames || 0),0);
    const expected = coverage.reduce((sum,item) => sum + (item.expected_frames || 0),0);
    const incomplete = coverage.filter(item => ['error','incomplete'].includes(item.status)).length;
    for (const [label,value] of [
      ['Dataset episodes',info.total_episodes ?? context.episodes.length],
      ['Dataset frames',info.total_frames ?? '—'],
      ['Duration',info.fps > 0 ? `${((info.total_frames || 0)/info.fps/3600).toFixed(2)} h` : '—'],
      ['Episodes checked',summary.episodes_scanned ?? result.result?.total_episodes ?? 'Unknown'],
      ['Flagged episodes',flagged],['Detected issues',issues.length]
    ]) { const card = document.createElement('div'); card.append(text('small',label),text('strong',typeof value === 'number' ? value.toLocaleString() : value)); metrics.append(card); }
    report.append(metrics,text('p',`Saved ${new Date(result.created_at).toLocaleString()} · ${info.codebase_version || 'Unknown format'} · ${info.robot_type || 'Unknown robot'} · ${info.fps || '—'} fps`,'muted'));
    report.append(text('p',`Checks in this run: ${(summary.checks || []).map(key => qualityNames[key] || key).join(', ') || 'Not recorded'}.`));
    report.append(text('p',coverage.length ? `Video coverage: ${measured.toLocaleString()} / ${expected.toLocaleString()} camera frames (${expected ? (measured/expected*100).toFixed(1) : '0'}%). ${incomplete} incomplete or failed streams. ${coverage.some(item => item.mode === 'sampled') ? 'Sampled frames; unchecked frames are not certified.' : ''}` : 'No video coverage recorded in this result. Video checks have not been established.','muted'));
    report.append(text('p','Flags are review candidates, not exclusions or training acceptance. Unchecked episodes and checks are not counted as passing. Saved evidence may also include earlier input results.','muted'));
    $('result-body').append(report);
  }
  function drawEmbedding(points) {
    const svg = $('embedding-plot'); svg.hidden = false; svg.replaceChildren(); svg.setAttribute('viewBox','0 0 800 320');
    const xs = points.map(point => point.x), ys = points.map(point => point.y), minX = Math.min(...xs), minY = Math.min(...ys), dx = Math.max(...xs)-minX || 1, dy = Math.max(...ys)-minY || 1;
    points.forEach(point => { const dot = document.createElementNS('http://www.w3.org/2000/svg','circle'); dot.setAttribute('cx',20+(point.x-minX)/dx*760); dot.setAttribute('cy',300-(point.y-minY)/dy*280); dot.setAttribute('r','4'); dot.setAttribute('fill','var(--accent)'); dot.setAttribute('tabindex','0'); const title = document.createElementNS(dot.namespaceURI,'title'); title.textContent = `Episode ${point.episode_index}: ${point.task}`; dot.append(title); const choose = () => { $('episode').value = String(point.episode_index); loadReview(); }; dot.onclick = choose; dot.onkeydown = event => { if (event.key === 'Enter') choose(); }; svg.append(dot); });
  }
  async function refreshJobs() {
    const data = await api('/api/control/jobs?limit=100');
    const oldActive = jobs.some(job => ['queued','running','cancel_requested','interrupted'].includes(job.status));
    jobs = data.jobs.filter(job => target.location_id ? job.location_id === target.location_id : job.options?.target?.dataset_key === target.dataset_key && !job.options?.target?.location_id);
    $('jobs').replaceChildren();
    for (const job of jobs) {
      const row = document.createElement('div'); row.className = 'card'; row.dataset.curationOperation = job.operation; row.hidden = page === 'quality' && job.operation !== 'curation.quality'; row.append(text('strong',`${operationNames[job.operation.split('.')[1]] || job.operation} · ${job.status}`),text('p',job.error || job.progress?.message || job.phase || ''));
      if (job.status === 'queued') row.append(text('small',`${job.queue_ahead || 0} task(s) ahead`));
      if (job.progress?.total > 0 && job.status === 'running') { const bar = document.createElement('div'); bar.className = 'progress'; const fill = document.createElement('span'); fill.style.width = `${Math.min(100,Math.max(0,100*(job.progress.current || 0)/job.progress.total))}%`; bar.append(fill); row.append(bar,text('small',`${job.progress.current || 0} / ${job.progress.total}`)); }
      row.append(text('small',`Started: ${job.started_at ? new Date(job.started_at).toLocaleString() : 'Waiting'} · Updated: ${new Date(job.updated_at).toLocaleString()}`));
      if (job.result?.viewer_url) { const link = text('a','Open output dataset'); link.href = job.result.viewer_url; row.append(link); }
      for (const [name,label] of [['cancel','Cancel'],['retry','Retry']]) {
        const permitted = job.available_actions?.[name];
        if (permitted === true || permitted?.allowed || Array.isArray(job.available_actions) && job.available_actions.includes(name)) { const button = text('button',label); button.onclick = () => action(async () => { await api(`/api/control/jobs/${job.job_id}/${name}`,{}); await refreshJobs(); }); row.append(button); }
      }
      $('jobs').append(row);
    }
    const active = jobs.some(job => ['queued','running','cancel_requested','interrupted'].includes(job.status));
    if (oldActive && !active) { delete target.dataset_version_id; await refresh(); }
    clearTimeout(timer); if (active && !destroyed) timer = setTimeout(() => refreshJobs().catch(error => { notice(error.message,true); if (!destroyed) timer=setTimeout(() => refreshJobs().catch(error => notice(error.message,true)),10000); }),4000);
  }
  function bind() {
    root.querySelectorAll('[data-page]').forEach(button => { button.onclick = () => selectPage(button.dataset.page); });
    $('refresh').onclick = () => action(async () => { delete target.dataset_version_id; await refresh(); await refreshJobs(); notice('Updated. Unsaved episode fields have been kept.'); });
    $('sync').onclick = () => action(() => submit('snapshot'));
    $('import-sidecars').onclick = () => reviewAction(async () => { await api(`/api/curation/drafts/${draft().workspace_id}/import-sidecars`,{target,expected_revision:draft().revision}); await refresh(); loadReview(true); notice('Existing review imported without changing source files.'); });
    $('reload-review').onclick = () => loadReview(); $('workspace').onchange = () => loadReview(); $('episode').onchange = () => loadReview();
    $('new-draft').onclick = () => reviewAction(async () => { const data = await api('/api/curation/drafts',{target}); await refresh(); $('workspace').value = data.workspace.workspace_id; loadReview(true); notice('Draft created.'); });
    $('add-filter').onclick = () => addPair('cohort-filters','',''); $('add-group').onclick = () => addPair('recipe-groups','','');
    $('add-transition').onclick = () => { addPair('transitions','','','number'); reviewStatus(); }; $('add-tag').onclick = () => { addPair('tags','',''); reviewStatus(); };
    $('review-form').onsubmit = saveReview;
    $('review-form').addEventListener('input', reviewStatus);
    $('review-form').addEventListener('change', reviewStatus);
    window.addEventListener('data-platform:before-dataset-change', beforeDatasetChange);
    window.addEventListener('beforeunload', beforeUnload);
    $('load-result').onclick = () => action(loadResult);
    $('result').onchange = () => { resultRequest++; updateResultVisibility(); if (activeTab === 'quality_report') loadResult().catch(error => notice(error.message, true)); };
    $('use-result').onchange = () => { const run = $('result').value; if (!run || $('use-result').disabled) return; if ($('use-result').checked) inputRuns.add(run); else inputRuns.delete(run); updateResultVisibility(); };
    $('accept-result').onclick = () => reviewAction(async () => { if (!result || result.run !== $('result').value || !['curation.stage','curation.labeling','curation.tagging'].includes(result.operation)) throw new Error('Open the selected annotation result first.'); await api(`/api/curation/drafts/${draft().workspace_id}/accept/${result.run}/${episode()}`,{expected_revision:draft().revision}); await refresh(); loadReview(true); notice('Result accepted into the draft.'); });
    $('publish').onclick = () => {
      if (reviewDirty()) { notice('Save or reload your episode review before publishing the draft.', true); return; }
      action(async () => { await api(`/api/curation/drafts/${draft().workspace_id}/publish`,{target, expected_revision:draft().revision,reason:$('publish-reason').value}); notice('Source validation queued. The draft will publish only if its revision and source still match.'); await refreshJobs(); });
    };
    $('build-form').onsubmit = event => { event.preventDefault(); action(async () => { const values = new FormData(event.target); await submit('materialize',{workers:Number(values.get('workers'))},{manifest_id:$('manifest').value,out_root:values.get('out_root')}); }); };
    $('manifest').onchange = buttons;
    for (const id of ['compare-a','compare-b','caption-run']) $(id).onchange = buttons;
    $('profile').onclick = () => action(async () => { const data = await api('/api/curation/dataset-profiles',{dataset_version_id:target.dataset_version_id}); $('profiles').textContent = `Profile created: ${data.dataset_profile?.dataset_profile_id || data.profile?.profile_id || 'ready'}`; notice('Dataset profile created.'); });
    $('recipe-form').onsubmit = event => { event.preventDefault(); reviewAction(async () => {
      const form = new FormData(event.target), body = {name:form.get('name'),base_dataset_version_id:target.dataset_version_id,include_episode_ids:indices(form.get('include')) || [],exclude_episode_ids:indices(form.get('exclude')) || [],random_seed:Number(form.get('seed')),deduplication:{mode:form.get('dedup') ? 'exact' : 'none'}};
      if (form.get('task_contains').trim()) body.cohort_query = {task_contains:form.get('task_contains').trim()};
      if (form.get('count')) { const requirement = await api('/api/curation/requirements',{name:form.get('name'),target_episode_count:Number(form.get('count'))}); body.requirement_id = requirement.requirement.requirement_id; body.composition = {max_episodes:Number(form.get('count'))}; }
      if ($('cohort-filters').children.length) {
        body.cohort_query ||= {}; body.cohort_query.metadata = {};
        for (const row of $('cohort-filters').children) {
          const key = row.children[0].value.trim(), values = row.children[1].value.split(',').map(value => value.trim()).filter(Boolean);
          if (!key || !values.length) throw new Error('Each metadata filter needs a dimension and accepted values.');
          body.cohort_query.metadata[key] = values;
        }
      }
      if (form.get('group_by')) {
        const weights = {};
        for (const row of $('recipe-groups').children) {
          const key = row.children[0].value.trim(), weight = Number(row.children[1].value);
          if (!key || !Number.isFinite(weight) || weight < 0 || (form.get('group_mode') === 'target_counts' && !Number.isInteger(weight))) throw new Error('Each group needs a label and a non-negative count or weight.');
          if (key in weights) throw new Error('Group labels must be unique.');
          weights[key] = weight;
        }
        if (!Object.keys(weights).length) throw new Error('Add at least one sampling group.');
        body.composition = {group_by:form.get('group_by'),[form.get('group_mode')]:weights};
        if (form.get('group_mode') === 'ratios') {
          if (!Number(form.get('count'))) throw new Error('Weighted sampling needs a target episode count.');
          body.composition.target_episode_count = Number(form.get('count'));
        }
      }
      const data = await api('/api/curation/recipes',body); const compiled = await api(`/api/curation/recipes/${data.recipe.recipe_id}/compile`,{}); await refresh(); $('workspace').value = compiled.workspace.workspace_id; loadReview(true); notice('Recipe compiled into a review workspace.');
    }); };
    $('compare').onclick = () => action(async () => { const data = await api('/api/curation/compare?' + new URLSearchParams({run_a:$('compare-a').value,run_b:$('compare-b').value})); $('comparison').replaceChildren(); const table = document.createElement('table'); for (const [field,label] of [['total_episodes','Episodes'],['total_frames','Frames'],['fps','FPS']]) { const tr=document.createElement('tr'); tr.append(text('td',label),text('td',data.summary.a.metadata[field]),text('td',data.summary.b.metadata[field])); table.append(tr); } $('comparison').append(table); for (const side of ['a','b']) { $('comparison').append(text('h3',data[side].target.dataset_key)); for (const [key,label] of [['scenarios','Scenarios'],['action','Action statistics'],['tags','Tags'],['vocab','Vocabulary']]) { const detail = document.createElement('details'); detail.append(text('summary',label),text('pre',JSON.stringify(data.summary[side][key],null,2))); $('comparison').append(detail); } $('comparison').append(text('p',`Embedding points: ${(data.summary[side].embedding?.points || data.summary[side].embedding || []).length || 0}`)); } notice('Comparison ready.'); });
    const captionApi = () => target.location_id ? `/api/control/locations/${encodeURIComponent(target.location_id)}/temporal-caption` : `/api/temporal-caption/${target.dataset_key}`;
    $('load-caption-runs').onclick = () => action(async () => { const data = await api(captionApi()); captionRuns = data.runs || []; options($('caption-run'),captionRuns,item => `${item.run}:${item.variant}`,item => `Episode ${item.episode_index} · ${item.variant} · ${item.run}`,'Select a caption result'); notice('Caption results loaded.'); });
    $('accept-caption').onclick = () => action(async () => { const value = captionRuns.find(item => `${item.run}:${item.variant}` === $('caption-run').value); if (!value) throw new Error('Select a caption result.'); await api(`/api/curation/drafts/${draft().workspace_id}/caption-evidence`,{target,expected_revision:draft().revision,run:value.run,variant:value.variant}); await refresh(); notice('Caption evidence saved in the draft.'); });
  }
  const captionBase = target.location_id ? `/remote/${encodeURIComponent(target.location_id)}` : `/${target.dataset_key}`;
  const captionApiBase = target.location_id ? `/api/control/locations/${encodeURIComponent(target.location_id)}/temporal-caption` : `/api/temporal-caption/${target.dataset_key}`;
  captionController = window.createTemporalCaptionTask($('caption-task'), {api:captionApiBase,reviewUrl:captionBase+'/temporal-caption/review'});
  drawForms(); bind();
  refresh().then(async () => {
    loadReview(true); await refreshJobs();
    const requested = new URLSearchParams(location.search).get('run');
    if (requested && [...$('result').options].some(option => option.value === requested)) { $('result').value = requested; await loadResult(); }
  }).catch(error => notice(error.message,true));
  return {selectPage, destroy() {
    destroyed = true; clearTimeout(timer); captionController?.destroy();
    window.removeEventListener('data-platform:before-dataset-change', beforeDatasetChange);
    window.removeEventListener('beforeunload', beforeUnload);
  }};
};

// Keep one controller per dataset, preserving unsaved fields when switching tabs.
window.syncCurationWorkspace = (() => {
  const mounted = new WeakMap();
  return (host, target, page, tab, enabled) => {
    const key = JSON.stringify(target);
    let state = mounted.get(host);
    if (state && state.key !== key) {
      state.controller.destroy(); host.replaceChildren(); mounted.delete(host); state = null;
    }
    if (!enabled) return;
    if (!state) {
      const root = document.createElement('div');
      root.append(document.getElementById('curation-workspace-template').content.cloneNode(true));
      host.replaceChildren(root);
      state = {key, controller: window.createCurationWorkspace(root, target)};
      mounted.set(host, state);
    }
    state.controller.selectPage(page, tab);
  };
})();
