// Nonmodal, account-scoped workspace. Conversation targets stay pinned across navigation.
(() => {
  let owner = null, role = null, current = null, target = null, host = null, root = null, controller = null, editor = null, opened = false, width = 420;
  const key = () => `assistant-workspace:${location.origin}:${owner}`;
  const read = () => {try {return JSON.parse(sessionStorage.getItem(key()) || 'null');} catch (_) {return null;}};
  const save = () => {if (owner) try {sessionStorage.setItem(key(), JSON.stringify({target, opened, width}));} catch (_) {}};
  const api = async (path, method='GET', body) => {
    const response = await fetch(path, {method, credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
    const value = await response.json(); if (!response.ok) throw new Error(value.error || 'Request failed'); return value;
  };
  const $ = id => root?.getElementById(id);
  function display() {
    if (!host) return;
    $('assistant-dock').hidden = !opened; $('assistant-reopen').hidden = opened;
    document.body.classList.toggle('platform-assistant-open', opened);
    document.documentElement.style.setProperty('--assistant-width', width + 'px');
    $('assistant-dock').style.width = width + 'px';
    $('assistant-target').textContent = target?.locationId ? `${target.label || target.locationId} · ${target.type}` : 'Choose a dataset or restore a conversation';
    const changed = current?.locationId && (current.locationId !== target?.locationId || target?.type !== 'dataset');
    $('assistant-use-current').hidden = !changed;
    $('assistant-use-current').textContent = `Start chat about ${current?.label || 'current dataset'}`;
    $('assistant-pinned').hidden = !changed;
    save();
  }
  function choose(value) {
    target = {locationId:value.location_id, type:value.context?.type || 'dataset', targetId:value.context?.target_id || value.location_id, label:value.dataset || value.location_id};
    display(); return true;
  }
  async function navigate(href) {
    const url = new URL(href, location.origin); if (url.origin !== location.origin) return;
    const app = document.querySelector('[x-data]')?._x_dataStack?.[0];
    if (url.pathname === '/' && app?.selectPage && !url.searchParams.has('job_id') && !url.searchParams.has('dataset')) {
      await app.selectPage(url.searchParams.get('page') || 'datasets'); return;
    }
    save(); location.assign(url.href);
  }
  function create() {
    if (host) return;
    const template = document.getElementById('assistant-workspace-template'); if (!template) return;
    host = document.createElement('div'); host.id = 'platform-assistant-workspace'; root = host.attachShadow({mode:'open'});
    root.append(template.content.cloneNode(true)); document.body.append(host);
    $('assistant-close').onclick = () => {opened = false; display(); $('assistant-reopen').focus();};
    $('assistant-reopen').onclick = () => {opened = true; display(); $('ai-message').focus();};
    root.addEventListener('keydown', event => {if (event.key === 'Escape') {opened = false; display(); $('assistant-reopen').focus();}});
    $('assistant-use-current').onclick = () => window.openPlatformAssistant({...current, type:'dataset', targetId:current.locationId});
    $('assistant-width').value = width;
    $('assistant-width').oninput = event => {width = Number(event.target.value); display();};
    controller = mountPipelineAssistant(root, {api, canEdit:true,
      getLocation:() => target?.locationId || '', getContext:() => ({type:target?.type || 'dataset', target_id:target?.targetId || target?.locationId || ''}),
      getDraft:() => editor && target?.locationId && editor.getLocation() === target.locationId ? editor.getDraft() : null,
      selectContext:choose, navigate,
      undoDraft:() => {if (editor && target?.locationId && editor.getLocation() === target.locationId) editor.undoDraft();},
      loadDraft:async (draft, added, conversationId) => {
        if (editor && target?.locationId && editor.getLocation() === target.locationId) return editor.loadDraft(draft, added);
        await navigate('/pipelines?' + new URLSearchParams({location_id:target.locationId, assistant_conversation:conversationId}));
      }});
    controller.reset(); display();
  }
  window.syncPlatformAssistant = value => {
    if (owner !== value.owner || role !== value.role) {
      controller?.destroy(); host?.remove(); host = root = controller = null; target = null; opened = false;
      document.body.classList.remove('platform-assistant-open'); owner = value.owner; role = value.role;
      if (owner) {const saved = read(); target = saved?.target || null; opened = !!saved?.opened; width = Math.max(320,Math.min(640,Number(saved?.width) || 420));}
    }
    current = value;
    if (!owner) return;
    const initializeTarget = !target && current.locationId;
    if (initializeTarget) target = {...current, type:'dataset', targetId:current.locationId};
    const existed = !!host; create(); display(); if (initializeTarget && existed) controller?.reset();
  };
  window.openPlatformAssistant = value => {
    const next = {...value, label:value.label || (current?.locationId === value.locationId ? current.label : value.locationId), type:value.type || 'dataset', targetId:value.targetId || value.locationId};
    const changed = !target || target.locationId !== next.locationId || target.type !== next.type || target.targetId !== next.targetId;
    target = next; opened = true; create(); display(); if (changed) controller?.reset().then(() => $('ai-message')?.focus()); else $('ai-message')?.focus();
  };
  window.closePlatformAssistant = () => {opened = false; display();};
  window.registerPlatformAssistantEditor = value => {
    editor = value;
    return {reset:() => {}, destroy:() => {if (editor === value) editor = null;}};
  };
  window.addEventListener('pagehide', () => controller?.destroy());
  // Viewer pages do not own the console's account/selection state. Restore only after authentication.
  if (document.body.dataset.assistantStandalone === 'true') {
    api('/api/ai-assistant/capabilities').then(result => window.syncPlatformAssistant({owner:result.owner})).catch(() => {});
  }
})();
