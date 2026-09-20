/* Shared Curation client: execution location is a property of the selected target. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const target = {...window.curationTarget};
  let context = null, result = null, busy = false, jobs = [], page = new URLSearchParams(location.search).get('page') || 'annotation';
  let inputRuns = new Set(), captionRuns = [], timer = null, reviewRevision = null;
  const pending = JSON.parse(sessionStorage.getItem('curation-deliveries') || '{}');
  const text = (tag, value, className) => { const node = document.createElement(tag); node.textContent = value; if (className) node.className = className; return node; };
  const notice = (message, error = false) => { $('notice').hidden = !message; $('notice').textContent = message; $('notice').className = error ? 'error' : ''; };
  const query = () => new URLSearchParams(Object.entries(target).filter(([, value]) => value != null && value !== '')).toString();
  const draft = () => context?.workspaces.find(item => item.workspace_id === $('workspace').value);
  const episode = () => Number($('episode').value);
  const operationNames = {snapshot:'Synchronize version', validate_source:'Validate source', stage:'Compute stages', quality:'Detect quality issues', labeling:'Run object labeling', tagging:'Run auto-tagging', embedding:'Compute embeddings', project:'Project embeddings', compare_summary:'Prepare comparison summary', construction_preview:'Preview construction', construction:'Generate data', materialize:'Build dataset'};
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
    document.querySelectorAll('[data-command]').forEach(button => {
      const capability = context.operations[`curation.${button.dataset.command}`];
      let blocked = capability?.reason || '';
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
    for (const id of ['publish','accept-result','accept-caption','import-sidecars']) $(id).disabled = busy || !context.can_edit || !draft() || draft().status !== 'open';
    $('review-form').querySelector('button[type=submit]').disabled = busy || !context.can_edit || !draft() || draft().status !== 'open';
    $('build-form').querySelector('button[type=submit]').disabled = busy || !context.operations['curation.materialize']?.can_execute || !$('manifest').value;
    $('recipe-form').querySelector('button[type=submit]').disabled = busy || !context.can_edit || !target.dataset_version_id;
    $('sync').disabled = busy || !context.operations['curation.snapshot']?.can_execute;
    $('sync').title = context.operations['curation.snapshot']?.reason || '';
    $('draft-state').textContent = draft() ? `${draft().status} · revision ${draft().revision}` : 'Select or create a draft';
  }
  function selectPage(value) {
    page = ['explore','quality','annotation','dataset_build'].includes(value) ? value : 'annotation';
    document.querySelectorAll('[data-section]').forEach(item => { item.hidden = item.dataset.section !== page; });
    document.querySelectorAll('[data-page]').forEach(item => item.setAttribute('aria-selected', item.dataset.page === page));
    const url = new URL(location.href); url.searchParams.set('page', page); history.replaceState(null, '', url);
  }
  function drawForms() {
    document.querySelectorAll('[data-operation]').forEach(container => {
      const operation = container.dataset.operation, form = document.createElement('form'), grid = document.createElement('div'); grid.className = 'fields';
      for (const [key,label,type,initial] of fields[operation]) {
        const field = text('label',label), input = document.createElement(type === 'select' ? 'select' : 'input'); input.name = key;
        if (type === 'select') initial.forEach(value => { const option = text('option', value.replaceAll('_',' ')); option.value = value; input.append(option); });
        else { input.type = ['number','checkbox'].includes(type) ? type : 'text'; if (type === 'number') input.step = 'any'; if (type === 'checkbox') input.checked = !!initial; else input.value = initial ?? ''; }
        if (type === 'episodes') input.placeholder = 'All episodes, or 3,7,17-19';
        field.append(input); grid.append(field);
      }
      if (operation === 'construction') { const label = text('label','Examples per scenario'); const rows = document.createElement('div'); rows.id = 'construction-counts'; label.append(rows); grid.append(label); }
      const row = document.createElement('div'); row.className = 'row'; const button = text('button', operationNames[operation]); button.type = 'submit'; button.dataset.command = operation; row.append(button, text('small','','operation-reason')); form.append(grid,row); container.append(form);
      if (operation === 'labeling') form.elements.backend.onchange = buttons;
      form.addEventListener('submit', event => { event.preventDefault(); action(async () => {
        const parameters = {};
        for (const [key,,type] of fields[operation]) {
          const input = form.elements.namedItem(key), value = input.value.trim();
          if (type === 'checkbox') parameters[key] = input.checked;
          else if (value) parameters[key] = type === 'episodes' ? indices(value) : type === 'list' ? value.split(',').map(item => item.trim()).filter(Boolean) : type === 'number' ? Number(value) : value;
        }
        if (operation === 'construction') { parameters.per_scenario_counts = {}; $('construction-counts').querySelectorAll('input').forEach(input => { parameters.per_scenario_counts[input.name] = Number(input.value); }); }
        await submit(operation, parameters);
      }); });
    });
  }
  async function submit(operation, parameters = {}, extra = {}) {
    const body = {target, operation:`curation.${operation}`, parameters, input_runs:[...inputRuns], ...extra};
    if (['construction','construction_preview'].includes(operation) && draft()) { body.workspace_id = draft().workspace_id; body.expected_revision = draft().revision; }
    const data = await api('/api/curation/jobs', body);
    notice(`Task accepted: ${operationNames[operation]}. Progress is shown in Tasks.`);
    await refreshJobs(); return data;
  }
  async function refresh() {
    const previous = target.dataset_version_id;
    const data = await api(`/api/curation/context?${query()}`);
    context = data; Object.assign(target, data.target);
    $('dataset-name').textContent = `${target.dataset_key.split('/').pop().replace(/-[a-f0-9]{8}$/, '')} · ${data.execution_node}`;
    $('version').textContent = target.dataset_version_id ? `Version ${target.dataset_version_id} · ${data.episodes.length} episodes` : 'Synchronize a version to begin Curation.';
    $('overview').textContent = `${data.info.total_frames || 0} frames · ${data.info.fps || '—'} fps · ${data.info.codebase_version || 'Unknown format'}`;
    $('viewer').hidden = !data.viewer_url; if (data.viewer_url) $('viewer').href = data.viewer_url;
    const captionBase = target.location_id ? `/remote/${encodeURIComponent(target.location_id)}` : `/${target.dataset_key}`;
    $('caption').href = captionBase + '/temporal-caption'; $('analysis').href = captionBase + '/analysis';
    options($('workspace'), data.workspaces, item => item.workspace_id, item => `${item.owner} · ${item.status} · ${item.workspace_id.slice(-8)}`, 'Select a workspace');
    options($('episode'), data.episodes, item => String(item.episode_index), item => `Episode ${item.episode_index}`);
    options($('result'), data.runs.filter(item => !['curation.snapshot','curation.validate_source'].includes(item.operation)), item => item.run, item => `${operationNames[item.operation.split('.')[1]]} · ${new Date(item.created_at).toLocaleString()}`, 'Select a result');
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
  }
  function reviewFor(index) { const uid = context?.episode_refs?.find(row => row.episode_index === index)?.episode_uid; return draft()?.decisions.find(item => item.episode_ref.episode_uid === uid); }
  function addPair(container, left, right, type = 'text') {
    const row = document.createElement('div'); row.className = 'tag-row';
    for (const value of [left,right]) { const input = document.createElement('input'); input.type = type; if (type === 'number') { input.step = 'any'; input.min = '0'; } input.value = value ?? ''; row.append(input); }
    const remove = text('button','Remove'); remove.type = 'button'; remove.onclick = () => row.remove(); row.append(remove); $(container).append(row);
  }
  function loadReview() {
    reviewRevision = draft()?.revision ?? null;
    const form = $('review-form'); form.reset(); $('transitions').replaceChildren(); $('tags').replaceChildren();
    const uid = context?.episode_refs?.find(row => row.episode_index === episode())?.episode_uid;
    const decision = reviewFor(episode());
    form.elements.decision.value = decision?.decision || ''; form.elements.reason.value = decision?.reason || '';
    const fields = draft()?.annotation_patches.find(item => item.episode_ref.episode_uid === uid)?.fields || {};
    form.elements.task.value = fields.task || '';
    (fields.subtask_transitions || []).forEach(item => addPair('transitions',item.time,item.state,'number'));
    Object.entries(fields.tags || {}).forEach(([key,value]) => addPair('tags',key,typeof value === 'object' ? JSON.stringify(value) : value));
    const box = fields.first_frame_bbox?.selected?.bbox;
    if (box) ['left','top','right','bottom'].forEach(key => { form.elements[key].value = box[key]; });
    [...$('episodes').children].forEach((row,index) => { row.lastElementChild.textContent = reviewFor(context.episodes[index].episode_index)?.decision || 'Undecided'; });
    buttons();
  }
  async function saveReview(event) {
    event.preventDefault(); await action(async () => {
      const form = $('review-form'), data = new FormData(form), fields = {}, body = {expected_revision:reviewRevision, decision:data.get('decision'), reason:data.get('reason')};
      if (data.get('task').trim()) fields.task = data.get('task').trim();
      const transitions = [...$('transitions').children].map(row => ({time:Number(row.children[0].value),state:Number(row.children[1].value)}));
      if (transitions.length) fields.subtask_transitions = transitions;
      if ($('tags').children.length) { fields.tags = {}; for (const row of $('tags').children) { const key = row.children[0].value.trim(), raw = row.children[1].value; if (!key) throw new Error('Tag name is required.'); try { fields.tags[key] = JSON.parse(raw); } catch { fields.tags[key] = raw; } } }
      if (data.get('left') !== '') { const bbox = Object.fromEntries(['left','top','right','bottom'].map(key => [key,Number(data.get(key))])); fields.first_frame_bbox = {selected:{bbox},selected_target:{bbox},source:'reviewed'}; }
      body.fields = fields; body.clear_fields = ['task','tags','first_frame_bbox','subtask_transitions'].filter(key => !(key in fields));
      if (data.get('repair') === 'trim') body.repair = {op:'trim',params:{start_frame:Number(data.get('start_frame')),end_frame:Number(data.get('end_frame'))}};
      if (data.get('repair') === 'value_edit') body.repair = {op:'value_edit',params:{edits:[{field:data.get('signal'),dimension:Number(data.get('dimension')),value:Number(data.get('value'))}]}};
      if (data.get('repair') === 'clear') body.repair = null;
      await api(`/api/curation/drafts/${draft().workspace_id}/episodes/${episode()}`,body,'PATCH');
      await refresh(); loadReview(); notice('Episode review saved.');
    });
  }
  async function loadResult() {
    if (!$('result').value) return;
    result = await api(`/api/curation/runs/${$('result').value}`);
    $('result-body').replaceChildren(); $('artifacts').replaceChildren();
    const rows = Object.values(result.labels || {}).map(item => [item.episode_index,item.task || '',item.selected ? 'Target selected' : 'No target selected']);
    Object.values(result.tags || {}).forEach(item => rows.push([item.episode_index,'Tags',Object.entries(item.tags || {}).map(([key,value]) => `${key}: ${JSON.stringify(value)}`).join(', ')]));
    (result.quality?.flagged_episodes || []).forEach(index => rows.push([index,'Quality issue',(result.quality.flag_reasons?.[index] || []).map(reason => reason.message || reason.reason || reason.code || String(reason)).join(', ')]));
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
    $('use-result').checked = inputRuns.has(result.run); notice('Result loaded. Select an episode to review or accept its annotations.');
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
      const row = document.createElement('div'); row.className = 'card'; row.append(text('strong',`${operationNames[job.operation.split('.')[1]] || job.operation} · ${job.status}`),text('p',job.error || job.progress?.message || job.phase || ''));
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
    clearTimeout(timer); if (active) timer = setTimeout(() => refreshJobs().catch(error => { notice(error.message,true); timer=setTimeout(refreshJobs,10000); }),4000);
  }
  function bind() {
    document.querySelectorAll('[data-page]').forEach(button => { button.onclick = () => selectPage(button.dataset.page); });
    $('refresh').onclick = () => action(async () => { delete target.dataset_version_id; await refresh(); await refreshJobs(); notice('Updated. Unsaved episode fields have been kept.'); });
    $('sync').onclick = () => action(() => submit('snapshot'));
    $('import-sidecars').onclick = () => action(async () => { await api(`/api/curation/drafts/${draft().workspace_id}/import-sidecars`,{target,expected_revision:draft().revision}); await refresh(); loadReview(); notice('Existing review imported without changing source files.'); });
    $('reload-review').onclick = loadReview; $('workspace').onchange = loadReview; $('episode').onchange = loadReview;
    $('new-draft').onclick = () => action(async () => { const data = await api('/api/curation/drafts',{target}); await refresh(); $('workspace').value = data.workspace.workspace_id; loadReview(); notice('Draft created.'); });
    $('add-filter').onclick = () => addPair('cohort-filters','',''); $('add-group').onclick = () => addPair('recipe-groups','','');
    $('add-transition').onclick = () => addPair('transitions','','','number'); $('add-tag').onclick = () => addPair('tags','','');
    $('review-form').onsubmit = saveReview;
    $('load-result').onclick = () => action(loadResult);
    $('use-result').onchange = () => { const run = $('result').value; if (!run) return; if ($('use-result').checked) inputRuns.add(run); else inputRuns.delete(run); $('input-description').textContent = `${inputRuns.size} input result(s) selected`; };
    $('accept-result').onclick = () => action(async () => { if (!result) throw new Error('Open a generated result first.'); await api(`/api/curation/drafts/${draft().workspace_id}/accept/${result.run}/${episode()}`,{expected_revision:draft().revision}); await refresh(); loadReview(); notice('Result accepted into the draft.'); });
    $('publish').onclick = () => action(async () => { await api(`/api/curation/drafts/${draft().workspace_id}/publish`,{target, expected_revision:draft().revision,reason:$('publish-reason').value}); notice('Source validation queued. The draft will publish only if its revision and source still match.'); await refreshJobs(); });
    $('build-form').onsubmit = event => { event.preventDefault(); action(async () => { const values = new FormData(event.target); await submit('materialize',{workers:Number(values.get('workers'))},{manifest_id:$('manifest').value,out_root:values.get('out_root')}); }); };
    $('manifest').onchange = buttons;
    $('profile').onclick = () => action(async () => { const data = await api('/api/curation/dataset-profiles',{dataset_version_id:target.dataset_version_id}); $('profiles').textContent = `Profile created: ${data.dataset_profile?.dataset_profile_id || data.profile?.profile_id || 'ready'}`; notice('Dataset profile created.'); });
    $('recipe-form').onsubmit = event => { event.preventDefault(); action(async () => {
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
      const data = await api('/api/curation/recipes',body); const compiled = await api(`/api/curation/recipes/${data.recipe.recipe_id}/compile`,{}); await refresh(); $('workspace').value = compiled.workspace.workspace_id; loadReview(); notice('Recipe compiled into a review workspace.');
    }); };
    $('compare').onclick = () => action(async () => { const data = await api('/api/curation/compare?' + new URLSearchParams({run_a:$('compare-a').value,run_b:$('compare-b').value})); $('comparison').replaceChildren(); const table = document.createElement('table'); for (const [field,label] of [['total_episodes','Episodes'],['total_frames','Frames'],['fps','FPS']]) { const tr=document.createElement('tr'); tr.append(text('td',label),text('td',data.summary.a.metadata[field]),text('td',data.summary.b.metadata[field])); table.append(tr); } $('comparison').append(table); for (const side of ['a','b']) { $('comparison').append(text('h3',data[side].target.dataset_key)); for (const [key,label] of [['scenarios','Scenarios'],['action','Action statistics'],['tags','Tags'],['vocab','Vocabulary']]) { const detail = document.createElement('details'); detail.append(text('summary',label),text('pre',JSON.stringify(data.summary[side][key],null,2))); $('comparison').append(detail); } $('comparison').append(text('p',`Embedding points: ${(data.summary[side].embedding?.points || data.summary[side].embedding || []).length || 0}`)); } notice('Comparison ready.'); });
    const captionApi = () => target.location_id ? `/api/control/locations/${encodeURIComponent(target.location_id)}/temporal-caption` : `/api/temporal-caption/${target.dataset_key}`;
    $('load-caption-runs').onclick = () => action(async () => { const data = await api(captionApi()); captionRuns = data.runs || []; options($('caption-run'),captionRuns,item => `${item.run}:${item.variant}`,item => `Episode ${item.episode_index} · ${item.variant} · ${item.run}`,'Select a caption result'); notice('Caption results loaded.'); });
    $('accept-caption').onclick = () => action(async () => { const value = captionRuns.find(item => `${item.run}:${item.variant}` === $('caption-run').value); if (!value) throw new Error('Select a caption result.'); await api(`/api/curation/drafts/${draft().workspace_id}/caption-evidence`,{target,expected_revision:draft().revision,run:value.run,variant:value.variant}); await refresh(); notice('Caption evidence saved in the draft.'); });
  }
  drawForms(); bind(); selectPage(page);
  refresh().then(refreshJobs).catch(error => notice(error.message,true));
})();
