// Follow authorized page references by default; explicitly restored history stays pinned.
(() => {
  let owner = null, role = null, current = null, target = null, host = null, root = null, controller = null, controls = null, editor = null, opened = false, width = 420, follow = true;
  const key = () => `assistant-workspace:${location.origin}:${owner}`;
  const read = () => {try {return JSON.parse(sessionStorage.getItem(key()) || 'null');} catch (_) {return null;}};
  const save = () => {if (owner) try {sessionStorage.setItem(key(), JSON.stringify({target, opened, width, follow}));} catch (_) {}};
  const api = async (path, method='GET', body) => {
    const response = await fetch(path, {method, credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
    const value = await response.json(); if (!response.ok) throw new Error(value.error || 'Request failed'); return value;
  };
  const $ = id => root?.getElementById(id);
  function display() {
    if (!host) return;
    $('assistant-dock').hidden = !opened; $('assistant-reopen').hidden = opened;
    controls?.refresh();
    document.body.classList.toggle('platform-assistant-open', opened);
    document.documentElement.style.setProperty('--assistant-width', width + 'px');
    $('assistant-dock').style.width = width + 'px';
    $('assistant-target').textContent = target?.locationId ? `${target.label || target.locationId} · ${target.page || target.type}${target.type === 'episode' ? ' · Episode ' + target.targetId : ''}` : 'Choose a dataset or restore a conversation';
    $('assistant-follow').checked = follow;
    $('assistant-use-current').hidden = follow || !current?.locationId;
    $('assistant-use-current').textContent = 'Follow current page';
    $('assistant-pinned').hidden = follow;
    save();
  }
  function choose(value) {
    follow = false;
    target = {locationId:value.location_id, type:value.context?.type || 'dataset', targetId:value.context?.target_id || value.location_id, page:value.context?.page, label:value.dataset || value.location_id};
    display(); return true;
  }
  async function navigate(href) {
    const url = new URL(href, location.origin); if (url.origin !== location.origin) return;
    const app = document.querySelector('[x-data]')?._x_dataStack?.[0];
    if (url.pathname === '/' && app?.selectPage && !url.searchParams.has('job_id') && !url.searchParams.has('dataset') && !url.searchParams.has('select')) {
      await app.selectPage(url.searchParams.get('page') || 'datasets'); return;
    }
    save(); location.assign(url.href);
  }
  function create() {
    if (host) return;
    const template = document.getElementById('assistant-workspace-template'); if (!template) return;
    host = document.createElement('div'); host.id = 'platform-assistant-workspace'; root = host.attachShadow({mode:'open'});
    root.append(template.content.cloneNode(true)); document.body.append(host);
    controls = bindAssistantControls({panel:$('assistant-dock'), trigger:$('assistant-reopen'), collapse:$('assistant-close'),
      enabled:() => !!owner, isOpen:() => opened, setOpen:value => {opened = value; display();}});
    $('assistant-use-current').onclick = () => setFollow(true);
    $('assistant-follow').onchange = event => setFollow(event.target.checked);
    $('assistant-width').value = width;
    $('assistant-width').oninput = event => {width = Number(event.target.value); display();};
    controller = mountPipelineAssistant(root, {api, canEdit:true,
      getLocation:() => target?.locationId || '', getContext:() => ({type:target?.type || 'dataset', target_id:target?.targetId || target?.locationId || '', ...(target?.page ? {page:target.page} : {})}),
      getDraft:() => editor && target?.locationId && editor.getLocation() === target.locationId ? editor.getDraft() : null,
      selectContext:choose, navigate,
      undoDraft:() => {if (editor && target?.locationId && editor.getLocation() === target.locationId) editor.undoDraft();},
      loadDraft:async (draft, added, conversationId) => {
        if (editor && target?.locationId && editor.getLocation() === target.locationId) return editor.loadDraft(draft, added);
        await navigate('/pipelines?' + new URLSearchParams({location_id:target.locationId, assistant_conversation:conversationId}));
      }});
    controller.reset(); display();
  }
  const identity = value => JSON.stringify([value?.locationId || '', value?.type || 'dataset', value?.targetId || value?.locationId || '', value?.page || '']);
  function adopt(value) {
    const next = value?.locationId ? {...value, type:value.type || 'dataset', targetId:value.targetId || value.locationId} : null;
    const changed = identity(next) !== identity(target); target = next;
    const existed = !!host; create(); display(); if (changed && existed) controller?.reset();
  }
  function setFollow(value) {
    follow = value; if (follow) adopt(current); else display();
  }
  window.syncPlatformAssistant = value => {
    if (owner !== value.owner || role !== value.role) {
      controls?.destroy(); controls = null; controller?.destroy(); host?.remove(); host = root = controller = null; target = null; opened = false; follow = true;
      document.body.classList.remove('platform-assistant-open'); owner = value.owner; role = value.role;
      if (owner) {const saved = read(); follow = saved?.follow !== false; target = follow ? null : saved?.target || null; opened = !!saved?.opened; width = Math.max(320,Math.min(640,Number(saved?.width) || 420));}
    }
    if (!value.type && current?.type === 'quality' && value.locationId === current.locationId && value.page === current.page) value = {...value, type:current.type, targetId:current.targetId};
    current = value;
    if (!owner) return;
    if (follow) adopt(current); else {create(); display();}
  };
  window.syncPlatformAssistantContext = value => {
    if (value.locationId !== current?.locationId || value.page !== current?.page) return;
    current = {...current, ...value}; if (follow) adopt(current);
  };
  window.openPlatformAssistant = value => {
    follow = true;
    const next = {...value, label:value.label || (current?.locationId === value.locationId ? current.label : value.locationId), type:value.type || 'dataset', targetId:value.targetId || value.locationId};
    const changed = identity(target) !== identity(next);
    target = next; current = {...current, ...next}; create(); controls?.open(); display();
    if (changed) controller?.reset().then(() => {if (opened && target === next) $('ai-message')?.focus();});
  };
  window.closePlatformAssistant = () => controls?.close();
  window.registerPlatformAssistantEditor = value => {
    editor = value;
    return {reset:() => {}, destroy:() => {if (editor === value) editor = null;}};
  };
  window.addEventListener('pagehide', () => {controls?.destroy(); controller?.destroy();});
  // Viewer pages do not own the console's account/selection state. Restore only after authentication.
  if (document.body.dataset.assistantStandalone === 'true') {
    api('/api/ai-assistant/capabilities').then(async result => {
      const account = {owner:result.owner, role:result.can_edit ? 'operator' : 'viewer'};
      window.syncPlatformAssistant(account);
      const page = window.platformAssistantPage;
      if (!page) return;
      try {
        const reference = await api('/api/ai-assistant/page-context?' + new URLSearchParams({dataset_key:page.datasetKey, episode_index:String(page.episodeIndex)}));
        window.syncPlatformAssistant({...account, locationId:reference.location_id, label:reference.dataset, type:reference.context.type, targetId:reference.context.target_id, page:'viewer'});
      } catch (_) {if (follow) $('ai-status').textContent = 'Current episode is not available to the assistant. Choose an accessible registered dataset.';}
    }).catch(() => {});
  }
})();
