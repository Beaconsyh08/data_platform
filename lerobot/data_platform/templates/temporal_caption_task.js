window.createTemporalCaptionTask = (root, {api,reviewUrl,enabled=true}) => {
const $=id=>root.querySelector(`[data-caption-id="${id}"]`);
let schemes=[], readiness=null, destroyed=false, timer=null, submitting=false;
const pendingKey=`caption-batch:${api}`;
let pending=JSON.parse(sessionStorage.getItem(pendingKey)||'null');
$('open-review').href=reviewUrl;
async function get(url){const response=await fetch(url);const data=await response.json();if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);return data;}
function selection(){
 const total=readiness?.total_episodes;
 if(!Number.isInteger(total)||total<1)throw new Error('Dataset episode count is unavailable. Refresh dataset metadata.');
 if($('episode-mode').value==='all')return Array.from({length:total},(_,i)=>i);
 const result=new Set();
 for(const token of $('job-episode').value.split(',')){
  const match=token.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
  if(!match)throw new Error('Use indices or ranges, for example 0, 5, 10-20.');
  const lo=Number(match[1]),hi=Number(match[2]??match[1]);
  if(!Number.isSafeInteger(lo)||!Number.isSafeInteger(hi)||hi<lo||hi>=total)throw new Error(`Episode indices must be between 0 and ${total-1}.`);
  for(let i=lo;i<=hi;i++)result.add(i);
 }
 return [...result].sort((a,b)=>a-b);
}
function update(){
 const scheme=schemes.find(x=>x.id===$('job-scheme').value);
 $('method-description').textContent=scheme?.description||'';
 const visualOnly=scheme?.supports_signals===false;
 if(visualOnly)$('include-signals').checked=false;
 $('range-label').hidden=$('episode-mode').value==='all';$('job-episode').required=!$('range-label').hidden;
 const cap=readiness?.schemes?.find(x=>x.id===scheme?.id);
 const reason=!enabled?'Preview only · start annotation on the platform':cap?.unavailable_reason||readiness?.unavailable_reason||(!readiness?.signal_options?'Upgrade the executor to support action/state input selection':'');
 $('job-readiness').textContent=reason||(visualOnly?'Ready · video only':'Ready');
 let valid=false, count=0;
 try{const episodes=selection();count=episodes.length;$('selection-summary').textContent=`${episodes.length} episode(s) · ${episodes.length} separate jobs`;valid=episodes.length>0;}
 catch(error){$('selection-summary').textContent=error.message;}
 $('job-submit').disabled=submitting||!enabled||!cap?.can_submit||!!reason||!valid;
 $('job-submit').textContent=submitting ? `Submitting ${pending?.confirmed||0}/${pending?.episodes.length||count}…` : valid ? `Submit ${count} annotation job${count===1?'':'s'}` : 'Submit annotation jobs';
 for(const id of ['job-scheme','episode-mode','job-episode','include-signals'])$(id).disabled=submitting;
 $('include-signals').disabled=submitting||visualOnly;
 $('include-signals').closest('label').title=visualOnly?'FLORA uses video only; action/state signals are unavailable.':'';
 $('caption-job-form').setAttribute('aria-busy',String(submitting));
}
async function refresh(){
 try{
  const catalog=await get(enabled ? `${api}/jobs` : api);if(destroyed)return;
  schemes=catalog.schemes||[];
  if(!$('job-scheme').options.length){for(const scheme of schemes){const option=document.createElement('option');option.value=scheme.id;option.textContent=scheme.name;$('job-scheme').append(option);}if(pending)$('job-scheme').value=pending.scheme;}
  if(enabled)readiness=catalog;
  if(!destroyed)update();
 }catch(error){readiness=null;if(!destroyed){update();$('job-readiness').textContent=error.message;}}
 if(enabled&&!destroyed)timer=setTimeout(refresh,10000);
}
if(pending){$('episode-mode').value='range';$('job-episode').value=pending.episodes.join(',');$('include-signals').checked=pending.include_signals;$('job-status').textContent='An earlier batch was not fully confirmed. Submit again to resume without duplicating confirmed jobs.';}
for(const id of ['job-scheme','episode-mode','include-signals'])$(id).onchange=update;
$('job-episode').oninput=update;
$('caption-job-form').onsubmit=async event=>{
 event.preventDefault();if(submitting||$('job-submit').disabled)return;
 const batch={episodes:selection(),scheme:$('job-scheme').value,include_signals:$('include-signals').checked};
 const signature=JSON.stringify(batch);
 if(!pending||pending.signature!==signature)pending={...batch,signature,key:crypto.randomUUID(),confirmed:0};
 sessionStorage.setItem(pendingKey,JSON.stringify(pending));submitting=true;update();
 try{
  for(let i=pending.confirmed;i<pending.episodes.length;i++){
   if(destroyed)throw new Error('Dataset changed. Return to this dataset to resume submission.');
   const response=await fetch(`${api}/jobs`,{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':`${pending.key}:${pending.episodes[i]}`},body:JSON.stringify({episode_index:pending.episodes[i],scheme:pending.scheme,include_signals:pending.include_signals})});
   const data=await response.json();if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);
   pending.confirmed=i+1;sessionStorage.setItem(pendingKey,JSON.stringify(pending));
   $('job-status').textContent=`${pending.confirmed}/${pending.episodes.length} jobs submitted. See Jobs for execution progress.`;
   update();
  }
  pending=null;sessionStorage.removeItem(pendingKey);
  window.dispatchEvent(new CustomEvent('caption-jobs-submitted'));
 }catch(error){$('job-status').textContent=`${error.message} Already submitted jobs remain in Jobs. Repeat the same selection to resume safely.`;}
 finally{submitting=false;update();}
};
refresh();
return {destroy(){destroyed=true;clearTimeout(timer);}};
};
