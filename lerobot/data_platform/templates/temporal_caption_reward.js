// Read-only reward evidence. Never convert a model score into a human review decision.
const rewardColors = ['#334155','#fb7185','#fbbf24','#34d399'];
function clearReward() {
    $('reward-review').hidden=true; $('reward-overlay').hidden=true;
    for(const id of ['reward-tracks','reward-values','reward-conflicts','reward-raw'])$(id).replaceChildren();
    $('reward-terminal').textContent='';
}
function rewardSeek(frame) {
    if(!artifact)return;
    $('loop').checked=false; $('video').currentTime=frame/artifact.fps;
}
function renderReward(data) {
    clearReward();
    const reward=data.reward;if(!reward)return;
    $('reward-review').hidden=false;$('reward-overlay').hidden=false;
    $('reward-terminal').textContent=`Episode outcome ${reward.terminal.score}/5 · ${reward.terminal.rationale}`;
    const tracks=[...reward.windows.map((w,i)=>({label:`Window ${i+1}`,scores:w.scores})),{label:'Merged',scores:reward.merged_scores}];
    for(const track of tracks) {
        const row=text('div','','reward-track'),label=text('span',track.label),canvas=document.createElement('canvas');
        canvas.width=data.frame_count;canvas.height=22;canvas.tabIndex=0;canvas.setAttribute('role','slider');
        canvas.setAttribute('aria-label',`${track.label}: seek video`);canvas.setAttribute('aria-valuemin','0');canvas.setAttribute('aria-valuemax',String(data.frame_count-1));
        canvas.setAttribute('aria-valuenow','0');
        const context=canvas.getContext('2d');
        track.scores.forEach((score,i)=>{context.fillStyle=rewardColors[score];context.fillRect(i,0,1,22);});
        canvas.onclick=event=>{const rect=canvas.getBoundingClientRect();rewardSeek(Math.max(0,Math.min(data.frame_count-1,Math.floor((event.clientX-rect.left)/rect.width*data.frame_count))));};
        canvas.onkeydown=event=>{if(!['ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();rewardSeek(Math.max(0,Math.min(data.frame_count-1,Math.floor($('video').currentTime*data.fps)+(event.key==='ArrowRight'?1:-1))));};
        const cursor=text('span','','reward-cursor'),surface=text('div','','reward-track-surface');surface.append(canvas,cursor);row.append(label,surface);$('reward-tracks').append(row);
    }
    if(!reward.disagreement_intervals.length)$('reward-conflicts').append(text('span','No overlapping-window score disagreements.'));
    for(const span of reward.disagreement_intervals) {
        const button=text('button',`${(span.start_frame/data.fps).toFixed(2)}–${(span.end_frame/data.fps).toFixed(2)}s`,'dp-btn');
        button.title='Review disagreeing reward windows';button.onclick=()=>rewardSeek(span.start_frame);$('reward-conflicts').append(button);
    }
    reward.windows.forEach((window,i)=>{const detail=document.createElement('details');detail.append(text('summary',`Window ${i+1} · raw model output`),text('pre',window.output));$('reward-raw').append(detail);});
    $('reward-raw').append(text('p',reward.alignment,'note'));
}
function updateReward(frame) {
    const reward=artifact?.reward;if(!reward)return;
    const scores=reward.windows.map(w=>w.scores[frame]);
    const conflict=new Set(scores.filter(Boolean)).size>1;
    const merged=reward.merged_scores[frame];
    $('reward-overlay').textContent=`Reward ${merged}/3 · W${reward.merged_source_window[frame]+1}${conflict?' · Windows disagree':''}`;
    $('reward-overlay').classList.toggle('reward-disagreement',conflict);
    $('reward-values').replaceChildren();
    scores.forEach((score,i)=>{const item=text('span',`W${i+1}: ${score?`${score}/3`:'outside coverage'}`,'badge');item.style.color=rewardColors[score];$('reward-values').append(item);});
    for(const cursor of $('reward-tracks').querySelectorAll('.reward-cursor'))cursor.style.left=`${frame/artifact.frame_count*100}%`;
    for(const canvas of $('reward-tracks').querySelectorAll('canvas'))canvas.setAttribute('aria-valuenow',String(frame));
}
