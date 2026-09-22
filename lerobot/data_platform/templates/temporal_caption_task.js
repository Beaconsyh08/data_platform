window.createTemporalCaptionTask = (root, {api,reviewUrl,enabled=true}) => {
const $=id=>root.querySelector(`[data-caption-id="${id}"]`);
let schemes=[],destroyed=false,timer=null;
function text(tag,value,cls=''){const e=document.createElement(tag);e.textContent=value;e.className=cls;return e;}
async function get(url){const r=await fetch(url);if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json();}
let jobReadiness=null;
function updateJobReadiness(){
 const method=jobReadiness?.schemes?.find(x=>x.id===$('job-scheme').value);
 $('job-submit').disabled=!enabled || submitting || !(method ? method.can_submit : jobReadiness?.can_submit);
 $('job-readiness').textContent=(!enabled ? 'Preview only · start annotation on the development platform' : '') || method?.unavailable_reason || jobReadiness?.unavailable_reason || (jobReadiness?'Ready · a separate annotation result will be saved':'Checking annotation executor…');
}
function chooseScheme(){
 const id=$('job-scheme').value;
 if(!id)return;
 $('method-description').textContent=schemes.find(x=>x.id===id)?.description || '';
 try{localStorage.setItem('dataPlatform.captionMethod',id);}catch(_){}
 const url=new URL(reviewUrl,location.origin);url.searchParams.set('scheme',id);url.searchParams.set('episode',$('job-episode').value);$('open-review').href=url;
 updateJobReadiness();
}
let method=new URLSearchParams(location.search).get('scheme');
try{method=method || localStorage.getItem('dataPlatform.captionMethod');}catch(_){}

const pendingKey=`caption-submit:${api}`;
let pending=JSON.parse(sessionStorage.getItem(pendingKey)||'null');
let submitting=false;
async function post(url,body,headers={}) {
    const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json',...headers},body:JSON.stringify(body)});
    const data=await response.json(); if(!response.ok) throw new Error(data.error || `HTTP ${response.status}`); return data;
}
async function refreshJobs() {
    try {
        const data=await get(`${api}/jobs`);if(destroyed)return;
        jobReadiness = data; updateJobReadiness();
        $('caption-import-form').closest('details').hidden=!data.can_import;

        $('caption-jobs').replaceChildren();
        for(const job of data.jobs) {
            const row=text('div',`Episode ${job.options.episode_index} · ${({video_events:'Video + Events',multiview_semantics:'Multi-view Semantics',fusion_review:'Fusion + Review'}[job.options.scheme]||job.options.scheme)} · ${job.status}${job.status==='queued'?` · ${job.queue_ahead} ahead`:''}`,'task-line');
            if(job.error) row.append(text('p',job.error));
            if(job.status==='running') {
                const detail=await get(`/api/control/jobs/${job.job_id}`);
                const event=detail.job.events?.at(-1); if(event) row.append(text('small',event.message));
            }
            if(job.result?.review_url) {const link=text('a','Open result');const target=new URL(reviewUrl,location.origin);target.search=new URL(job.result.review_url,location.origin).search;link.href=target;row.append(link);}
            for(const action of ['cancel','retry'].filter(a=>(Array.isArray(job.available_actions) ? job.available_actions.includes(a) : job.available_actions?.[a]?.allowed === true || job.available_actions?.[a] === true))) {
                const button=text('button',action==='cancel'?'Cancel':'Retry (calls Qwen again)');
                button.onclick=async()=>{button.disabled=true;try{await post(`/api/control/jobs/${job.job_id}/${action}`,{revision:job.revision,idempotency_key:crypto.randomUUID()});await refreshJobs();}catch(error){$('job-status').textContent=error.message;button.disabled=false;}};
                row.append(button);
            }
            $('caption-jobs').append(row);
        }
    } catch(error) {jobReadiness=null;updateJobReadiness();$('job-status').textContent=`Status unavailable; existing tasks may still be running. ${error.message}`;}
}
if(pending) {$('job-episode').value=pending.body.episode_index;$('job-scheme').value=pending.body.scheme;$('job-status').textContent='Previous submission was not confirmed. Submit again to recover the same task.';}
else {const episode=new URLSearchParams(location.search).get('episode');if(episode!==null)$('job-episode').value=episode;}
$('job-scheme').onchange=()=>{chooseScheme();};
$('caption-job-form').onsubmit=async event=>{
    event.preventDefault();if(submitting)return;
    const body={episode_index:Number($('job-episode').value),scheme:$('job-scheme').value};
    if(!Number.isInteger(body.episode_index)||body.episode_index<0)return;
    if(!pending||JSON.stringify(pending.body)!==JSON.stringify(body))pending={key:crypto.randomUUID(),body};
    sessionStorage.setItem(pendingKey,JSON.stringify(pending));submitting=true;$('job-submit').disabled=true;$('job-status').textContent='Submitting…';
    try {await post(`${api}/jobs`,pending.body,{'Idempotency-Key':pending.key});pending=null;sessionStorage.removeItem(pendingKey);$('job-status').textContent='Task accepted.';await refreshSaved();}
    catch(error){$('job-status').textContent=`${error.message}. If delivery is uncertain, submit the same selection again to recover the task.`;}
    finally {submitting=false;await refreshJobs();}
};
$('caption-import-form').onsubmit=async event=>{
    event.preventDefault();const file=$('caption-import-file').files[0];if(!file)return;
    const form=new FormData();form.append('results',file);$('caption-import-submit').disabled=true;
    try{const response=await fetch(`${api}/import`,{method:'POST',body:form});const data=await response.json();if(!response.ok)throw new Error(data.error);location.href=data.review_url;}
    catch(error){$('job-status').textContent=error.message;$('caption-import-submit').disabled=false;}
};
async function refreshSaved(){
 const data=await get(api);if(destroyed)return;
 schemes=data.schemes || [];
 if(!$('job-scheme').options.length){for(const scheme of schemes){const option=text('option',scheme.name);option.value=scheme.id;$('job-scheme').append(option);}if(schemes.some(x=>x.id===(pending?.body.scheme || method)))$('job-scheme').value=pending?.body.scheme || method;chooseScheme();}
 $('saved-results').replaceChildren();
 const runs=data.runs || [];
 $('saved-status').textContent=runs.length ? `${runs.length} saved result versions in this environment.` : 'No saved captions for this dataset in this environment. Results from another environment must be imported.';
 for(const run of runs){const url=new URL(reviewUrl,location.origin);url.search=new URLSearchParams({scheme:run.scheme.id,episode:run.episode_index,run:run.run});const row=text('p','');const link=text('a',`Episode ${run.episode_index} · ${run.scheme.name} · ${run.variant}`);link.href=url;row.append(link);$('saved-results').append(row);}
}
async function poll(){if(destroyed)return;await Promise.all([refreshJobs(),refreshSaved().catch(error=>{$('saved-status').textContent=error.message;})]);if(!destroyed)timer=setTimeout(poll,5000);}
refreshSaved().catch(error=>{$('saved-status').textContent=error.message;});
if(enabled)poll();else $('job-readiness').textContent='Preview only · start annotation on the development platform';
$('job-episode').oninput=chooseScheme;
return {destroy(){destroyed=true;clearTimeout(timer);}};
};
