
// Cagentic
const $ = s => document.querySelector(s);
const log = $('#log'), input = $('#input'), sendBtn = $('#send');
const _nativeFetch = window.fetch.bind(window);
const _gatewayToken = document.querySelector('meta[name="cagentic-token"]')?.content || '';
window.fetch = (resource, options={}) => {
  const target = typeof resource === 'string' ? resource : (resource&&resource.url)||'';
  if(target.startsWith('/api/')){
    const headers = new Headers(options.headers || (resource&&resource.headers) || {});
    headers.set('X-Cagentic-Token', _gatewayToken);
    options={...options,headers};
  }
  return _nativeFetch(resource,options);
};
let state = {
  chats: [], currentId: null, settings: {}, busy: false,
  voiceOut: false, voiceName: '', renderedPanels: new Set(), closedWindows: [],
  projects: [], activeProjectId: null,
  os: null, osView: 'today', userName: '', architectureMode: 'all', selectedCapability: '',
  _openProjects: new Set(), _openUnaffiliated: true, _openProjectsRoot: true,
};

// ---- CLOCK ------------------------------------------------------------------
function updateClock() {
  const n = new Date(), pad = v => String(v).padStart(2,'0');
  $('#jClock').textContent = pad(n.getHours())+':'+pad(n.getMinutes())+':'+pad(n.getSeconds());
  const days=['SUN','MON','TUE','WED','THU','FRI','SAT'];
  const months=['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
  $('#jDate').textContent = days[n.getDay()]+' '+n.getDate()+' '+months[n.getMonth()]+' '+n.getFullYear();
}
setInterval(updateClock, 1000); updateClock();

// ---- ORB --------------------------------------------------------------------
(function initOrb() {
  const canvas = $('#orbCanvas'); if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let W, H, cx, cy, particles = [], t = 0;
  function resize() {
    const p = canvas.parentElement;
    W = canvas.width = p.clientWidth || 600;
    H = canvas.height = p.clientHeight || 300;
    cx = W/2; cy = H/2;
  }
  function mkPart() {
    const th = Math.random()*Math.PI*2, ph = Math.random()*Math.PI, r = 45+Math.random()*40;
    return { x:cx+r*Math.sin(ph)*Math.cos(th), y:cy+r*Math.sin(ph)*Math.sin(th)*0.4, z:Math.cos(ph),
      vx:(Math.random()-.5)*0.3, vy:(Math.random()-.5)*0.3, life:Math.random(),
      decay:0.007+Math.random()*0.016, size:0.7+Math.random()*2.2, alpha:0.4+Math.random()*0.6 };
  }
  function resetPart(p) {
    const th=Math.random()*Math.PI*2, ph=Math.random()*Math.PI, r=43+Math.random()*42;
    p.x=cx+r*Math.sin(ph)*Math.cos(th); p.y=cy+r*Math.sin(ph)*Math.sin(th)*0.4; p.z=Math.cos(ph); p.life=1;
  }
  function initParts(){ particles=[]; for(let i=0;i<220;i++) particles.push(mkPart()); }
  const ORBS=[{r:105,s:0.65,sz:3.5,ph:0},{r:105,s:0.65,sz:3.5,ph:Math.PI},
    {r:82,s:-1.05,sz:2.5,ph:Math.PI/2},{r:125,s:0.45,sz:2,ph:Math.PI/3},{r:82,s:-1.05,sz:2.5,ph:Math.PI*1.5}];
  function draw() {
    ctx.clearRect(0,0,W,H);
    // speed reacts to state: faster when busy/listening/speaking
    const sp = state.busy ? 0.03 : (window.__jSpeak ? 0.022 : 0.011);
    t += sp;
    for(let r=120;r>=12;r-=18) {
      const g=ctx.createRadialGradient(cx,cy,r*0.4,cx,cy,r);
      g.addColorStop(0,`rgba(199,155,216,${0.022+(120-r)*0.0006})`); g.addColorStop(1,'rgba(0,0,0,0)');
      ctx.beginPath(); ctx.arc(cx,cy,r,0,Math.PI*2); ctx.fillStyle=g; ctx.fill();
    }
    const mg=ctx.createRadialGradient(cx,cy,0,cx,cy,65);
    mg.addColorStop(0,'rgba(244,236,248,0.88)'); mg.addColorStop(0.22,'rgba(199,155,216,0.58)');
    mg.addColorStop(0.6,'rgba(154,111,176,0.22)'); mg.addColorStop(1,'rgba(0,0,0,0)');
    ctx.beginPath(); ctx.arc(cx,cy,65,0,Math.PI*2); ctx.fillStyle=mg; ctx.fill();
    const ic=ctx.createRadialGradient(cx,cy,0,cx,cy,20);
    ic.addColorStop(0,'rgba(255,255,255,1)'); ic.addColorStop(0.5,'rgba(230,215,238,0.75)'); ic.addColorStop(1,'rgba(199,155,216,0)');
    ctx.beginPath(); ctx.arc(cx,cy,20,0,Math.PI*2); ctx.fillStyle=ic; ctx.fill();
    particles.forEach(p=>{
      p.x+=p.vx; p.y+=p.vy; p.life-=p.decay; if(p.life<=0) resetPart(p);
      const a=Math.max(0,p.life)*p.alpha, br=0.5+p.z*0.5;
      ctx.beginPath(); ctx.arc(p.x,p.y,p.size,0,Math.PI*2);
      ctx.fillStyle=`rgba(${Math.round(120+br*79)},${Math.round(85+br*70)},${Math.round(140+br*76)},${a})`; ctx.fill();
    });
    ORBS.forEach(o=>{
      const a=t*o.s+o.ph, ox=cx+o.r*Math.cos(a), oy=cy+o.r*0.38*Math.sin(a);
      ctx.beginPath(); ctx.arc(ox,oy,o.sz,0,Math.PI*2);
      ctx.fillStyle='rgba(199,155,216,0.9)'; ctx.shadowColor='#c79bd8'; ctx.shadowBlur=12; ctx.fill(); ctx.shadowBlur=0;
    });
    requestAnimationFrame(draw);
  }
  window.addEventListener('resize',()=>{resize();initParts();});
  resize(); initParts(); draw();
})();

// ---- COGNITIVE CORE --------------------------------------------------------
(function initCognitiveCore(){
  const canvas=$('#coreCanvas');if(!canvas)return;const ctx=canvas.getContext('2d');
  const reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let width=0,height=0,dpr=1,time=0,nodes=[];
  function seed(){
    nodes=[];const count=Math.max(90,Math.min(190,Math.round(width*height/3100)));
    for(let i=0;i<count;i++){
      const theta=Math.random()*Math.PI*2,phi=Math.acos(2*Math.random()-1),radius=.2+Math.pow(Math.random(),.55)*.8;
      nodes.push({theta,phi,radius,size:.6+Math.random()*1.8,phase:Math.random()*Math.PI*2});
    }
  }
  function resize(){
    const box=canvas.getBoundingClientRect();dpr=Math.min(2,window.devicePixelRatio||1);
    width=Math.max(1,box.width);height=Math.max(1,box.height);canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);seed();
  }
  function project(node){
    const spin=node.theta+time*(state.busy?.0032:.0012),pulse=1+Math.sin(time*.018+node.phase)*.025;
    const x=Math.sin(node.phi)*Math.cos(spin),y=Math.cos(node.phi),z=Math.sin(node.phi)*Math.sin(spin);
    const scale=Math.min(width*.39,height*.46)*node.radius*pulse,perspective=.82+z*.18;
    return {x:width/2+x*scale,y:height*.45+y*scale*.78,z,alpha:.18+(z+1)*.28,size:node.size*perspective};
  }
  function draw(){
    ctx.clearRect(0,0,width,height);time+=reduced?0:1;
    const cx=width/2,cy=height*.45,rad=Math.min(width*.35,height*.42);
    const glow=ctx.createRadialGradient(cx,cy,0,cx,cy,rad*1.22);glow.addColorStop(0,'rgba(199,155,216,.24)');glow.addColorStop(.38,'rgba(199,155,216,.09)');glow.addColorStop(1,'rgba(0,0,0,0)');ctx.fillStyle=glow;ctx.fillRect(0,0,width,height);
    const points=nodes.map(project);
    ctx.lineWidth=.55;
    for(let i=0;i<points.length;i++){
      const a=points[i];for(let j=i+1;j<Math.min(points.length,i+15);j++){
        const b=points[j],dx=a.x-b.x,dy=a.y-b.y,dist=Math.hypot(dx,dy);
        if(dist<Math.min(54,width*.075)){ctx.strokeStyle=`rgba(199,155,216,${Math.max(0,.12-dist/520)*(a.alpha+b.alpha)})`;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}
      }
    }
    points.sort((a,b)=>a.z-b.z).forEach(p=>{ctx.beginPath();ctx.arc(p.x,p.y,p.size,0,Math.PI*2);ctx.fillStyle=`rgba(199,155,216,${p.alpha})`;ctx.shadowColor='#c79bd8';ctx.shadowBlur=p.size>1.5?7:2;ctx.fill();});ctx.shadowBlur=0;
    if(!reduced)requestAnimationFrame(draw);
  }
  if('ResizeObserver' in window){const observer=new ResizeObserver(()=>{resize();if(reduced)draw();});observer.observe(canvas);}else{window.addEventListener('resize',()=>{resize();if(reduced)draw();});}
  resize();draw();
})();

// ---- HELPERS ----------------------------------------------------------------
function esc(s){ return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// Allow only http(s) (and data:image for <img>) URLs into href/src attributes;
// anything else (javascript:, data:text/html, etc.) is dropped to '#'.
function safeUrl(u){ u=(u||'').trim(); return /^https?:\/\//i.test(u)?u:'#'; }
function safeImgUrl(u){ u=(u||'').trim(); return (/^https?:\/\//i.test(u)||/^data:image\//i.test(u))?u:''; }
function md(src) {
  const blocks=[];
  let s=(src||'').replace(/```(\w*)\n?([\s\S]*?)```/g,(m,lang,code)=>{
    blocks.push('<div class="codeblock"><div class="cb-head"><span class="cb-lang">'+(lang||'text')+'</span>'+
      '<button class="cb-copy">COPY</button></div><pre><code>'+esc(code.replace(/\n$/,''))+'</code></pre></div>');
    return '\x00B'+(blocks.length-1)+'\x00';
  });
  s=esc(s);
  s=s.replace(/`([^`\n]+)`/g,'<code>$1</code>');
  s=s.replace(/^\s*#{1,6}\s+(.+)$/gm,'<h3>$1</h3>');
  s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g,'$1<em>$2</em>');
  s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  s=s.replace(/(?:^|\n)((?:\s*[-*]\s+.+(?:\n|$))+)/g,(m,b)=>
    '\n<ul>'+b.trim().split('\n').map(x=>'<li>'+x.replace(/^\s*[-*]\s+/,'')+'</li>').join('')+'</ul>');
  s=s.split(/\n{2,}/).map(p=>p.trim()?'<p>'+p+'</p>':'').join('');
  s=s.replace(/\n/g,'<br>');
  s=s.replace(/<p>(<(?:ul|h3|div))/g,'$1').replace(/(<\/(?:ul|h3|div)>)<\/p>/g,'$1');
  s=s.replace(/\x00B(\d+)\x00/g,(m,i)=>blocks[+i]);
  return s;
}
// strip plain text of markdown/HUD for speech
function plain(text){
  return stripHud(text).replace(/```[\s\S]*?```/g,' code block ')
    .replace(/[#*`>_]/g,'').replace(/\[([^\]]+)\]\([^)]+\)/g,'$1').replace(/\s+/g,' ').trim();
}
function scrollDown(){ log.scrollTop=log.scrollHeight; }
function getThread(){ let t=log.querySelector('.j-thread'); if(!t){t=document.createElement('div');t.className='j-thread';log.appendChild(t);} return t; }
function clearLog(){ log.innerHTML=''; }
function avatarHTML(){ return '<div class="j-avatar">C</div>'; }
function setOrbLabel(text){ const l=$('#orbLabel'); if(l) l.textContent=(text||'New Chat'); }
function compactOrb(on){ const z=$('#orbZone'); if(z) z.classList.toggle('compact', on); }

// ---- PERSONAL OS -----------------------------------------------------------
function showOsView(name){
  const valid=['today','inbox','planner','goals','routines','skills','connections','assistant'];
  if(!valid.includes(name)) name='today';
  state.osView=name;
  valid.forEach(v=>{
    const el=$('#'+v+'View'); if(el) el.classList.toggle('hidden',v!==name);
  });
  document.querySelectorAll('.os-nav-item').forEach(el=>el.classList.toggle('active',el.dataset.view===name));
  document.body.dataset.osView=name;
  try{ localStorage.setItem('cagentic_os_view',name); }catch(e){}
  if(name==='skills')renderArchitecture();
  if(name==='assistant') setTimeout(()=>{scrollDown();input.focus();},30);
}
function osDate(ts, withTime=true){
  if(!ts) return 'No date';
  const d=new Date(Number(ts)*1000);
  return d.toLocaleDateString([], {weekday:'short',month:'short',day:'numeric'})+
    (withTime?' · '+d.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'}):'');
}
function osRelative(ts){
  if(!ts) return 'Anytime';
  const diff=Number(ts)*1000-Date.now(), mins=Math.round(Math.abs(diff)/60000);
  if(diff<0){ if(mins<60)return mins+'m overdue'; if(mins<1440)return Math.round(mins/60)+'h overdue'; return Math.round(mins/1440)+'d overdue'; }
  if(mins<60)return 'in '+mins+'m'; if(mins<1440)return 'in '+Math.round(mins/60)+'h'; return 'in '+Math.round(mins/1440)+'d';
}
function osAgo(ts){
  if(!ts)return 'just now';const mins=Math.max(0,Math.round((Date.now()-Number(ts)*1000)/60000));
  if(mins<1)return 'just now';if(mins<60)return mins+'m ago';if(mins<1440)return Math.round(mins/60)+'h ago';return Math.round(mins/1440)+'d ago';
}
function osEmpty(title,sub,button,type){
  return '<div class="os-empty"><span>✦</span><strong>'+esc(title)+'</strong><p>'+esc(sub||'')+'</p>'+
    (button?'<button class="os-text-btn" data-open-capture="'+esc(type||'event')+'">'+esc(button)+'</button>':'')+'</div>';
}
function renderPersonalOS(){
  const data=state.os; if(!data) return;
  $('#osGreeting').textContent=data.greeting||'Hello';
  $('#osUserName').textContent=state.userName?', '+state.userName:'';
  $('#osDate').textContent=(data.date_label||'')+' · Here is what deserves your attention.';
  const stats=data.stats||{};
  const architecture=data.architecture||{},systemStatus=architecture.status||{};
  const statItems=[
    ['EVENTS',stats.events_today||0,'today',Math.min(100,(stats.events_today||0)*18+14)],
    ['INBOX',stats.unread_inbox||0,'unread',Math.min(100,(stats.unread_inbox||0)*12+10)],
    ['DEADLINES',stats.open_deadlines||0,stats.overdue?stats.overdue+' overdue':'open',Math.min(100,(stats.open_deadlines||0)*16+12)],
    ['GOALS',stats.active_goals||0,'active',Math.min(100,(stats.active_goals||0)*18+15)],
    ['SKILLS',systemStatus.capabilities||0,'mapped',Math.min(100,systemStatus.capabilities||0)]
  ];
  $('#osStats').innerHTML=statItems.map(x=>'<div class="vault-vital" style="--level:'+x[3]+'%"><div><span>● '+x[0]+'</span><em>'+esc(x[2])+'</em></div><strong>'+x[1]+'</strong><i></i></div>').join('');

  const insights=data.insights||[];
  $('#insightList').innerHTML=insights.length?insights.slice(0,4).map(i=>'<button class="vault-directive '+esc(i.severity||'calm')+'" data-ai-prompt="'+esc(i.action_prompt||('Help me act on: '+i.title))+'"><i></i><span><strong>'+esc(i.title)+'</strong><em>'+esc(i.body)+'</em></span></button>').join(''):osEmpty('All clear','No active directives.');

  const agenda=(data.agenda||[]).slice(0,7);
  $('#todayTimeline').innerHTML=agenda.length?agenda.map(item=>{const d=new Date(Number(item.start_at)*1000);return '<button class="vault-schedule-row" data-view-jump="planner"><time>'+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',hour12:false})+'</time><span><strong>'+esc(item.title)+'</strong><em>'+esc(item.location||item.kind||'event')+'</em></span></button>';}).join(''):osEmpty('Schedule clear','No events in range.','Add event','event');

  const deadlines=(data.deadlines||[]).filter(d=>d.status!=='done').slice(0,6);
  $('#deadlineList').innerHTML=deadlines.length?deadlines.map(d=>'<button class="vault-deadline '+(d.due_at&&d.due_at*1000<Date.now()?'overdue':'')+'" data-deadline-done="'+esc(d.id)+'"><span>□</span><strong>'+esc(d.text)+'</strong><em>'+esc(d.due_at?osRelative(d.due_at):'open')+'</em></button>').join(''):osEmpty('No deadlines','Attention wire clear.','Add','deadline');

  const goals=(data.goals||[]).filter(g=>g.status==='active').slice(0,4);
  $('#goalStrip').innerHTML=goals.length?goals.map(g=>'<button class="vault-document" data-view-jump="goals"><span>'+esc(g.title)+'</span><em>'+Math.round(Number(g.progress)||0)+'%</em></button>').join(''):osEmpty('No goals indexed','Set a primary direction.','Create','goal');

  const integrations=(data.integrations||[]).slice(0,5);
  $('#integrationStrip').innerHTML=integrations.length?integrations.map(c=>'<button data-view-jump="connections"><i class="'+(c.connected?'online':'')+'"></i>'+esc(c.name)+'</button>').join(''):'<span>LOCAL ONLY</span>';
  const wire=(data.notifications||[]).slice(0,3);
  $('#vaultWire').innerHTML=wire.length?wire.map(item=>'<button data-notification-prompt="'+esc(item.action_prompt||item.body)+'" data-notification-id="'+esc(item.id)+'"><span>› '+esc(item.title)+'</span><em>'+esc(item.body)+'</em></button>').join(''):'<p>› Monitor active. No new intelligence packets.</p>';
  $('#vaultQueueState').textContent=(Number(data.unread_notifications)||0)+' QUEUED';
  $('#vaultCoreState').textContent='CORE · '+(state.busy?'ACTIVE':'IDLE')+'   LINK · ONLINE   RUNNER · '+(data.proactive_running?'ALIVE':'STANDBY');
  const inboxCount=Number(data.unread_inbox)||0, inboxBadge=$('#inboxNavCount');
  if(inboxBadge){inboxBadge.textContent=inboxCount>99?'99+':String(inboxCount);inboxBadge.classList.toggle('hidden',inboxCount===0);}
  renderInbox(); renderPlanner(); renderGoals(); renderRoutines(); renderArchitecture(); renderConnections(); renderNotifications(); bindOsActions();
}
function goalCard(g){
  const pct=Math.max(0,Math.min(100,Number(g.progress)||0));
  return '<article class="os-goal-card"><div class="os-goal-top"><span>'+esc((g.category||'personal').toUpperCase())+'</span><em>'+pct+'%</em></div><strong>'+esc(g.title)+'</strong><div class="os-progress"><i style="width:'+pct+'%"></i></div><div class="os-goal-foot"><span>'+(g.target_at?esc('Target '+osDate(g.target_at,false)):'No target date')+'</span><button data-goal-step="'+esc(g.id)+'">+10%</button></div></article>';
}
function connectionChip(c){
  const initials=(c.name||'?').split(/\s+/).map(x=>x[0]).join('').slice(0,2).toUpperCase();
  return '<div class="os-connection-chip"><span class="os-service-icon">'+esc(initials)+'</span><div><strong>'+esc(c.name)+'</strong><em>'+esc(c.detail||c.kind||'')+'</em></div><i class="'+(c.connected?'online':'offline')+'"></i></div>';
}
function renderPlanner(){
  const el=$('#plannerTimeline'), data=state.os; if(!el||!data)return;
  const agenda=data.agenda||[];
  if(!agenda.length){el.innerHTML=osEmpty('Your planner is open','Add an event or ask Cagentic to design a week around your goals.','Create event','event');return;}
  let lastDay='';
  el.innerHTML=agenda.map(item=>{
    const d=new Date(Number(item.start_at)*1000), day=d.toLocaleDateString([],{weekday:'long',month:'long',day:'numeric'});
    const head=day!==lastDay?'<div class="os-day-divider">'+esc(day)+'</div>':''; lastDay=day;
    const actions=item.kind==='event'?'<button class="os-row-delete" data-event-delete="'+esc(item.id)+'" title="Delete">×</button>':'<button class="os-row-done" data-deadline-done="'+esc(item.id)+'">Done</button>';
    return head+'<article class="os-agenda-row '+esc(item.kind||'event')+'"><time>'+d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'})+'</time><i></i><div><strong>'+esc(item.title)+'</strong><span>'+esc(item.location||item.kind||'event')+'</span></div>'+actions+'</article>';
  }).join('');
}
function renderGoals(){
  const el=$('#goalsWorkspace'), data=state.os; if(!el||!data)return;
  const goals=data.goals||[];
  if(!goals.length){el.innerHTML=osEmpty('Your goals live here','Create a direction and Cagentic will help protect time for it.','Create your first goal','goal');return;}
  el.innerHTML=goals.map(g=>{
    const pct=Math.max(0,Math.min(100,Number(g.progress)||0));
    return '<article class="os-goal-detail '+esc(g.status||'active')+'"><div class="os-goal-ring" style="--pct:'+pct+'"><span>'+pct+'%</span></div><div class="os-goal-detail-copy"><span>'+esc((g.category||'personal').toUpperCase())+' · '+esc(g.status||'active')+'</span><h2>'+esc(g.title)+'</h2><p>'+esc(g.description||'No description yet. Ask Cagentic to turn this into a plan.')+'</p><div class="os-goal-actions"><button data-goal-step="'+esc(g.id)+'">Move +10%</button>'+(g.status!=='completed'?'<button data-goal-complete="'+esc(g.id)+'">Mark complete</button>':'')+'<button class="danger" data-goal-delete="'+esc(g.id)+'">Delete</button></div></div><div class="os-goal-target"><span>TARGET</span><strong>'+esc(g.target_at?osDate(g.target_at,false):'Open-ended')+'</strong></div></article>';
  }).join('');
}
function renderConnections(){
  const el=$('#connectionsGrid'), data=state.os; if(!el||!data)return;
  const items=data.integrations||[];
  el.innerHTML=items.length?items.map(c=>{
    const email=c.adapter==='imap', syncAttr=email?'data-email-sync':'data-connection-sync', deleteAttr=email?'data-email-delete':'data-connection-delete';
    return '<article class="os-connection-card '+(c.managed?'managed':'')+'"><div class="os-service-icon large">'+esc((c.name||'?').slice(0,2).toUpperCase())+'</div><div><span>'+esc(c.kind||'SERVICE')+'</span><h2>'+esc(c.name)+'</h2><p>'+esc(c.detail||'')+'</p>'+(c.endpoint?'<em>'+esc(c.endpoint)+'</em>':'')+'</div><div class="os-connection-status '+(c.connected?'online':'')+'"><i></i>'+(c.status==='error'?'Needs attention':c.connected?'Connected':'Available')+'</div>'+(c.managed?'<div class="os-connection-actions"><button '+syncAttr+'="'+esc(c.id)+'">Sync now</button><button class="danger" '+deleteAttr+'="'+esc(c.id)+'">Remove</button></div>':'')+'</article>';
  }).join(''):osEmpty('No services yet','Connect a calendar, inbox, or MCP server.');
}
function renderInbox(){
  const el=$('#inboxWorkspace'),data=state.os;if(!el||!data)return;const items=data.inbox||[];
  if(!items.length){el.innerHTML=osEmpty('Your inbox is clear','Capture a thought or connect email to bring unread headers into one place.','Capture something','inbox');return;}
  el.innerHTML=items.map(item=>'<article class="os-inbox-item '+esc(item.status||'new')+'"><div class="os-inbox-kind">'+esc((item.kind||'item').slice(0,1).toUpperCase())+'</div><div class="os-inbox-copy"><span>'+esc((item.kind||'item').toUpperCase())+(item.sender?' · '+esc(item.sender):'')+'</span><h2>'+esc(item.title)+'</h2><p>'+esc(item.summary||'Captured '+osAgo(item.received_at||item.created_at))+'</p></div><div class="os-inbox-actions">'+(item.status==='new'?'<button data-inbox-status="read" data-inbox-id="'+esc(item.id)+'">Read</button>':'')+(item.status!=='done'?'<button data-inbox-status="done" data-inbox-id="'+esc(item.id)+'">Done</button>':'')+'<button data-inbox-status="archived" data-inbox-id="'+esc(item.id)+'">Archive</button><button class="danger" data-inbox-delete="'+esc(item.id)+'">×</button></div></article>').join('');
}
function renderRoutines(){
  const el=$('#routinesWorkspace'),data=state.os;if(!el||!data)return;const items=data.routines||[];
  if(!items.length){el.innerHTML=osEmpty('No routines yet','Schedule a useful briefing and Cagentic will surface it as a notification.');return;}
  const dayNames=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  el.innerHTML=items.map(item=>'<article class="os-routine-card '+(item.enabled?'':'disabled')+'"><div class="os-routine-orb">↻</div><div><span>'+esc((item.kind||'custom').replaceAll('_',' ').toUpperCase())+'</span><h2>'+esc(item.name)+'</h2><p>'+esc((item.days||[]).map(d=>dayNames[d]).join(' · '))+' at '+esc(item.schedule_time||'08:00')+'</p><em>'+(item.last_run_at?'Last ran '+esc(osAgo(item.last_run_at)):item.next_run_at?'Next '+esc(osRelative(item.next_run_at)):'Paused')+'</em></div><div class="os-routine-actions"><button data-routine-run="'+esc(item.id)+'">Run now</button><button data-routine-toggle="'+esc(item.id)+'" data-routine-enabled="'+(item.enabled?'1':'0')+'">'+(item.enabled?'Pause':'Enable')+'</button><button class="danger" data-routine-delete="'+esc(item.id)+'">Delete</button></div></article>').join('');
}
function architectureCapability(id){
  const branches=state.os?.architecture?.branches||[];
  for(const branch of branches){const found=(branch.capabilities||[]).find(item=>item.id===id);if(found)return {...found,branch:branch.name};}
  return null;
}
function showArchitectureDetail(id){
  state.selectedCapability=id;const item=architectureCapability(id),detail=$('#architectureDetail');if(!detail||!item)return;
  detail.classList.remove('hidden');
  detail.innerHTML='<div><span>'+esc(item.branch.toUpperCase())+' / '+esc(item.kind.toUpperCase())+'</span><strong>'+esc(item.name)+'</strong><em>'+esc((item.tools||[]).join(' · '))+'</em></div><p>'+esc(item.prompt)+'</p><button>Hand to conductor →</button>';
  detail.querySelector('button').onclick=()=>{showOsView('assistant');send(item.prompt);};
  document.querySelectorAll('[data-capability-id]').forEach(el=>el.classList.toggle('selected',el.dataset.capabilityId===id));
}
function renderArchitecture(){
  const arch=state.os?.architecture;if(!arch)return;
  const status=arch.status||{},statusEl=$('#architectureStatus');
  if(statusEl)statusEl.innerHTML='<span><i></i> SYSTEM LIVE</span><em>'+Number(status.capabilities||0)+' CAPABILITIES · '+Number(status.installed_skills||0)+' INSTALLED SKILLS · '+Number(status.routines||0)+' ROUTINES · '+Number(status.integrations||0)+' INTEGRATIONS</em>';
  const model=$('#architectureModel');if(model)model.textContent=(arch.conductor?.model||'local model')+' · the conductor';
  const modes=$('#architectureModes');if(modes){
    modes.innerHTML='<button data-architecture-mode="all" aria-pressed="'+(state.architectureMode==='all')+'">ALL <em>'+Number(status.capabilities||0)+'</em></button>'+(arch.modes||[]).map(mode=>'<button class="'+esc(mode.id)+'" data-architecture-mode="'+esc(mode.id)+'" aria-pressed="'+(state.architectureMode===mode.id)+'">'+esc(mode.label.toUpperCase())+' <em>'+Number(mode.count||0)+'</em></button>').join('');
  }
  const branches=$('#architectureBranches');if(branches){
    branches.innerHTML=(arch.branches||[]).map(branch=>{
      const visible=(branch.capabilities||[]).filter(item=>state.architectureMode==='all'||item.kind===state.architectureMode);
      if(!visible.length)return '';
      return '<section class="architecture-branch '+(branch.foundation?'foundation ':'')+(branch.custom?'custom':'')+'"><header><span>'+esc(branch.name.toUpperCase())+'</span><em>'+visible.length+' SYSTEMS</em></header><div>'+visible.map(item=>'<button class="architecture-capability '+esc(item.kind)+(item.available?'':' unavailable')+(item.active?'':' dormant')+(state.selectedCapability===item.id?' selected':'')+'" data-capability-id="'+esc(item.id)+'" aria-label="'+esc(item.name+', '+item.kind+', '+(item.active?'active':'available on demand')+', tools: '+(item.tools||[]).join(', '))+'"><span>'+esc(item.name)+'</span><i>'+({manual:'◆',skill:'●',routine:'◷',agent:'✦'}[item.kind]||'·')+'</i></button>').join('')+'</div></section>';
    }).join('');
  }
  const automation=$('#automationLayer');if(automation){const a=arch.automation||{};automation.innerHTML='<header><span>AUTOMATION LAYER</span><em>'+Number(a.enabled_routines||0)+' ACTIVE ROUTINES</em></header><div>'+(a.triggers||[]).map(trigger=>'<span>↳ '+esc(trigger)+'</span>').join('')+'</div>';}
  const links=$('#architectureIntegrations');if(links){const items=arch.integrations||[];links.innerHTML=items.length?items.map(item=>'<button data-view-jump="connections"><i class="'+(item.connected?'online':'')+'"></i><span>'+esc(item.name)+'</span><em>'+esc(item.kind||item.adapter||'service')+'</em></button>').join(''):'<span class="architecture-no-links">Local foundation · SQLite · tools · model runtime</span>';}
  document.querySelectorAll('[data-architecture-mode]').forEach(el=>el.onclick=()=>{state.architectureMode=el.dataset.architectureMode;state.selectedCapability='';const detail=$('#architectureDetail');if(detail)detail.classList.add('hidden');renderArchitecture();});
  document.querySelectorAll('[data-capability-id]').forEach(el=>el.onclick=()=>showArchitectureDetail(el.dataset.capabilityId));
  document.querySelectorAll('#architectureIntegrations [data-view-jump]').forEach(el=>el.onclick=()=>showOsView(el.dataset.viewJump));
}
function renderNotifications(){
  const data=state.os||{}, count=Number(data.unread_notifications)||0, badge=$('#notificationCount');
  if(badge){badge.textContent=count>99?'99+':String(count);badge.classList.toggle('hidden',count===0);}
  const list=$('#notificationList'); if(!list)return;
  const items=data.notifications||[];
  list.innerHTML=items.length?items.map(item=>'<article class="os-notification '+(item.read?'read':'')+' '+esc(item.severity||'attention')+'"><div class="os-notification-dot"></div><div><time>'+esc(osAgo(item.created_at))+'</time><strong>'+esc(item.title)+'</strong><p>'+esc(item.body)+'</p><div class="os-notification-actions">'+(item.action_prompt?'<button data-notification-prompt="'+esc(item.action_prompt)+'" data-notification-id="'+esc(item.id)+'">Ask Cagentic</button>':'')+'<button data-notification-dismiss="'+esc(item.id)+'">Dismiss</button></div></div></article>').join(''):osEmpty('You are all caught up','Cagentic will notify you when something genuinely needs attention.');
}
function openNotifications(){$('#notificationPanel').classList.add('open');}
function closeNotifications(){$('#notificationPanel').classList.remove('open');}
function bindOsActions(){
  document.querySelectorAll('[data-open-capture]').forEach(el=>el.onclick=()=>openCapture(el.dataset.openCapture));
  document.querySelectorAll('[data-view-jump]').forEach(el=>el.onclick=()=>showOsView(el.dataset.viewJump));
  document.querySelectorAll('[data-ai-prompt]').forEach(el=>el.onclick=()=>{showOsView('assistant');send(el.dataset.aiPrompt);});
  document.querySelectorAll('[data-deadline-done]').forEach(el=>el.onclick=()=>updateOs('/api/os/deadlines/update',{id:el.dataset.deadlineDone,status:'done'}));
  document.querySelectorAll('[data-event-delete]').forEach(el=>el.onclick=()=>updateOs('/api/os/events/delete',{id:el.dataset.eventDelete}));
  document.querySelectorAll('[data-goal-step]').forEach(el=>el.onclick=()=>{
    const g=(state.os.goals||[]).find(x=>x.id===el.dataset.goalStep); if(g) updateOs('/api/os/goals/update',{id:g.id,progress:Math.min(100,(Number(g.progress)||0)+10)});
  });
  document.querySelectorAll('[data-goal-complete]').forEach(el=>el.onclick=()=>updateOs('/api/os/goals/update',{id:el.dataset.goalComplete,status:'completed'}));
  document.querySelectorAll('[data-goal-delete]').forEach(el=>el.onclick=()=>updateOs('/api/os/goals/delete',{id:el.dataset.goalDelete}));
  document.querySelectorAll('[data-connection-sync]').forEach(el=>el.onclick=async()=>{
    el.disabled=true;el.textContent='Syncing…';const r=await api('/api/os/integrations/sync',{id:el.dataset.connectionSync});if(r.os){state.os=r.os;renderPersonalOS();}
  });
  document.querySelectorAll('[data-connection-delete]').forEach(el=>el.onclick=()=>showConfirm('Remove this calendar connection? Imported events will be kept.',()=>updateOs('/api/os/integrations/delete',{id:el.dataset.connectionDelete})));
  document.querySelectorAll('[data-email-sync]').forEach(el=>el.onclick=async()=>{el.disabled=true;el.textContent='Syncing…';const r=await api('/api/os/email/sync',{id:el.dataset.emailSync});if(r.os){state.os=r.os;renderPersonalOS();}});
  document.querySelectorAll('[data-email-delete]').forEach(el=>el.onclick=()=>showConfirm('Remove this email connection? Imported inbox items will be kept.',()=>updateOs('/api/os/email/delete',{id:el.dataset.emailDelete})));
  document.querySelectorAll('[data-inbox-status]').forEach(el=>el.onclick=()=>updateOs('/api/os/inbox/update',{id:el.dataset.inboxId,status:el.dataset.inboxStatus}));
  document.querySelectorAll('[data-inbox-delete]').forEach(el=>el.onclick=()=>showConfirm('Permanently delete this inbox item?',()=>updateOs('/api/os/inbox/delete',{id:el.dataset.inboxDelete})));
  document.querySelectorAll('[data-routine-run]').forEach(el=>el.onclick=async()=>{el.disabled=true;el.textContent='Running…';await updateOs('/api/os/routines/run',{id:el.dataset.routineRun});});
  document.querySelectorAll('[data-routine-toggle]').forEach(el=>el.onclick=()=>updateOs('/api/os/routines/update',{id:el.dataset.routineToggle,enabled:el.dataset.routineEnabled!=='1'}));
  document.querySelectorAll('[data-routine-delete]').forEach(el=>el.onclick=()=>showConfirm('Delete this proactive routine?',()=>updateOs('/api/os/routines/delete',{id:el.dataset.routineDelete})));
  document.querySelectorAll('[data-notification-dismiss]').forEach(el=>el.onclick=async()=>{const r=await api('/api/os/notifications/action',{id:el.dataset.notificationDismiss,action:'dismiss'});state.os.notifications=r.notifications||[];state.os.unread_notifications=r.unread_notifications||0;renderNotifications();bindOsActions();});
  document.querySelectorAll('[data-notification-prompt]').forEach(el=>el.onclick=async()=>{await api('/api/os/notifications/action',{id:el.dataset.notificationId,action:'read'});closeNotifications();showOsView('assistant');send(el.dataset.notificationPrompt);});
}
async function updateOs(path,body){
  const result=await api(path,body);
  if(result.os){state.os=result.os;renderPersonalOS();}
  return result;
}
async function refreshPersonalOS(){
  try{state.os=await api('/api/os');renderPersonalOS();}catch(e){console.warn('OS refresh failed',e);}
}

async function exportCalendar(){
  const response=await fetch('/api/os/calendar/export.ics');
  if(!response.ok)return;
  const blob=await response.blob(), url=URL.createObjectURL(blob), link=document.createElement('a');
  link.href=url;link.download='cagentic-calendar.ics';document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}

function openConnectionModal(){
  $('#connectionName').value='';$('#connectionKind').value='ical';$('#connectionUrl').value='';$('#connectionUsername').value='';$('#connectionPassword').value='';$('#connectionAutoSync').checked=true;$('#connectionError').classList.add('hidden');selectConnectionKind();$('#connectionModal').classList.remove('hidden');setTimeout(()=>$('#connectionName').focus(),40);
}
function closeConnectionModal(){$('#connectionModal').classList.add('hidden');}
function selectConnectionKind(){const caldav=$('#connectionKind').value==='caldav';$('#connectionAuthFields').classList.toggle('hidden',!caldav);$('#connectionUrl').placeholder=caldav?'https://calendar.example.com/dav/calendars/you/main/':'https://…/calendar.ics';}
async function saveConnection(){
  const err=$('#connectionError'), body={name:$('#connectionName').value.trim(),kind:$('#connectionKind').value,url:$('#connectionUrl').value.trim(),username:$('#connectionUsername').value,password:$('#connectionPassword').value,direction:$('#connectionKind').value==='caldav'?$('#connectionDirection').value:'pull',auto_sync:$('#connectionAutoSync').checked};
  if(!body.name||!body.url){err.textContent='Name and calendar URL are required.';err.classList.remove('hidden');return;}
  $('#connectionSave').disabled=true;$('#connectionSave').textContent='Connecting…';
  try{
    const created=await api('/api/os/integrations/create',body);if(!created.ok)throw new Error(created.error||'Could not save connection');
    const synced=await api('/api/os/integrations/sync',{id:created.connection.id});state.os=synced.os||created.os;renderPersonalOS();
    if(!synced.ok)throw new Error(synced.error||'Connection saved, but the first sync failed.');
    closeConnectionModal();
  }catch(e){err.textContent=e.message||String(e);err.classList.remove('hidden');}
  finally{$('#connectionSave').disabled=false;$('#connectionSave').textContent='Connect & sync';}
}

function openEmailModal(){
  $('#emailName').value='';$('#emailHost').value='';$('#emailPort').value='993';$('#emailUsername').value='';$('#emailPassword').value='';$('#emailSsl').checked=true;$('#emailError').classList.add('hidden');$('#emailModal').classList.remove('hidden');setTimeout(()=>$('#emailName').focus(),40);
}
function closeEmailModal(){$('#emailModal').classList.add('hidden');}
async function saveEmailConnection(){
  const err=$('#emailError'),body={name:$('#emailName').value.trim(),host:$('#emailHost').value.trim(),port:Number($('#emailPort').value)||993,username:$('#emailUsername').value.trim(),password:$('#emailPassword').value,use_ssl:$('#emailSsl').checked,auto_sync:true};
  if(!body.name||!body.host||!body.username){err.textContent='Name, IMAP host, and username are required.';err.classList.remove('hidden');return;}
  $('#emailSave').disabled=true;$('#emailSave').textContent='Connecting…';
  try{
    const created=await api('/api/os/email/create',body);if(!created.ok)throw new Error(created.error||'Could not save connection');
    const synced=await api('/api/os/email/sync',{id:created.connection.id});state.os=synced.os||created.os;renderPersonalOS();
    if(!synced.ok)throw new Error(synced.error||'Connection saved, but the first sync failed.');
    closeEmailModal();
  }catch(e){err.textContent=e.message||String(e);err.classList.remove('hidden');}
  finally{$('#emailSave').disabled=false;$('#emailSave').textContent='Connect & sync';}
}

function openRoutineModal(){
  $('#routineName').value='';$('#routineKind').value='daily_plan';$('#routineTime').value='08:00';$('#routinePrompt').value='';$('#routineError').classList.add('hidden');
  document.querySelectorAll('#routineDays input').forEach(el=>{el.checked=Number(el.value)<5;});
  $('#routineModal').classList.remove('hidden');setTimeout(()=>$('#routineName').focus(),40);
}
function closeRoutineModal(){$('#routineModal').classList.add('hidden');}
async function saveRoutine(){
  const err=$('#routineError'),days=[...document.querySelectorAll('#routineDays input:checked')].map(el=>Number(el.value));
  const body={name:$('#routineName').value.trim(),kind:$('#routineKind').value,schedule_time:$('#routineTime').value,days,prompt:$('#routinePrompt').value.trim(),enabled:true};
  if(!body.name||!body.schedule_time||!days.length){err.textContent='Name, local time, and at least one day are required.';err.classList.remove('hidden');return;}
  $('#routineSave').disabled=true;
  try{const result=await api('/api/os/routines/create',body);if(!result.ok)throw new Error(result.error||'Could not create routine');state.os=result.os;closeRoutineModal();renderPersonalOS();}
  catch(e){err.textContent=e.message||String(e);err.classList.remove('hidden');}
  finally{$('#routineSave').disabled=false;}
}

let _captureType='event';
function localInputValue(date){const off=date.getTimezoneOffset();return new Date(date.getTime()-off*60000).toISOString().slice(0,16);}
function selectCaptureType(type){
  _captureType=['inbox','event','deadline','goal'].includes(type)?type:'inbox';
  document.querySelectorAll('[data-capture-type]').forEach(el=>el.classList.toggle('active',el.dataset.captureType===_captureType));
  document.querySelectorAll('[data-fields]').forEach(el=>el.classList.toggle('hidden',el.dataset.fields!==_captureType));
}
function openCapture(type='event'){
  selectCaptureType(type); $('#captureTitle').value=''; $('#captureNotes').value=''; $('#captureError').classList.add('hidden');
  const start=new Date(); start.setMinutes(Math.ceil(start.getMinutes()/30)*30,0,0); const end=new Date(start.getTime()+3600000);
  $('#captureStart').value=localInputValue(start); $('#captureEnd').value=localInputValue(end); $('#captureDue').value=localInputValue(end); $('#captureTarget').value='';
  $('#captureModal').classList.remove('hidden'); setTimeout(()=>$('#captureTitle').focus(),40);
}
function closeCapture(){$('#captureModal').classList.add('hidden');}
async function saveCapture(){
  const title=$('#captureTitle').value.trim(), notes=$('#captureNotes').value.trim(), err=$('#captureError');
  if(!title){err.textContent='Give this item a title.';err.classList.remove('hidden');return;}
  let path,body={title};
  if(_captureType==='inbox'){path='/api/os/inbox/create';body={...body,summary:notes,kind:'capture'};}
  else if(_captureType==='event'){path='/api/os/events/create';body={...body,start_at:$('#captureStart').value,end_at:$('#captureEnd').value,location:$('#captureLocation').value,description:notes};}
  else if(_captureType==='deadline'){path='/api/os/deadlines/create';body={...body,due_at:$('#captureDue').value};}
  else {path='/api/os/goals/create';body={...body,target_at:$('#captureTarget').value,category:$('#captureCategory').value,description:notes};}
  try{
    const result=await api(path,body); if(!result.ok)throw new Error(result.error||'Could not save');
    state.os=result.os; closeCapture(); renderPersonalOS();
  }catch(e){err.textContent=e.message||String(e);err.classList.remove('hidden');}
}

// ---- HUD --------------------------------------------------------------------
const HUD_RX = /```hud\s*\n?([\s\S]*?)```/g;
function extractHud(text){
  const out=[]; let m;
  HUD_RX.lastIndex=0;
  while((m=HUD_RX.exec(text||''))!==null){
    try{ out.push({raw:m[1].trim(), obj:JSON.parse(m[1].trim())}); }catch(e){}
  }
  return out;
}
function stripHud(text){ return (text||'').replace(HUD_RX,'').trim(); }

// Panel types that render inline in the chat and the user can act on.
const INTERACTIVE_TYPES=new Set(['actions','choices','form','checklist']);
function renderPanels(text){
  const found=extractHud(text);
  if(!found.length) return;
  // Handle clear directives first
  found.forEach(({obj})=>{ if((obj.panel||'').toLowerCase()==='clear') clearViewport(); });
  const layer=$('#windowLayer');
  found.forEach(({raw,obj})=>{
    const type=(obj.panel||'').toLowerCase();
    if(type==='clear') return;
    if(state.renderedPanels.has(raw)) return;
    // Interactive panels live inline in the conversation thread.
    if(INTERACTIVE_TYPES.has(type)){
      const el=buildInteractive(obj); if(!el) return;
      state.renderedPanels.add(raw);
      getThread().appendChild(el); scrollDown();
      return;
    }
    // Data panels render as draggable floating windows.
    const inner=buildPanelInner(obj); if(!inner) return;
    state.renderedPanels.add(raw);
    const idx=_winCascade; // capture before _nextWinPos increments
    const pos=_nextWinPos();
    const win=document.createElement('div'); win.className='hud-window';
    win.style.cssText='left:'+pos.x+'px;top:'+pos.y+'px;--i:'+idx;
    const title=obj.title||(type.charAt(0).toUpperCase()+type.slice(1));
    win.innerHTML='<div class="hud-win-head"><span class="hud-win-title">'+esc(title)+'</span>'+
      '<button class="hud-win-close" title="Close">&times;</button></div>'+
      '<div class="hud-win-body">'+inner+'</div>'+
      '<div class="hud-win-resize"></div>';
    win.querySelector('.hud-win-close').addEventListener('pointerdown',e=>{e.stopPropagation();_closeWindow(win);});
    layer.appendChild(win);
    _initWindow(win);
  });
}
// ---- INTERACTIVE WIDGETS ----------------------------------------------------
function _sendFromWidget(text){
  text=(text||'').trim();
  if(!text||state.busy) return;
  if(log.querySelector('.j-empty')) clearLog();
  send(text);
}
function _markUsed(wrap, activeEl){
  wrap.classList.add('ix-used');
  if(activeEl) activeEl.classList.add('ix-active');
  wrap.querySelectorAll('button,input').forEach(el=>{ el.disabled=true; });
}
function buildInteractive(p){
  const type=(p.panel||'').toLowerCase();
  const wrap=document.createElement('div');
  wrap.className='ix-panel ix-'+type;
  const title=p.title?'<div class="ix-title">'+esc(p.title)+'</div>':'';
  if(type==='actions'){
    const btns=(p.buttons||p.items||[]);
    wrap.innerHTML=title+'<div class="ix-actions-row">'+btns.map((b,i)=>{
      const label=typeof b==='string'?b:(b.label||b.prompt||'');
      return '<button class="ix-btn" data-i="'+i+'">'+esc(label)+'</button>';
    }).join('')+'</div>';
    wrap.querySelectorAll('.ix-btn').forEach(btn=>{ btn.onclick=()=>{
      const b=btns[+btn.dataset.i];
      const prompt=typeof b==='string'?b:(b.prompt||b.send||b.label||'');
      _markUsed(wrap,btn); _sendFromWidget(prompt);
    }; });
  } else if(type==='choices'){
    const opts=(p.options||p.items||[]); const pre=p.prompt||'';
    wrap.innerHTML=title+'<div class="ix-choices-list">'+opts.map((o,i)=>{
      const label=typeof o==='string'?o:(o.label||'');
      return '<button class="ix-choice" data-i="'+i+'"><span class="ix-choice-mark">&#9656;</span>'+esc(label)+'</button>';
    }).join('')+'</div>';
    wrap.querySelectorAll('.ix-choice').forEach(btn=>{ btn.onclick=()=>{
      const o=opts[+btn.dataset.i];
      const val=typeof o==='string'?o:(o.label||'');
      const prompt=(typeof o==='object'&&o.prompt)?o.prompt:(pre+val);
      _markUsed(wrap,btn); _sendFromWidget(prompt);
    }; });
  } else if(type==='form'){
    const fields=(p.fields||[]); const btnLabel=p.button||'Submit';
    wrap.innerHTML=title+'<div class="ix-form-fields">'+fields.map((f,i)=>{
      const name=f.name||('field'+i);
      const lab=f.label?'<label class="ix-flabel">'+esc(f.label)+'</label>':'';
      return '<div class="ix-field">'+lab+'<input class="ix-input" data-name="'+esc(name)+'" placeholder="'+esc(f.placeholder||'')+'" value="'+esc(f.value||'')+'"/></div>';
    }).join('')+'</div><button class="ix-submit">'+esc(btnLabel)+'</button>';
    const submit=()=>{
      const vals={};
      wrap.querySelectorAll('.ix-input').forEach(inp=>{ vals[inp.dataset.name]=inp.value.trim(); });
      let out=p.submit||p.prompt||'';
      if(out) out=out.replace(/\{(\w+)\}/g,(m,k)=>vals[k]!==undefined?vals[k]:m);
      else out=Object.values(vals).filter(Boolean).join(' ');
      _markUsed(wrap,wrap.querySelector('.ix-submit')); _sendFromWidget(out);
    };
    wrap.querySelector('.ix-submit').onclick=submit;
    wrap.querySelectorAll('.ix-input').forEach(inp=>inp.addEventListener('keydown',e=>{
      if(e.key==='Enter'){ e.preventDefault(); submit(); }
    }));
  } else if(type==='checklist'){
    const items=(p.items||[]);
    wrap.innerHTML=title+'<div class="ix-checklist-list">'+items.map(it=>{
      const label=typeof it==='string'?it:(it.label||'');
      const done=(typeof it==='object'&&it.done);
      return '<label class="ix-check'+(done?' checked':'')+'"><input type="checkbox" '+(done?'checked':'')+'/><span class="ix-box"></span><span class="ix-clabel">'+esc(label)+'</span></label>';
    }).join('')+'</div>';
    wrap.querySelectorAll('.ix-check').forEach(c=>{
      const cb=c.querySelector('input');
      cb.addEventListener('change',()=>c.classList.toggle('checked',cb.checked));
    });
  } else { return null; }
  return wrap;
}
function buildPanelInner(p){
  if(!p||typeof p!=='object') return null;
  const title='';
  let inner='';
  switch((p.panel||'').toLowerCase()){
    case 'stats':
      inner=(p.items||[]).map(it=>'<div class="vp-stat-row"><span class="l">'+esc(it.label||'')+
        '</span><span class="v '+(it.accent||'')+'">'+esc(String(it.value??''))+'</span></div>').join('');
      break;
    case 'metric':
      inner='<div class="vp-metric"><div class="big">'+esc(String(p.value??''))+
        (p.unit?'<span class="unit">'+esc(p.unit)+'</span>':'')+'</div>'+
        (p.trend?'<div class="trend '+esc(p.trend)+'">'+({up:'▲ RISING',down:'▼ FALLING',flat:'■ STABLE'}[p.trend]||'')+'</div>':'')+
        (p.sub?'<div class="sub">'+esc(p.sub)+'</div>':'')+'</div>';
      break;
    case 'list':
      inner='<ul class="vp-list">'+(p.items||[]).map(i=>'<li>'+esc(String(i))+'</li>').join('')+'</ul>';
      break;
    case 'table':
      inner='<table class="vp-table"><thead><tr>'+(p.columns||[]).map(c=>'<th>'+esc(c)+'</th>').join('')+
        '</tr></thead><tbody>'+(p.rows||[]).map(r=>'<tr>'+r.map(c=>'<td>'+esc(String(c))+'</td>').join('')+'</tr>').join('')+'</tbody></table>';
      break;
    case 'image':
      inner='<div class="vp-image"><img src="'+esc(safeImgUrl(p.url||''))+'" alt="" onerror="this.style.display=\'none\'"/>'+
        (p.caption?'<div class="cap">'+esc(p.caption)+'</div>':'')+'</div>';
      break;
    case 'web':
      inner=(p.results||[]).map(r=>'<div class="vp-web-item">'+
        '<a href="'+esc(r.url||'#')+'" target="_blank" rel="noopener">'+esc(r.title||r.url||'')+'</a>'+
        (r.url?'<div class="url">'+esc(r.url)+'</div>':'')+
        (r.snippet?'<div class="snip">'+esc(r.snippet)+'</div>':'')+'</div>').join('');
      break;
    case 'alert':
      const lvl=(p.level||'info').toLowerCase();
      return '<div class="vp-alert '+lvl+'"><div class="at">'+esc(p.title||lvl.toUpperCase())+
        '</div><div class="ax">'+esc(p.text||'')+'</div></div>';
    case 'progress':
      inner=(p.items||[]).map(it=>{const pct=Math.max(0,Math.min(100,+it.pct||0));
        return '<div class="vp-prog-row"><div class="pl"><span>'+esc(it.label||'')+'</span><span>'+pct+'%</span></div>'+
        '<div class="vp-prog-bar"><div class="vp-prog-fill" style="width:'+pct+'%"></div></div></div>';}).join('');
      break;
    case 'map':
      inner='<div class="vp-map"><div class="mgrid"></div><div class="crosshair"></div>'+
        '<div class="mlabel">'+esc(p.label||((p.lat??'?')+', '+(p.lon??'?')))+'</div></div>';
      break;
    case 'bar':{ const vals=(p.values||[]).map(Number); const labs=p.labels||vals.map((_,i)=>String(i+1));
      const maxV=Math.max(...vals,1); const col=p.color||'#c79bd8';
      const W2=320,H2=160,padL=36,padR=10,padT=14,padB=22;
      const plotW=W2-padL-padR, plotH=H2-padT-padB;
      const bw=Math.max(10,Math.min(36,Math.floor(plotW/Math.max(vals.length,1)*0.6)));
      const gap=Math.floor(plotW/Math.max(vals.length,1));
      // grid lines
      let grid='';
      for(let g=0;g<=4;g++){
        const gy=padT+plotH*(1-g/4);
        const gv=(maxV*g/4);
        grid+=`<line x1="${padL}" y1="${gy}" x2="${W2-padR}" y2="${gy}" stroke="#2a2235" stroke-width="1"/>`;
        grid+=`<text x="${padL-4}" y="${gy+3}" text-anchor="end" font-size="8" fill="#6b5f7a">${gv%1===0?gv:gv.toFixed(1)}</text>`;
      }
      // gradient def
      const gid='bg'+(_winCascade||0);
      let bars=grid;
      bars+=`<defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${esc(col)}" stop-opacity="1"/><stop offset="100%" stop-color="${esc(col)}" stop-opacity="0.45"/></linearGradient></defs>`;
      vals.forEach((v,i)=>{
        const bh=Math.round((v/maxV)*plotH); const x=padL+i*gap+(gap-bw)/2; const y=padT+plotH-bh;
        bars+=`<rect x="${x}" y="${y}" width="${bw}" height="${bh}" fill="url(#${gid})" rx="3" ry="3"/>`;
        bars+=`<text x="${x+bw/2}" y="${H2-4}" text-anchor="middle" font-size="9" fill="#b0a6ba">${esc(String(labs[i]||''))}</text>`;
        bars+=`<text x="${x+bw/2}" y="${y-4}" text-anchor="middle" font-size="8" font-weight="600" fill="${esc(col)}">${esc(String(v))}</text>`;
      });
      inner=`<svg viewBox="0 0 ${W2} ${H2}" style="width:100%;height:auto">${bars}</svg>`; break; }
    case 'line':{ const ds=(p.datasets||[{values:p.values||[],label:'',color:'#c79bd8'}]);
      const labs=p.labels||[];  const maxAll=Math.max(...ds.flatMap(d=>d.values||[]).map(Number),1);
      const W2=320,H2=160,padL=36,padR=10,padT=14,padB=22;
      const plotW=W2-padL-padR, plotH=H2-padT-padB;
      let lines=''; const colors=['#c79bd8','#8ecf95','#e3a978','#c97fd4','#e5928f'];
      // grid
      for(let g=0;g<=4;g++){
        const gy=padT+plotH*(1-g/4); const gv=(maxAll*g/4);
        lines+=`<line x1="${padL}" y1="${gy}" x2="${W2-padR}" y2="${gy}" stroke="#2a2235" stroke-width="1"/>`;
        lines+=`<text x="${padL-4}" y="${gy+3}" text-anchor="end" font-size="8" fill="#6b5f7a">${gv%1===0?gv:gv.toFixed(1)}</text>`;
      }
      ds.forEach((d,di)=>{ const vals=(d.values||[]).map(Number); const col=d.color||colors[di%colors.length];
        if(!vals.length) return;
        const pts=vals.map((v,i)=>{const x=padL+i*plotW/Math.max(vals.length-1,1); const y=padT+plotH*(1-v/maxAll); return `${x},${y}`;});
        // area fill
        const areaPts=[`${padL},${padT+plotH}`,...pts,`${padL+plotW},${padT+plotH}`].join(' ');
        const aid='la'+(_winCascade||0)+di;
        lines+=`<defs><linearGradient id="${aid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${esc(col)}" stop-opacity="0.25"/><stop offset="100%" stop-color="${esc(col)}" stop-opacity="0.02"/></linearGradient></defs>`;
        lines+=`<polygon points="${areaPts}" fill="url(#${aid})"/>`;
        lines+=`<polyline points="${pts.join(' ')}" fill="none" stroke="${esc(col)}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>`;
        pts.forEach((pt,i)=>{ const[x,y]=pt.split(',');
          lines+=`<circle cx="${x}" cy="${y}" r="3.5" fill="#16111c" stroke="${esc(col)}" stroke-width="2"/>`; });
        if(d.label){ const lastPt=pts[pts.length-1].split(',');
          lines+=`<text x="${+lastPt[0]+6}" y="${+lastPt[1]+3}" font-size="9" font-weight="600" fill="${esc(col)}">${esc(d.label)}</text>`; }
      });
      labs.forEach((l,i)=>{ const x=padL+i*plotW/Math.max(labs.length-1,1);
        lines+=`<text x="${x}" y="${H2-4}" text-anchor="middle" font-size="9" fill="#b0a6ba">${esc(String(l))}</text>`; });
      inner=`<svg viewBox="0 0 ${W2} ${H2}" style="width:100%;height:auto">${lines}</svg>`; break; }
    case 'pie':{ const vals=(p.values||[]).map(Number); const labs=p.labels||vals.map((_,i)=>String(i+1));
      const total=vals.reduce((a,b)=>a+b,0)||1;
      const colors=['#c79bd8','#8ecf95','#e3a978','#c97fd4','#e5928f','#b0a6ba','#7ec8e3','#d4a76a'];
      const cx=100,cy=80,r=62,ri=32; let angle=-Math.PI/2; let slices=''; let legend='';
      // shadow ring
      slices+=`<circle cx="${cx+1}" cy="${cy+2}" r="${r+2}" fill="none" stroke="#0a0810" stroke-width="4" opacity="0.4"/>`;
      vals.forEach((v,i)=>{ const sweep=2*Math.PI*(v/total); const col=colors[i%colors.length];
        const mid=angle+sweep/2;
        const x1=cx+r*Math.cos(angle),y1=cy+r*Math.sin(angle);
        const x2=cx+r*Math.cos(angle+sweep),y2=cy+r*Math.sin(angle+sweep);
        const xi1=cx+ri*Math.cos(angle),yi1=cy+ri*Math.sin(angle);
        const xi2=cx+ri*Math.cos(angle+sweep),yi2=cy+ri*Math.sin(angle+sweep);
        const lg=sweep>Math.PI?1:0;
        // slight explode for large slices
        const ex=sweep>0.3?2*Math.cos(mid):0, ey=sweep>0.3?2*Math.sin(mid):0;
        slices+=`<path d="M${xi1+ex} ${yi1+ey} L${x1+ex} ${y1+ey} A${r} ${r} 0 ${lg} 1 ${x2+ex} ${y2+ey} L${xi2+ex} ${yi2+ey} A${ri} ${ri} 0 ${lg} 0 ${xi1+ex} ${yi1+ey}" fill="${col}" opacity="0.9" stroke="#16111c" stroke-width="1"/>`;
        // percentage label inside slice
        if(sweep>0.25){
          const lr=(r+ri)/2, lx=cx+lr*Math.cos(mid)+ex, ly=cy+lr*Math.sin(mid)+ey;
          const pct=Math.round(v/total*100);
          slices+=`<text x="${lx}" y="${ly+3}" text-anchor="middle" font-size="9" font-weight="600" fill="#f0eaf2">${pct}%</text>`;
        }
        const pct=Math.round(v/total*100);
        legend+=`<rect x="190" y="${8+i*18}" width="10" height="10" rx="2" fill="${col}"/>`;
        legend+=`<text x="204" y="${17+i*18}" font-size="10" fill="#cdbbd8">${esc(String(labs[i]))} <tspan fill="#8a7e96">${pct}%</tspan></text>`;
        angle+=sweep; });
      // center label
      slices+=`<circle cx="${cx}" cy="${cy}" r="${ri-4}" fill="#16111c" opacity="0.6"/>`;
      inner=`<svg viewBox="0 0 320 165" style="width:100%;height:auto">${slices}${legend}</svg>`; break; }
    case 'stocks':{
      const inner=buildStocksCard(p);
      if(!inner) return null;
      return title+inner;
    }
    case 'crypto':{
      const inner=buildCryptoCard(p);
      if(!inner) return null;
      return title+inner;
    }
    case 'weather':{
      const inner=buildWeatherCard(p);
      if(!inner) return null;
      return title+inner;
    }
    case 'sports':{
      const inner=buildSportsCard(p);
      if(!inner) return null;
      return title+inner;
    }
    case 'calendar':{
      const inner=buildCalendarCard(p);
      if(!inner) return null;
      return title+inner;
    }
    default: return null;
  }
  return title+inner;
}

// ---- SPECIALTY WIDGETS (stocks, weather, crypto, sports, calendar) --------
//
// `show_widget` is the agent-facing tool. It emits an SSE `widget` event with
// {type, title, data}; the frontend drops it into a draggable HUD window.
// We share the renderer with the inline `hud` panels (panels also use
// {panel: 'stocks', ...}) so the model can pick either channel.
function _fmtNum(n, dp){
  const x=Number(n); if(!isFinite(x)) return '—';
  if(dp===undefined){
    if(Math.abs(x)>=1000) return x.toLocaleString('en-US',{maximumFractionDigits:0});
    return x.toLocaleString('en-US',{maximumFractionDigits:2});
  }
  return x.toLocaleString('en-US',{minimumFractionDigits:dp,maximumFractionDigits:dp});
}
// ============================================================================
// SPECIALTY WIDGETS — stocks, crypto, weather, sports, calendar
// ============================================================================
//
// `show_widget` is the agent-facing tool. It emits an SSE `widget` event with
// {type, title, data}; the frontend drops it into a draggable HUD window.
// We share the renderer with the inline `hud` panels (panels also use
// {panel: 'stocks', ...}) so the model can pick either channel.
//
// The cards here are designed to look like small versions of the real apps:
//   * stocks / crypto  → Robinhood × Bloomberg-terminal feel
//   * weather          → Apple-Weather feel with condition-tinted header
//   * sports           → ESPN scoreboard
//   * calendar         → Google Calendar × Fantastical timeline

function _fmtVol(n){
  const x=Number(n); if(!isFinite(x)) return '—';
  const a=Math.abs(x);
  if(a>=1e12) return (x/1e12).toFixed(2)+'T';
  if(a>=1e9)  return (x/1e9).toFixed(2)+'B';
  if(a>=1e6)  return (x/1e6).toFixed(2)+'M';
  if(a>=1e3)  return (x/1e3).toFixed(1)+'K';
  return String(x);
}
function _fmtBig(n){ // for mkt cap, p/e, etc.
  const x=Number(n); if(!isFinite(x)) return '—';
  const a=Math.abs(x);
  if(a>=1e12) return (x/1e12).toFixed(2)+'T';
  if(a>=1e9)  return (x/1e9).toFixed(2)+'B';
  if(a>=1e6)  return (x/1e6).toFixed(1)+'M';
  return x.toLocaleString('en-US',{maximumFractionDigits:0});
}
function _fmtPct(n, dp){
  const x=Number(n); if(!isFinite(x)) return '—';
  return x.toFixed(dp===undefined?2:dp)+'%';
}
function _greetingColor(n){
  // color by % change; thresholds tuned for a peach/rose palette
  const x=Number(n);
  if(!isFinite(x)) return 'var(--text-2)';
  if(x>=1) return 'var(--ok)';
  if(x<=-1) return 'var(--hot)';
  return 'var(--text-2)';
}
function _marketState(now){
  // Heuristic US-market state from local hour (server clock, no TZ awareness).
  const d=new Date(now||Date.now());
  const day=d.getDay(); if(day===0||day===6) return {open:false,label:'CLOSED · WKND'};
  const h=d.getHours(), m=d.getMinutes();
  const t=h*60+m;
  if(t<4*60)        return {open:false,label:'CLOSED'};
  if(t<9*60+30)     return {open:false,label:'PRE-MKT'};
  if(t<16*60)       return {open:true, label:'● OPEN'};
  if(t<20*60)       return {open:false,label:'AFTER-HRS'};
  return {open:false,label:'CLOSED'};
}

// --- SVG chart primitives ---
function _uid(p){ return (p||'id')+Math.random().toString(36).slice(2,8); }

function _sparkSVG(vals, color, w, h){
  // Compact gradient-filled line chart, like the existing 'line' panel but
  // tighter and used as a hero accent for stocks/crypto.
  const vs=(vals||[]).map(Number).filter(v=>isFinite(v));
  if(vs.length<2) return '';
  const W=w||120, H=h||34, padL=2, padR=2, padT=3, padB=3;
  const min=Math.min(...vs), max=Math.max(...vs);
  const span=Math.max(max-min, 1e-9);
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const xFor=i=>padL+i*plotW/(vs.length-1);
  const yFor=v=>padT+plotH-(v-min)/span*plotH;
  const pts=vs.map((v,i)=>xFor(i)+','+yFor(v));
  const area=[xFor(0)+','+(padT+plotH), ...pts, xFor(vs.length-1)+','+(padT+plotH)].join(' ');
  const gid=_uid('spk');
  const last=vs[vs.length-1], first=vs[0];
  const up=last>=first;
  const stroke=up?color:'#e5928f';
  return '<svg viewBox="0 0 '+W+' '+H+'" class="vp-spark" preserveAspectRatio="none">'+
    '<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="0" y2="1">'+
    '<stop offset="0%" stop-color="'+stroke+'" stop-opacity=".35"/>'+
    '<stop offset="100%" stop-color="'+stroke+'" stop-opacity="0"/></linearGradient></defs>'+
    '<polygon points="'+area+'" fill="url(#'+gid+')"/>'+
    '<polyline points="'+pts.join(' ')+'" fill="none" stroke="'+stroke+'" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'+
    '<circle cx="'+xFor(vs.length-1)+'" cy="'+yFor(last)+'" r="2.4" fill="#16111c" stroke="'+stroke+'" stroke-width="1.5"/>'+
    '</svg>';
}

function _areaChartSVG(vals, opts){
  // Bigger chart: gridlines, area+line, current-price marker on the right.
  // opts: {w, h, color, showAxis, padL, padR, padT, padB, gradient}
  const vs=(vals||[]).map(Number).filter(v=>isFinite(v));
  if(vs.length<2) return '';
  const o=opts||{};
  const W=o.w||480, H=o.h||160;
  const padL=o.padL||36, padR=o.padR||52, padT=o.padT||10, padB=o.padB||18;
  const stroke=o.color||'#c79bd8';
  const min=Math.min(...vs), max=Math.max(...vs);
  const span=Math.max(max-min, 1e-9);
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const xFor=i=>padL+i*plotW/(vs.length-1);
  const yFor=v=>padT+plotH-(v-min)/span*plotH;
  const pts=vs.map((v,i)=>[xFor(i),yFor(v)]);
  const area='M '+xFor(0)+' '+(padT+plotH)+' L '+pts.map(p=>p[0]+' '+p[1]).join(' L ')+' L '+xFor(vs.length-1)+' '+(padT+plotH)+' Z';
  const line=pts.map((p,i)=>(i?'L':'M')+' '+p[0]+' '+p[1]).join(' ');
  const gid=_uid('ar');
  const last=vs[vs.length-1], first=vs[0];
  const up=last>=first;
  const lineStroke=up?stroke:'#e5928f';
  // gridlines: 4 horizontal, plus min/max labels on the y-axis
  const grid=[];
  for(let g=0; g<=4; g++){
    const y=padT+(g/4)*plotH;
    const val=max-(g/4)*span;
    grid.push('<line x1="'+padL+'" y1="'+y+'" x2="'+(W-padR)+'" y2="'+y+'" stroke="rgba(199,155,216,.08)" stroke-width="1" stroke-dasharray="'+(g===0||g===4?'0':'2 3')+'"/>');
    if(o.showAxis!==false){
      grid.push('<text x="'+(padL-6)+'" y="'+(y+3)+'" font-size="9" fill="#7d7388" text-anchor="end" font-family="inherit">'+_fmtNum(val, val<10?2:0)+'</text>');
    }
  }
  // current-price marker on the right
  const lastY=yFor(last);
  const marker=o.gradient===false ? '' : (
    '<line x1="'+xFor(vs.length-1)+'" y1="'+padT+'" x2="'+xFor(vs.length-1)+'" y2="'+(padT+plotH)+'" stroke="'+lineStroke+'" stroke-width="1" stroke-dasharray="2 2" opacity=".6"/>'+
    '<rect x="'+(W-padR+2)+'" y="'+(lastY-9)+'" width="'+(padR-6)+'" height="18" rx="2" fill="'+lineStroke+'" opacity=".95"/>'+
    '<text x="'+(W-padR/2-2)+'" y="'+(lastY+4)+'" font-size="10" fill="#16111c" text-anchor="middle" font-weight="700">'+_fmtNum(last,2)+'</text>'
  );
  return '<svg viewBox="0 0 '+W+' '+H+'" class="st-chart-svg" preserveAspectRatio="none">'+
    '<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="0" y2="1">'+
    '<stop offset="0%" stop-color="'+lineStroke+'" stop-opacity=".45"/>'+
    '<stop offset="100%" stop-color="'+lineStroke+'" stop-opacity="0"/></linearGradient></defs>'+
    grid.join('')+
    '<path d="'+area+'" fill="url(#'+gid+')"/>'+
    '<path d="'+line+'" fill="none" stroke="'+lineStroke+'" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'+
    '<circle cx="'+xFor(vs.length-1)+'" cy="'+lastY+'" r="3" fill="#16111c" stroke="'+lineStroke+'" stroke-width="1.8"/>'+
    marker+
  '</svg>';
}

function _candleChartSVG(ohlc, opts){
  // ohlc: array of {o,h,l,c}. Pure-SVG candlesticks.
  const vs=(ohlc||[]).filter(c=>c&&isFinite(c.o)&&isFinite(c.h)&&isFinite(c.l)&&isFinite(c.c));
  if(vs.length<2) return '';
  const o=opts||{};
  const W=o.w||480, H=o.h||160;
  const padL=o.padL||36, padR=o.padR||12, padT=o.padT||8, padB=o.padB||12;
  const min=Math.min(...vs.map(c=>c.l));
  const max=Math.max(...vs.map(c=>c.h));
  const span=Math.max(max-min, 1e-9);
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const yFor=v=>padT+plotH-(v-min)/span*plotH;
  const colW=plotW/vs.length;
  const bodyW=Math.max(2, colW*0.62);
  // gridlines
  const grid=[];
  for(let g=0; g<=4; g++){
    const y=padT+(g/4)*plotH;
    const val=max-(g/4)*span;
    grid.push('<line x1="'+padL+'" y1="'+y+'" x2="'+(W-padR)+'" y2="'+y+'" stroke="rgba(199,155,216,.08)" stroke-width="1" stroke-dasharray="'+(g===0||g===4?'0':'2 3')+'"/>');
    grid.push('<text x="'+(padL-6)+'" y="'+(y+3)+'" font-size="9" fill="#7d7388" text-anchor="end" font-family="inherit">'+_fmtNum(val, val<10?2:0)+'</text>');
  }
  const candles=vs.map((c,i)=>{
    const cx=padL+i*colW+colW/2;
    const up=c.c>=c.o;
    const color=up?'#8ecf95':'#e5928f';
    const yo=yFor(c.o), yc=yFor(c.c), yh=yFor(c.h), yl=yFor(c.l);
    const top=Math.min(yo,yc), bot=Math.max(yo,yc);
    return '<line x1="'+cx+'" y1="'+yh+'" x2="'+cx+'" y2="'+yl+'" stroke="'+color+'" stroke-width="1"/>'+
      '<rect x="'+(cx-bodyW/2)+'" y="'+top+'" width="'+bodyW+'" height="'+Math.max(1,bot-top)+'" fill="'+color+'"/>';
  }).join('');
  return '<svg viewBox="0 0 '+W+' '+H+'" class="st-chart-svg" preserveAspectRatio="none">'+
    grid.join('')+candles+
  '</svg>';
}

function _hourlyTempSVG(hourly, opts){
  // hourly: [{h: '1p'|'13', t: 72, icon?: 'sun'}]. Renders a smooth 24h temp
  // curve with hour labels and a current-hour highlight band.
  const hs=(hourly||[]).map(h=>({h:String(h.h||''), t:Number(h.t), icon:h.icon||''})).filter(h=>isFinite(h.t));
  if(hs.length<2) return '';
  const o=opts||{};
  const W=o.w||480, H=o.h||92;
  const padL=o.padL||10, padR=o.padR||10, padT=o.padT||16, padB=o.padB||22;
  const ts=hs.map(h=>h.t);
  const tmin=Math.min(...ts), tmax=Math.max(...ts);
  const span=Math.max(tmax-tmin, 1);
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const xFor=i=>padL+(i/(hs.length-1))*plotW;
  const yFor=v=>padT+plotH-(v-tmin)/span*plotH;
  // smooth path via Catmull-Rom → cubic Bezier
  const pts=hs.map((h,i)=>[xFor(i),yFor(h.t)]);
  let path='';
  if(pts.length>=2){
    path='M '+pts[0][0]+' '+pts[0][1];
    for(let i=0; i<pts.length-1; i++){
      const p0=pts[i-1]||pts[i], p1=pts[i], p2=pts[i+1], p3=pts[i+2]||p2;
      const t=0.18;
      const c1x=p1[0]+(p2[0]-p0[0])*t, c1y=p1[1]+(p2[1]-p0[1])*t;
      const c2x=p2[0]-(p3[0]-p1[0])*t, c2y=p2[1]-(p3[1]-p1[1])*t;
      path+=' C '+c1x+' '+c1y+', '+c2x+' '+c2y+', '+p2[0]+' '+p2[1];
    }
  }
  const area=path+' L '+pts[pts.length-1][0]+' '+(padT+plotH)+' L '+pts[0][0]+' '+(padT+plotH)+' Z';
  const gid=_uid('hr');
  // current-hour band (first item)
  const currentIdx=0;
  const bx0=xFor(currentIdx)-plotW/(hs.length-1)/2, bx1=bx0+plotW/(hs.length-1);
  // hour labels: show every 6th
  const labels=hs.map((h,i)=>{
    if(i!==0 && i!==hs.length-1 && i%6!==0) return '';
    return '<text x="'+xFor(i)+'" y="'+(H-6)+'" font-size="9" fill="#b0a6ba" text-anchor="middle" font-family="inherit">'+esc(h.h)+'</text>';
  }).join('');
  // spot dots every 6
  const dots=hs.map((h,i)=> i%6===0 || i===hs.length-1
    ? '<circle cx="'+xFor(i)+'" cy="'+yFor(h.t)+'" r="2.4" fill="#16111c" stroke="#c79bd8" stroke-width="1.4"/>'
    : '').join('');
  return '<svg viewBox="0 0 '+W+' '+H+'" class="ww-hourly-svg" preserveAspectRatio="none">'+
    '<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="0" y2="1">'+
    '<stop offset="0%" stop-color="#c79bd8" stop-opacity=".35"/>'+
    '<stop offset="100%" stop-color="#c79bd8" stop-opacity="0"/></linearGradient></defs>'+
    '<rect x="'+bx0+'" y="'+padT+'" width="'+(bx1-bx0)+'" height="'+plotH+'" fill="rgba(199,155,216,.10)"/>'+
    '<path d="'+area+'" fill="url(#'+gid+')"/>'+
    '<path d="'+path+'" fill="none" stroke="#c79bd8" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'+
    dots+labels+
  '</svg>';
}

function _sunArcSVG(sunrise, sunset, now){
  // sunrise/sunset: "HH:MM" strings. now: current "HH:MM" or null.
  if(!sunrise||!sunset) return '';
  const toMin=s=>{const p=s.split(':').map(Number); return p[0]*60+(p[1]||0);};
  const sr=toMin(sunrise), ss=toMin(sunset);
  const mid=(sr+ss)/2;
  const total=ss-sr;
  const W=240, H=58, padL=18, padR=18, baseline=H-10;
  const ax0=padL, ax1=W-padR, ay=baseline;
  const xFor=m=>ax0+((m-sr)/total)*(ax1-ax0);
  const yFor=m=>{ const t=(m-sr)/total; return ay - Math.sin(t*Math.PI)*40; };
  // arc + ground line
  const sx0=xFor(sr), sy0=yFor(sr);
  const sx1=xFor(ss), sy1=yFor(ss);
  const mx=xFor(mid), my=yFor(mid);
  const arc='M '+sx0+' '+sy0+' Q '+mx+' '+(my-12)+', '+sx1+' '+sy1;
  const gid=_uid('sn');
  // sun position from `now` (defaults to actual current time of day)
  const nowMin = now ? toMin(now) : (()=>{const d=new Date(); return d.getHours()*60+d.getMinutes();})();
  const sunFrac = Math.max(0, Math.min(1, (nowMin-sr)/total));
  const sunX = ax0 + sunFrac*(ax1-ax0);
  const sunY = ay - Math.sin(sunFrac*Math.PI)*40;
  const sunOnArc = nowMin>=sr && nowMin<=ss;
  const sun = sunOnArc
    ? '<circle cx="'+sunX+'" cy="'+sunY+'" r="4.2" fill="#c79bd8" stroke="#16111c" stroke-width="1.4"/>'
    : '<circle cx="'+sunOnArc?sunX:(nowMin<sr?sx0:sx1)+'" cy="'+(sunOnArc?sunY:ay-3)+'" r="3" fill="#5a4e69"/>';
  return '<svg viewBox="0 0 '+W+' '+H+'" class="ww-sun-svg" preserveAspectRatio="xMidYMid meet">'+
    '<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="1" y2="0">'+
    '<stop offset="0%" stop-color="#e5928f" stop-opacity=".55"/>'+
    '<stop offset="50%" stop-color="#c79bd8" stop-opacity=".85"/>'+
    '<stop offset="100%" stop-color="#e6c073" stop-opacity=".55"/></linearGradient></defs>'+
    '<line x1="'+ax0+'" y1="'+ay+'" x2="'+ax1+'" y2="'+ay+'" stroke="rgba(199,155,216,.18)" stroke-dasharray="2 3"/>'+
    '<path d="'+arc+'" fill="none" stroke="url(#'+gid+')" stroke-width="1.6" stroke-linecap="round"/>'+
    sun+
    '<text x="'+sx0+'" y="'+(H-1)+'" font-size="9" fill="#b0a6ba" text-anchor="middle" font-family="inherit">'+esc(sunrise)+'</text>'+
    '<text x="'+sx1+'" y="'+(H-1)+'" font-size="9" fill="#b0a6ba" text-anchor="middle" font-family="inherit">'+esc(sunset)+'</text>'+
  '</svg>';
}

// ============================================================================
// STOCKS / CRYPTO
// ============================================================================
//
// A Robinhood × Bloomberg-terminal look. The card has:
//   1. Hero row:  market status pill · exchange chip · symbol · name ·
//                 price · colored change row
//   2. Timeframe tab strip:  1D | 1W | 1M | 3M | 1Y | All   (CSS-only)
//   3. Main chart:           area+line with gridlines + current-price marker,
//                             or candlesticks if `ohlc` is provided
//   4. Stats grid:           Open, High, Low, Volume, Mkt Cap, 52W Range
//   5. Watchlist:            symbol · mini-spark · price · change%
//   6. News strip (optional)

function _stTimeframeTabs(active){
  const tabs=['1D','1W','1M','3M','1Y','All'];
  return '<div class="st-tabs">'+
    tabs.map(t=>'<div class="st-tab'+(t===active?' st-tab-active':'')+'">'+t+'</div>').join('')+
  '</div>';
}

function _stStatsGrid(p, isCrypto){
  // Open/High/Low/Volume + Mkt Cap + 52W Range with mini position bar
  const cells=[];
  if(p.open!==undefined)  cells.push(['Open',  _fmtNum(p.open,2)]);
  if(p.high!==undefined)  cells.push(['High',  _fmtNum(p.high,2)]);
  if(p.low!==undefined)   cells.push(['Low',   _fmtNum(p.low,2)]);
  if(p.volume!==undefined)cells.push([isCrypto?'24h Vol':'Volume', _fmtVol(p.volume)]);
  if(p.market_cap!==undefined) cells.push(['Mkt Cap', _fmtBig(p.market_cap)]);
  if(isCrypto && p.dominance!==undefined) cells.push(['Dominance', _fmtPct(p.dominance,1)]);
  if(!isCrypto && p.pe!==undefined) cells.push(['P/E', _fmtNum(p.pe,2)]);
  // 52W Range with mini bar
  if(p.low_52w!==undefined && p.high_52w!==undefined){
    const lo=Number(p.low_52w), hi=Number(p.high_52w), cur=Number(p.price);
    const span=Math.max(hi-lo, 1e-9);
    const pct=isFinite(cur)?Math.max(0,Math.min(100, ((cur-lo)/span)*100)):0;
    const rangeHtml='<div class="st-stat"><div class="st-stat-l">52W Range</div>'+
      '<div class="st-stat-r"><div class="st-range">'+
      '<div class="st-range-track"></div>'+
      '<div class="st-range-fill" style="width:'+pct.toFixed(1)+'%"></div>'+
      '<div class="st-range-tick" style="left:'+pct.toFixed(1)+'%"></div>'+
      '</div>'+
      '<div class="st-range-vals"><span>'+_fmtNum(lo,2)+'</span><span>'+_fmtNum(hi,2)+'</span></div></div></div>';
    cells.push(['__raw__', rangeHtml]);
  }
  const grid=cells.map(c=>{
    if(c[0]==='__raw__') return c[1];
    return '<div class="st-stat"><div class="st-stat-l">'+esc(c[0])+'</div><div class="st-stat-v">'+esc(c[1])+'</div></div>';
  }).join('');
  return '<div class="st-stats">'+grid+'</div>';
}

function _stWatchlist(items){
  if(!items||!items.length) return '';
  return '<div class="st-watch">'+
    items.map(it=>{
      const u=Number(it.change)>=0;
      const spk=(it.chart&&it.chart.values)?_sparkSVG(it.chart.values, u?'#8ecf95':'#e5928f', 56, 18):'';
      return '<div class="st-watch-row">'+
        '<div class="st-watch-sym">'+esc(it.symbol||'')+'</div>'+
        '<div class="st-watch-spk">'+spk+'</div>'+
        '<div class="st-watch-name">'+esc(it.name||'')+'</div>'+
        '<div class="st-watch-price">'+_fmtNum(it.price,2)+'</div>'+
        '<div class="st-watch-chg '+(u?'ok':'hot')+'">'+(u?'+':'')+_fmtNum(it.change_pct,2)+'%</div>'+
      '</div>';
    }).join('')+
  '</div>';
}

function _stNews(news){
  if(!news||!news.length) return '';
  return '<div class="st-news">'+
    news.map(n=>'<div class="st-news-row">'+
      '<div class="st-news-dot"></div>'+
      '<div class="st-news-body">'+
        '<div class="st-news-title">'+esc(n.title||'')+'</div>'+
        '<div class="st-news-meta"><span>'+esc(n.source||'')+'</span>'+(n.time?'<span class="st-news-sep">·</span><span>'+esc(n.time)+'</span>':'')+'</div>'+
      '</div>'+
    '</div>').join('')+
  '</div>';
}

function buildStocksCard(p){
  // p: {title, symbol, name, price, change, change_pct, exchange, market_state,
  //     open, high, low, volume, market_cap, pe, low_52w, high_52w,
  //     chart: {labels, values, timeframe}, ohlc: [...], items: [...], news: [...]}
  const items=(p.items||[]);
  const hasTopLevel=(p.price!==undefined||p.change!==undefined||p.change_pct!==undefined||p.symbol);
  if(!items.length && !p.chart && !p.ohlc && !hasTopLevel) return null;

  // hero
  const sym=(p.symbol||(items[0]&&items[0].symbol)||'').toUpperCase();
  const name=p.name||(items[0]&&items[0].name)||'';
  const price=(p.price!==undefined)?p.price:(items[0]&&items[0].price);
  const chg=(p.change!==undefined)?p.change:(items[0]&&items[0].change);
  const chgPct=(p.change_pct!==undefined)?p.change_pct:(items[0]&&items[0].change_pct);
  const up=Number(chg)>=0;
  const arrow=up?'\u25B2':'\u25BC';
  const chgColorClass = up?'ok':(Number(chg)<0?'hot':'flat');

  const ms = p.market_state || _marketState();
  const msClass = ms.open ? 'st-ms-open' : 'st-ms-closed';
  const ex = p.exchange || (sym.endsWith('-USD')?'CRYPTO':(sym.length<=4?'NYSE':'NASDAQ'));

  // chart
  const tf = (p.chart&&p.chart.timeframe) || '1D';
  const chart = p.ohlc
    ? _candleChartSVG(p.ohlc, {w:480,h:160,padL:36,padR:12})
    : (p.chart&&p.chart.values ? _areaChartSVG(p.chart.values, {w:480,h:160,color:'#c79bd8',padL:36,padR:52}) : '');

  // watchlist — strip the first item if it duplicates the hero
  const rest = items.length>1 ? items.slice(1) : [];

  return '<div class="sw-card sw-stocks">'+
    '<div class="st-head">'+
      '<div class="st-head-l">'+
        '<div class="st-sym">'+esc(sym)+'</div>'+
        '<div class="st-name">'+esc(name)+'</div>'+
        '<div class="st-chips">'+
          '<span class="st-chip st-ex">'+esc(ex)+'</span>'+
          '<span class="st-chip '+msClass+'">'+esc(ms.label)+'</span>'+
        '</div>'+
      '</div>'+
      '<div class="st-head-r">'+
        '<div class="st-price">'+_fmtNum(price,2)+'</div>'+
        '<div class="st-chg '+chgColorClass+'">'+arrow+' '+(up?'+':'')+_fmtNum(chg,2)+' <span class="st-chg-pct">'+(up?'+':'')+_fmtNum(chgPct,2)+'%</span></div>'+
      '</div>'+
    '</div>'+
    (chart ? ('<div class="st-chart">'+_stTimeframeTabs(tf)+chart+'</div>') : '')+
    _stStatsGrid(p, false)+
    _stWatchlist(rest)+
    _stNews(p.news)+
  '</div>';
}

function buildCryptoCard(p){
  // Mirror stocks with crypto labels. Reuses everything via the same code path
  // by flipping isCrypto=true for the stats grid and the exchange chip.
  const items=(p.items||[]);
  const hasTopLevel=(p.price!==undefined||p.change!==undefined||p.change_pct!==undefined||p.symbol);
  if(!items.length && !p.chart && !p.ohlc && !hasTopLevel) return null;

  const sym=(p.symbol||(items[0]&&items[0].symbol)||'').toUpperCase();
  const name=p.name||(items[0]&&items[0].name)||'';
  const price=(p.price!==undefined)?p.price:(items[0]&&items[0].price);
  const chg=(p.change!==undefined)?p.change:(items[0]&&items[0].change);
  const chgPct=(p.change_pct!==undefined)?p.change_pct:(items[0]&&items[0].change_pct);
  const up=Number(chg)>=0;
  const arrow=up?'\u25B2':'\u25BC';
  const chgColorClass = up?'ok':(Number(chg)<0?'hot':'flat');

  const tf = (p.chart&&p.chart.timeframe) || '24H';
  const chart = p.ohlc
    ? _candleChartSVG(p.ohlc, {w:480,h:160,padL:36,padR:12})
    : (p.chart&&p.chart.values ? _areaChartSVG(p.chart.values, {w:480,h:160,color:'#c79bd8',padL:36,padR:52}) : '');
  const rest = items.length>1 ? items.slice(1) : [];
  const ex = p.exchange || (sym.endsWith('-USD')?'GLOBAL':(sym.length<=4?'CEX':'DEX'));

  return '<div class="sw-card sw-stocks sw-crypto">'+
    '<div class="st-head">'+
      '<div class="st-head-l">'+
        '<div class="st-sym">'+esc(sym)+'</div>'+
        '<div class="st-name">'+esc(name)+'</div>'+
        '<div class="st-chips">'+
          '<span class="st-chip st-ex">'+esc(ex)+'</span>'+
          '<span class="st-chip st-ms-open">● 24H</span>'+
        '</div>'+
      '</div>'+
      '<div class="st-head-r">'+
        '<div class="st-price">'+_fmtNum(price,2)+'</div>'+
        '<div class="st-chg '+chgColorClass+'">'+arrow+' '+(up?'+':'')+_fmtNum(chg,2)+' <span class="st-chg-pct">'+(up?'+':'')+_fmtNum(chgPct,2)+'%</span></div>'+
      '</div>'+
    '</div>'+
    (chart ? ('<div class="st-chart">'+_stTimeframeTabs(tf)+chart+'</div>') : '')+
    _stStatsGrid(p, true)+
    _stWatchlist(rest)+
  '</div>';
}

// ============================================================================
// WEATHER
// ============================================================================
//
// Apple-Weather feel: condition-tinted header gradient, big temp, 24h hourly
// curve, 5-day forecast with hi/lo bars + precip, sunrise/sunset arc, 4-up
// wind/humidity/UV/pressure grid.

function _wxIcon(s){
  const m={
    'sunny':'\u2600','clear':'\u2600',
    'partly':'\u26C5','partly cloudy':'\u26C5','cloudy':'\u2601','overcast':'\u2601',
    'rain':'\u2602','rainy':'\u2602','showers':'\u2602','drizzle':'\u2602',
    'thunder':'\u26C8','thunderstorm':'\u26C8',
    'snow':'\u2744','snowy':'\u2744','sleet':'\u2745',
    'fog':'\u2601','mist':'\u2601','haze':'\u2601',
    'wind':'\u2638','windy':'\u2638',
    'night':'\u263E','clear night':'\u263E',
    'hot':'\u2600','cold':'\u2744'
  };
  return m[(s||'').toLowerCase()]||'\u2601';
}
function _wxToneClass(s){
  const k=(s||'').toLowerCase();
  if(k.includes('thunder')||k.includes('rain')||k.includes('shower')||k.includes('drizzle')) return 'ww-tone-rain';
  if(k.includes('snow')||k.includes('sleet')) return 'ww-tone-snow';
  if(k.includes('fog')||k.includes('mist')||k.includes('haze')||k.includes('overcast')) return 'ww-tone-fog';
  if(k.includes('night')||k.includes('clear')&&k.includes('night')) return 'ww-tone-night';
  if(k.includes('cloud')||k.includes('partly')) return 'ww-tone-cloud';
  return 'ww-tone-day';
}

function buildWeatherCard(p){
  // p: {title, location, updated, current:{temp, condition, icon, feels, humidity, wind, uv, pressure, visibility},
  //     hourly: [{h, t, icon}], forecast: [{day, high, low, icon, condition, precip}],
  //     sunrise, sunset}
  const cur=p.current||{};
  const fc=p.forecast||[];
  const hourly=p.hourly||[];
  const tone=_wxToneClass(cur.icon||cur.condition||'sunny');
  const tempStr = cur.temp!==undefined ? Math.round(Number(cur.temp)) : '—';
  const iconStr = _wxIcon(cur.icon||cur.condition||'sunny');
  const hl=fc.length?{hi:Math.max(...fc.map(d=>Number(d.high||d.hi||0))), lo:Math.min(...fc.map(d=>Number(d.low||d.lo||0)))}:{hi:0,lo:0};
  // meta grid (humidity, wind, uv, pressure, visibility, feels)
  const meta=[];
  if(cur.humidity!==undefined)    meta.push(['Humidity',   cur.humidity+'%']);
  if(cur.wind!==undefined)        meta.push(['Wind',       typeof cur.wind==='number'?cur.wind+' mph':String(cur.wind)]);
  if(cur.feels!==undefined)       meta.push(['Feels Like', Math.round(Number(cur.feels))+'°']);
  if(cur.uv!==undefined)          meta.push(['UV Index',   String(cur.uv)]);
  if(cur.pressure!==undefined)    meta.push(['Pressure',   String(cur.pressure)]);
  if(cur.visibility!==undefined)  meta.push(['Visibility', typeof cur.visibility==='number'?cur.visibility+' mi':String(cur.visibility)]);
  // forecast with hi/lo bars
  const fcHtml = fc.length ? (
    '<div class="ww-fc">'+
      fc.map(d=>{
        const hi=Number(d.high??d.hi), lo=Number(d.low??d.lo);
        // bar position within the day's overall hi/lo range
        const dayMin=Math.min(lo, hl.lo);
        const dayMax=Math.max(hi, hl.hi);
        const span=Math.max(dayMax-dayMin, 1e-9);
        const a=((lo-dayMin)/span)*100, b=((hi-dayMin)/span)*100;
        return '<div class="ww-fcday">'+
          '<div class="ww-fcd">'+esc((d.day||'').slice(0,3))+'</div>'+
          '<div class="ww-fci">'+_wxIcon(d.icon||d.condition)+'</div>'+
          '<div class="ww-fch">'+Math.round(hi)+'°</div>'+
          '<div class="ww-fcbar"><div class="ww-fcbar-fill" style="left:'+a.toFixed(1)+'%;width:'+(b-a).toFixed(1)+'%"></div></div>'+
          '<div class="ww-fcl">'+Math.round(lo)+'°</div>'+
          (d.precip!==undefined?'<div class="ww-fcp">'+Math.round(Number(d.precip))+'%</div>':'')+
        '</div>';
      }).join('')+
    '</div>'
  ) : '';
  const hourlySvg = hourly.length ? _hourlyTempSVG(hourly) : '';
  const sunSvg = _sunArcSVG(p.sunrise, p.sunset, p.now);

  return '<div class="sw-card sw-weather '+tone+'">'+
    '<div class="ww-hero">'+
      '<div class="ww-hero-l">'+
        '<div class="ww-loc">'+esc(p.location||p.title||'Current Location')+'</div>'+
        (p.updated?'<div class="ww-upd">Updated '+esc(p.updated)+'</div>':'')+
        '<div class="ww-temp">'+tempStr+'<span class="ww-temp-u">°</span></div>'+
        '<div class="ww-cond">'+esc(cur.condition||'')+'</div>'+
        (fc.length?'<div class="ww-hl"><span>H '+Math.round(hl.hi)+'°</span><span class="ww-hl-sep">·</span><span>L '+Math.round(hl.lo)+'°</span></div>':'')+
      '</div>'+
      '<div class="ww-ic">'+iconStr+'</div>'+
    '</div>'+
    (hourlySvg?'<div class="ww-hourly">'+hourlySvg+'</div>':'')+
    (meta.length?'<div class="ww-meta">'+meta.map(([l,v])=>
      '<div class="ww-metacell"><div class="ww-metal">'+esc(l)+'</div><div class="ww-metav">'+esc(String(v))+'</div></div>'
    ).join('')+'</div>':'')+
    fcHtml+
    (sunSvg?'<div class="ww-sun">'+sunSvg+'</div>':'')+
  '</div>';
}

// ============================================================================
// SPORTS
// ============================================================================
//
// ESPN scoreboard: league chip, status pill with pulsing dot, two team rows
// with logo-block + record + big score, winner accent-tinted.

function _sxInitials(name){
  return (name||'').split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0].toUpperCase()).join('')||'?';
}
function _sxTint(name){
  // Deterministic hue from team name, 0-360
  let h=0; for(let i=0; i<name.length; i++) h=(h*31+name.charCodeAt(i))%360;
  return 'hsl('+h+',55%,52%)';
}

function buildSportsCard(p){
  // p: {title, league, games: [{home, away, home_score, away_score, status, time, note, home_record, away_record, home_color, away_color}]}
  const games=p.games||p.items||[];
  if(!games.length) return null;
  const league=p.league||'';
  return '<div class="sw-card sw-sports">'+
    (league?'<div class="sx-league">'+esc(league)+'</div>':'')+
    games.map(g=>{
      const hs=Number(g.home_score), as=Number(g.away_score);
      const hasScore=isFinite(hs)&&isFinite(as);
      const homeWin=hasScore&&hs>as, awayWin=hasScore&&as>hs;
      const status=(g.status||'').toLowerCase();
      const live=status==='live'||status==='in progress'||status==='in_progress';
      const final=status==='final'||status==='finished';
      const scheduled=status==='scheduled'||status==='pre'||(!hasScore&&!live&&!final);
      const homeCol=g.home_color||_sxTint(g.home||'H');
      const awayCol=g.away_color||_sxTint(g.away||'A');
      return '<div class="sx-row">'+
        '<div class="sx-status-row">'+
          (live?'<span class="sx-dot"></span><span class="sx-status-lbl sx-live">LIVE</span>':'')+
          (final?'<span class="sx-status-lbl sx-final">FINAL</span>':'')+
          (scheduled?'<span class="sx-status-lbl sx-sched">'+esc(g.status||g.time||'SCHEDULED')+'</span>':'')+
          (g.time&&hasScore?'<span class="sx-time">'+esc(g.time)+'</span>':'')+
        '</div>'+
        '<div class="sx-game">'+
          '<div class="sx-team '+(homeWin?' sx-win':'')+'">'+
            '<div class="sx-logo" style="background:'+esc(homeCol)+'22;border-color:'+esc(homeCol)+'">'+esc(_sxInitials(g.home))+'</div>'+
            '<div class="sx-team-info">'+
              '<div class="sx-name">'+esc(g.home||'')+'</div>'+
              (g.home_record?'<div class="sx-rec">'+esc(g.home_record)+'</div>':'')+
            '</div>'+
            (hasScore?'<div class="sx-score '+(homeWin?' sx-score-win':'')+'">'+hs+'</div>':'')+
          '</div>'+
          '<div class="sx-team '+(awayWin?' sx-win':'')+'">'+
            '<div class="sx-logo" style="background:'+esc(awayCol)+'22;border-color:'+esc(awayCol)+'">'+esc(_sxInitials(g.away))+'</div>'+
            '<div class="sx-team-info">'+
              '<div class="sx-name">'+esc(g.away||'')+'</div>'+
              (g.away_record?'<div class="sx-rec">'+esc(g.away_record)+'</div>':'')+
            '</div>'+
            (hasScore?'<div class="sx-score '+(awayWin?' sx-score-win':'')+'">'+as+'</div>':'')+
          '</div>'+
        '</div>'+
        (g.note?'<div class="sx-note">'+esc(g.note)+'</div>':'')+
      '</div>';
    }).join('')+
  '</div>';
}

// ============================================================================
// CALENDAR
// ============================================================================
//
// Google-Calendar × Fantastical timeline: date header, all-day events as
// pill rows, hourly gutter on the left, color-coded events, current-time
// red horizontal line.

function _cxToMin(s){
  if(!s) return null;
  const m=String(s).match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/i);
  if(!m) return null;
  let h=parseInt(m[1],10), mm=parseInt(m[2]||'0',10);
  const ap=(m[3]||'').toLowerCase();
  if(ap==='pm'&&h<12) h+=12;
  if(ap==='am'&&h===12) h=0;
  return h*60+mm;
}
function _cxFormatHour(h){
  const ap=h<12?'a':'p';
  const v=h%12===0?12:h%12;
  return v+ap;
}

function buildCalendarCard(p){
  // p: {title, date, day_name, now: 'HH:MM', events: [{start:'9:00', end:'10:30', title, location, note, color, all_day}]}
  const events=p.events||p.items||[];
  if(!events.length) return null;
  const allDay = events.filter(e=>e.all_day);
  const timed  = events.filter(e=>!e.all_day);
  // collect hour gutter bounds
  const startMins = timed.map(e=>_cxToMin(e.start)).filter(v=>v!==null);
  const endMins   = timed.map(e=>_cxToMin(e.end||e.start)).filter(v=>v!==null);
  let hourStart = startMins.length ? Math.floor(Math.min(...startMins)/60) : 8;
  let hourEnd   = endMins.length   ? Math.ceil(Math.max(...endMins)/60)   : 18;
  hourStart = Math.max(0, Math.min(23, hourStart-1));
  hourEnd   = Math.max(hourStart+4, Math.min(24, hourEnd+1));
  const spanMins = (hourEnd-hourStart)*60;
  const nowMin = _cxToMin(p.now);
  const nowPct = (nowMin!==null) ? ((nowMin-hourStart*60)/spanMins)*100 : null;

  // all-day pills
  const allDayHtml = allDay.length
    ? '<div class="cx-allday">'+allDay.map(e=>{
        const color=e.color||'#c79bd8';
        return '<div class="cx-allday-pill" style="--cx-color:'+esc(color)+'">'+esc(e.title||'')+'</div>';
      }).join('')+'</div>'
    : '';
  // timeline
  const hourLabels=[];
  for(let h=hourStart; h<=hourEnd; h++){
    hourLabels.push('<div class="cx-hour"><div class="cx-hour-lbl">'+_cxFormatHour(h)+'</div><div class="cx-hour-line"></div></div>');
  }
  const eventsHtml = timed.map(e=>{
    const s=_cxToMin(e.start);
    let en=_cxToMin(e.end||e.start);
    if(en===null||en<=s) en=s+30;
    const top=((s-hourStart*60)/spanMins)*100;
    const height=Math.max(4, ((en-s)/spanMins)*100);
    const color=e.color||'#c79bd8';
    return '<div class="cx-ev" style="top:'+top.toFixed(2)+'%;height:'+height.toFixed(2)+'%;--cx-color:'+esc(color)+'">'+
      '<div class="cx-ev-bar"></div>'+
      '<div class="cx-ev-body">'+
        '<div class="cx-ev-time">'+esc((e.start||'')+(e.end?' – '+e.end:''))+'</div>'+
        '<div class="cx-ev-title">'+esc(e.title||'')+'</div>'+
        (e.location?'<div class="cx-ev-loc">'+esc(e.location)+'</div>':'')+
      '</div>'+
    '</div>';
  }).join('');

  return '<div class="sw-card sw-cal">'+
    (p.date||p.day_name?'<div class="cx-date">'+esc(p.day_name||'')+(p.date?'<span class="cx-date-num">'+esc(p.date)+'</span>':'')+'</div>':'')+
    allDayHtml+
    '<div class="cx-timeline">'+
      '<div class="cx-hours">'+hourLabels.join('')+'</div>'+
      '<div class="cx-track">'+eventsHtml+(nowPct!==null?'<div class="cx-now" style="top:'+nowPct.toFixed(2)+'%"><div class="cx-now-dot"></div><div class="cx-now-line"></div></div>':'')+'</div>'+
    '</div>'+
  '</div>';
}

// Orchestrator: turn a `show_widget` SSE event into a draggable HUD window.
function renderWidget(d){
  const type=(d.type||'').toLowerCase();
  const title=d.title||((type||'').charAt(0).toUpperCase()+(type||'').slice(1));
  const data=d.data||{};
  // Build a synthetic panel payload so buildPanelInner does the work.
  const p={panel:type, title:title, ...data};
  const inner=buildPanelInner(p);
  const layer=$('#windowLayer');
  if(!inner){ return; }
  const idx=_winCascade;
  const pos=_nextWinPos();
  const win=document.createElement('div'); win.className='hud-window sw-window sw-'+type;
  win.style.cssText='left:'+pos.x+'px;top:'+pos.y+'px;--i:'+idx;
  win.innerHTML='<div class="hud-win-head"><span class="hud-win-title">'+esc(title)+'</span>'+
    '<button class="hud-win-close" title="Close">&times;</button></div>'+
    '<div class="hud-win-body sw-body">'+inner+'</div>'+
    '<div class="hud-win-resize"></div>';
  win.querySelector('.hud-win-close').addEventListener('pointerdown',e=>{e.stopPropagation();_closeWindow(win);});
  layer.appendChild(win);
  _initWindow(win);
}

// ---- FLOATING HUD WINDOWS ----------------------------------------------------
let _winCascade = 0;
function _nextWinPos(){
  const layer=$('#windowLayer');
  const lw=layer.clientWidth, lh=layer.clientHeight;
  const cols=Math.max(1, Math.floor((lw-40)/280));
  const rows=Math.max(1, Math.floor((lh-40)/240));
  const col=_winCascade%cols, row=Math.floor(_winCascade/cols)%rows;
  const cellW=(lw-40)/cols, cellH=(lh-40)/rows;
  const x=20+col*cellW+20, y=20+row*cellH+20;
  _winCascade++;
  return {x: Math.min(x, lw-260), y: Math.min(y, lh-200)};
}
// One shared pointer manager drives every window's drag + resize, so windows
// don't each leak a set of document-level listeners. Pointer events unify
// mouse and touch in a single path.
let _drag=null; // {win, mode:'move'|'resize', sx, sy, ox, oy, ow, oh}
function _initWindow(win){
  const head=win.querySelector('.hud-win-head');
  if(head) head.addEventListener('pointerdown',e=>{
    if(e.target.classList.contains('hud-win-close')) return;
    _drag={win, mode:'move', sx:e.clientX, sy:e.clientY,
      ox:parseInt(win.style.left)||0, oy:parseInt(win.style.top)||0};
    win.classList.add('dragging'); e.preventDefault();
  });
  const handle=win.querySelector('.hud-win-resize');
  if(handle) handle.addEventListener('pointerdown',e=>{
    _drag={win, mode:'resize', sx:e.clientX, sy:e.clientY,
      ow:win.offsetWidth, oh:win.offsetHeight};
    win.classList.add('resizing'); e.preventDefault(); e.stopPropagation();
  });
}
document.addEventListener('pointermove',e=>{
  if(!_drag) return;
  const dx=e.clientX-_drag.sx, dy=e.clientY-_drag.sy;
  if(_drag.mode==='move'){
    _drag.win.style.left=(_drag.ox+dx)+'px';
    _drag.win.style.top=(_drag.oy+dy)+'px';
  } else {
    _drag.win.style.width=Math.max(220,_drag.ow+dx)+'px';
    _drag.win.style.height=Math.max(100,_drag.oh+dy)+'px';
  }
});
document.addEventListener('pointerup',()=>{
  if(!_drag) return;
  _drag.win.classList.remove('dragging','resizing');
  _drag=null;
});
function _closeWindow(win){
  win.style.display='none';
  state.closedWindows.push(win);
  _updateRestorePill();
}
function _restoreWindow(win){
  win.style.display='';
  state.closedWindows=state.closedWindows.filter(w=>w!==win);
  // Re-trigger entrance animation
  win.style.animation='none'; win.offsetHeight; win.style.animation='';
  _updateRestorePill();
}
function _updateRestorePill(){
  const pill=$('#restorePill');
  const dd=$('#restoreDropdown');
  if(!pill) return;
  if(state.closedWindows.length===0){
    pill.style.display='none';
    pill.classList.remove('open');
    return;
  }
  pill.style.display='';
  pill.querySelector('.restore-pill-count').textContent=state.closedWindows.length;
  dd.innerHTML=state.closedWindows.map((w,i)=>{
    const t=w.querySelector('.hud-win-title');
    const label=t?t.textContent:('Panel '+(i+1));
    return '<div class="restore-item" data-ri="'+i+'">↩ '+esc(label)+'</div>';
  }).join('');
  dd.querySelectorAll('.restore-item').forEach(el=>{
    el.onclick=()=>{ const idx=+el.dataset.ri; _restoreWindow(state.closedWindows[idx]); };
  });
}
$('#restorePill').addEventListener('click',e=>{
  if(e.target.closest('.restore-item')) return;
  e.currentTarget.classList.toggle('open');
});
// Close dropdown when clicking outside
document.addEventListener('pointerdown',e=>{
  const pill=$('#restorePill');
  if(pill && !pill.contains(e.target)) pill.classList.remove('open');
});
function clearViewport(){
  state.renderedPanels.clear();
  state.closedWindows=[];
  _winCascade=0;
  const layer=$('#windowLayer');
  if(layer) layer.innerHTML='';
  _updateRestorePill();
}

// bring window to front on interaction
$('#windowLayer').addEventListener('pointerdown',e=>{
  const win=e.target.closest('.hud-window');
  if(win && !e.target.classList.contains('hud-win-close')){
    // move to end of DOM = top of stack
    e.currentTarget.appendChild(win);
  }
});

// ---- EMPTY STATE ------------------------------------------------------------
const QUICK = [
  {icon:'🔍', title:'Search the web',  sub:'Find and summarise anything online', prompt:'Search the web for '},
  {icon:'🖥️', title:'Read my screen',  sub:'Summarise what\'s in my browser tab', prompt:'Read my screen and summarise what you see'},
  {icon:'📊', title:'Show me stats',   sub:'Render live data as floating panels',  prompt:'Show me a status panel of my system'},
  {icon:'📈', title:'Draw a chart',    sub:'Bar, line, or pie - visualise data',   prompt:'Show me a bar chart comparing '},
  {icon:'📝', title:'Take a note',     sub:'Remember something for later',        prompt:'Take a note: '},
  {icon:'⏰', title:'Set a reminder',  sub:'Add something to my reminder list',   prompt:'Add a reminder: '},
  {icon:'📂', title:'Browse files',    sub:'List or read files on your machine',  prompt:'List files in my current directory'},
  {icon:'🎛️', title:'Interactive panel', sub:'Buttons & forms I can click',       prompt:'Show me an interactive panel with a few action buttons I can click'},
];
function showEmpty() {
  clearLog();
  const wrap=document.createElement('div'); wrap.className='j-empty';
   wrap.innerHTML='<div class="j-empty-title">Select a chat or type a message</div><div class="quick-cards">'+
    QUICK.map((q,i)=>`<div class="qcard" style="--i:${i}" data-prompt="${esc(q.prompt)}"><span class="qcard-icon">${q.icon}</span>`+
      `<span class="qcard-title">${esc(q.title)}</span><span class="qcard-sub">${esc(q.sub)}</span></div>`).join('')+'</div>';
  log.appendChild(wrap);
  wrap.querySelectorAll('.qcard').forEach(c=>{ c.onclick=()=>{ input.value=c.dataset.prompt; autoGrow(); input.focus(); }; });
}

// ---- RENDERING --------------------------------------------------------------
let _userMsgIdx=0;
const MSG_ACTIONS_HTML='<button class="msg-act-btn" data-act="resend" title="Resend">&#8635; resend</button><button class="msg-act-btn" data-act="edit" title="Edit">&#9998; edit</button><button class="msg-act-btn del-btn" data-act="delete" title="Delete">&#10005; delete</button>';
function wireMsgActions(row, idx, text){
  row.querySelector('[data-act="resend"]').onclick=()=>resendMsg(idx,row);
  row.querySelector('[data-act="edit"]').onclick=()=>editMsg(idx,row,text);
  row.querySelector('[data-act="delete"]').onclick=()=>deleteMsg(idx,row);
}
function addUser(text){
    const idx=_userMsgIdx++;
    const r=document.createElement('div'); r.className='msg-row user'; r.dataset.idx=idx;
    r.innerHTML='<div class="bubble">'+esc(text)+'</div><div class="msg-actions">'+MSG_ACTIONS_HTML+'</div>';
    wireMsgActions(r, idx, text);
    getThread().appendChild(r); scrollDown(); return r;
  }
// ---- DOM HELPERS (shared) ---------------------------------------------------
function truncateAfter(row, includeSelf){
  // Remove all DOM siblings after (and optionally including) the given row.
  const thread=getThread();
  const toRemove=[];
  let cutting=false;
  for(const ch of thread.children){
    if(ch===row){ cutting=true; if(includeSelf) toRemove.push(ch); continue; }
    if(cutting) toRemove.push(ch);
  }
  toRemove.forEach(ch=>ch.remove());
}

function resendMsg(idx,row){
  if(state.busy) return;
  const bubble=row.querySelector('.bubble');
  const text=bubble.textContent||'';
  truncateAfter(row, false);
  streamEdit(idx,text);
}
function streamEdit(idx,text){
  live={body:null,raw:'',toolRow:null,thinking:null,turnStart:null,tokensIn:0,tokensOut:0};
  showThinking(); setOrbLabel(_curVerb+'\u2026'); setBusy(true); compactOrb(true);
  fetch('/api/chat/edit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:idx,message:text})})
  .then(r=>{ if(!r.ok||!r.body) throw new Error(r.status); return readSSE(r,handle); })
  .then(()=>{clearThinking();if(state.busy)finishTurn();})
  .catch(()=>{clearThinking();addNote('CONNECTION FAILURE',true);finishTurn();});
}

function deleteMsg(idx,row){
  if(state.busy) return;
  truncateAfter(row, true);
  // If no messages left, show empty state
  if(!getThread().children.length) showEmpty();
  // Tell backend to truncate history
  fetch('/api/chat/delete-msg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:idx})})
  .then(r=>r.json()).then(d=>{
    if(d.messages) { /* reload chat state */ }
    refreshChats();
  }).catch(()=>{});
}

function editMsg(idx,row,origText){
  if(state.busy) return;
  row.classList.add('editing');
  const bubble=row.querySelector('.bubble');
  const actions=row.querySelector('.msg-actions');
  const ta=document.createElement('textarea'); ta.className='edit-area'; ta.value=origText;
  bubble.innerHTML=''; bubble.appendChild(ta);
  ta.style.height=Math.min(ta.scrollHeight,130)+'px';
  ta.focus();
  actions.innerHTML='<button class="msg-act-btn edit-save" title="Save &amp; send">&#10003; save</button><button class="msg-act-btn edit-cancel" title="Cancel">&#10005; cancel</button>';
  const save=()=>{
    const newText=ta.value.trim(); if(!newText){cancel();return;}
    row.classList.remove('editing');
    truncateAfter(row, false);
    // Update the bubble with new text
    bubble.innerHTML=esc(newText);
    streamEdit(idx,newText);
  };
  const cancel=()=>{
    row.classList.remove('editing');
    bubble.innerHTML=esc(origText);
    actions.innerHTML=MSG_ACTIONS_HTML;
    wireMsgActions(row, idx, origText);
  };
  actions.querySelector('.edit-save').onclick=save;
  actions.querySelector('.edit-cancel').onclick=cancel;
  ta.addEventListener('keydown',e=>{
    if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();save();}
    if(e.key==='Escape'){e.preventDefault();cancel();}
  });
}
function addAssistant(html, tools){
    const r=document.createElement('div'); r.className='msg-row assistant';
    r.innerHTML=avatarHTML()+'<div class="msg-body">'+(html||'')+'</div>';
    getThread().appendChild(r);
    (tools||[]).forEach(t=>addToolRow({name:t},true));
    scrollDown(); return r;
  }
function addToolRow(t, done){
  // Collapse same-name tool calls, skipping over non-tool-row elements
  // (info notes, assistant text, etc.) so that calls across iterations
  // of the tool loop still collapse into one row.
  const thread=getThread();
  let prev=null;
  for(let el=thread.lastElementChild; el; el=el.previousElementSibling){
    if(el.classList.contains('tool-row')){ prev=el; break; }
  }
  if(prev && prev.dataset.name===t.name){
    let cnt=prev.dataset.cnt ? parseInt(prev.dataset.cnt)+1 : 2;
    prev.dataset.cnt=cnt;
    const nameEl=prev.querySelector('.tname');
    if(nameEl) nameEl.textContent=t.name+' \u00d7'+cnt;
    if(t.summary){
      const sumEl=prev.querySelector('.tsum');
      if(sumEl) sumEl.textContent=t.summary;
    }
    if(!done){ prev.classList.remove('ok','bad'); prev.classList.add('pending'); }
    scrollDown(); return prev;
  }
  const row=document.createElement('div'); row.className='tool-row'+(done?'':' pending');
  row.dataset.name=t.name||'';
  const isCmd=(t.name||'').startsWith('run_')||(t.name||'').startsWith('bash');
  const icon=isCmd?'&#9654;':'&#9889;';
  const iconColor=isCmd?'var(--warn)':'var(--accent)';
  row.innerHTML='<span class="tool-icon" style="color:'+iconColor+'">'+icon+'</span>'+
    '<span class="tname">'+esc(t.name||'')+'</span>'+
    (t.summary?'<span class="tsum">'+esc(t.summary)+'</span>':'')+
    (done?'':'<span class="tres">RUNNING&#8230;</span>');
  getThread().appendChild(row); scrollDown(); return row;
}
function addNote(text, isErr){
  const n=document.createElement('div'); n.className='note-row'+(isErr?' err':'');
  n.textContent=text||''; getThread().appendChild(n); scrollDown();
}
function showPermission(d){
  const box=document.createElement('div'); box.className='perm-box';
  box.innerHTML='<div class="pq">AUTHORIZATION REQUIRED: <code>'+esc(d.tool)+'</code>'+(d.summary?' &mdash; '+esc(d.summary):'')+' </div>';
  const btns=document.createElement('div'); btns.className='perm-btns';
  const answer=(a,past)=>{
    box.innerHTML='<div class="pq"><code>'+esc(d.tool)+'</code></div><div class="perm-decided">&#8594; '+past.toUpperCase()+'</div>';
    fetch('/api/permission',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answer:a,id:d.id})});
  };
  [['yes','APPROVE','approved'],['always','ALWAYS ALLOW','always allowed'],['no','DENY','denied']].forEach(([a,l,p])=>{
    const b=document.createElement('button'); b.className=a; b.textContent=l; b.onclick=()=>answer(a,p); btns.appendChild(b);
  });
  box.appendChild(btns); getThread().appendChild(box); scrollDown();
}

// ---- SSE STREAM READER (shared) -------------------------------------------
async function readSSE(response, onEvent){
  if(!response||!response.body) return;
  const reader=response.body.getReader(), dec=new TextDecoder(); let buf='';
  while(true){
    let chunk; try{chunk=await reader.read();}catch(e){break;}
    if(chunk.done) break;
    buf+=dec.decode(chunk.value,{stream:true}); let i;
    while((i=buf.indexOf('\n\n'))>=0){ const line=buf.slice(0,i); buf=buf.slice(i+2);
      if(line.startsWith('data: ')){ try{onEvent(JSON.parse(line.slice(6)));}catch(e){} } }
  }
}

// ---- LIVE TURN --------------------------------------------------------------
let live={body:null,raw:'',toolRow:null,thinking:null,turnStart:null,tokensIn:0,tokensOut:0};
let _thinkTimer=null;
const _VERBS=['hatching','orbiting','pondering','brewing','simmering','marinating','percolating','crystallizing','weaving','conjuring','manifesting','distilling','synthesizing','calculating','reverberating','catalyzing','assembling','composting','fermenting','spinning','dreaming','musing','ruminating','cooking','germinating','blossoming','incubating','metabolizing','transmuting','alchemizing'];
let _curVerb='thinking';
function randomVerb(){ return _VERBS[Math.floor(Math.random()*_VERBS.length)]; }
function showThinking(){
  _curVerb=randomVerb();
  const t=document.createElement('div'); t.className='thinking-row';
  t.innerHTML=avatarHTML()+'<span>'+_curVerb+'\u2026</span><div class="thinking-dots"><span></span><span></span><span></span></div><span class="thinking-timer" id="thinkTimer"></span>';
  getThread().appendChild(t); scrollDown(); live.thinking=t;
  live.turnStart=Date.now(); live.tokensIn=0; live.tokensOut=0;
  const timerEl=t.querySelector('#thinkTimer');
  _thinkTimer=setInterval(()=>{ if(!live.turnStart){clearInterval(_thinkTimer);_thinkTimer=null;return;} const s=((Date.now()-live.turnStart)/1000).toFixed(1); let parts=[s+'s']; if(live.tokensIn) parts.push('\u2193'+live.tokensIn); if(live.tokensOut) parts.push('\u2191'+live.tokensOut); if(timerEl) timerEl.textContent=parts.join(' \u00b7 '); },100);
}
function clearThinking(){ if(live.thinking){live.thinking.remove();live.thinking=null;} if(_thinkTimer){clearInterval(_thinkTimer);_thinkTimer=null;} }
function handle(ev){
  const k=ev.kind, d=ev.data||{};
  if(k!=='user') clearThinking();
  if(k==='delta'){
    if(!live.body){const _r=addAssistant('');live.body=_r.querySelector('.msg-body');live.raw='';}
    live.raw+=d.text||'';
    live.tokensOut+=Math.round((d.text||'').length/4);
    live.body.innerHTML=md(stripHud(live.raw));
    live.body.classList.add('cursor'); scrollDown();
  } else if(k==='assistant'){
    const txt=(d.text||'');
    if(!live.body&&stripHud(txt).trim()){const _r=addAssistant(md(stripHud(txt)));live.body=_r.querySelector('.msg-body');live.raw=txt;}
    else if(live.body){ live.raw=txt; live.body.innerHTML=md(stripHud(txt)); }
    if(live.body) live.body.classList.remove('cursor');
    renderPanels(txt);
    if(state.voiceOut){ const p=plain(txt); if(p) speak(p); }
  } else if(k==='plan'){
    const p=document.createElement('div'); p.className='plan-box';
    p.innerHTML='<div class="ph">&#9658; Plan</div><ol>'+(d.steps||[]).map(s=>'<li>'+esc(s)+'</li>').join('')+'</ol>';
    getThread().appendChild(p); live.body=null; scrollDown();
  } else if(k==='tool_call'){
    live.body=null; live.toolRow=addToolRow({name:d.name,summary:d.summary},false);
  } else if(k==='tool_result'){
    if(live.toolRow){
      live.toolRow.classList.remove('pending'); live.toolRow.classList.add(d.ok?'ok':'bad');
      const res=live.toolRow.querySelector('.tres')||document.createElement('span');
      res.className='tres'; res.textContent=(d.ok?'✓ ':'✗ ')+(d.first_line||'').slice(0,90);
      if(!res.parentNode) live.toolRow.appendChild(res); live.toolRow=null;
    }
  } else if(k==='permission'){ live.body=null; showPermission(d);
  } else if(k==='info'||k==='warn'){ addNote(d.text,false); live.body=null;
    /* If the info message mentions tokens, update live.tokensIn */
    const m=d.text&&d.text.match(/~(\d[\d,]*)\s*tokens/);
    if(m) live.tokensIn=parseInt(m[1].replace(/,/g,''),10);
  } else if(k==='error'){ addNote(d.text||'ERROR: SYSTEM FAULT',true); live.body=null;
  } else if(k==='widget'){
    // show_widget SSE event — render a dedicated specialty card. We translate
    // the (type, title, data) payload into a HUD panel so it benefits from
    // dragging/resizing like the inline ```hud``` panels.
    live.body=null;
    renderWidget(d);
  } else if(k==='done'){
    if(live.body) live.body.classList.remove('cursor'); live.body=null;
    if(_thinkTimer){clearInterval(_thinkTimer);_thinkTimer=null;}
    const usage=d.usage||{};
    const hasStats=usage.input||usage.output||usage.ms;
    if(hasStats||live.turnStart){
      const row=document.createElement('div'); row.className='done-stats';
      let parts=[];
      if(usage.input||usage.output) parts.push('tokens in/out '+usage.input+'/'+usage.output);
      if(usage.ms) parts.push(Math.round(usage.ms)+'ms');
      if(live.turnStart){
        const elapsed=((Date.now()-live.turnStart)/1000).toFixed(1);
        parts.push(elapsed+'s');
      }
      row.innerHTML=parts.join('<span class="ds-sep">\u00b7</span>');
      getThread().appendChild(row); scrollDown();
    }
    /* Show token stats in footer */
    const ts=$('#tokenStats');
    if(ts){ let tp=[]; if(usage.input) tp.push('\u2193'+usage.input); if(usage.output) tp.push('\u2191'+usage.output); if(tp.length){ts.textContent=tp.join(' ');ts.classList.remove('hidden');}else{ts.classList.add('hidden');} }
    live.turnStart=null;
  } else if(k==='end'){ finishTurn(); }
  scrollDown();
}
log.addEventListener('click',e=>{
  const btn=e.target.closest('.cb-copy'); if(!btn) return;
  const code=btn.closest('.codeblock').querySelector('pre code');
  navigator.clipboard.writeText(code.textContent||'').then(()=>{ btn.textContent='COPIED'; setTimeout(()=>{btn.textContent='COPY';},1400); });
});

// ---- VOICE OUTPUT (TTS) -----------------------------------------------------
let voices=[];
function loadVoices(){ voices=window.speechSynthesis ? speechSynthesis.getVoices() : []; populateVoiceSelect(); }
if(window.speechSynthesis){ speechSynthesis.onvoiceschanged=loadVoices; loadVoices(); }
function pickVoice(){
  if(!voices.length) return null;
  if(state.voiceName){ const v=voices.find(v=>v.name===state.voiceName); if(v) return v; }
  // prefer a deep/UK English voice
  return voices.find(v=>/en-GB/i.test(v.lang)&&/male|daniel|arthur/i.test(v.name))
      || voices.find(v=>/en-GB/i.test(v.lang)) || voices.find(v=>/^en/i.test(v.lang)) || voices[0];
}
function speak(text){
  if(!window.speechSynthesis) return;
  speechSynthesis.cancel();
  const u=new SpeechSynthesisUtterance(text.slice(0,600));
  const v=pickVoice(); if(v) u.voice=v;
  u.rate=1.0; u.pitch=0.9;
  window.__jSpeak=true; const z=$('#orbZone'); if(z) z.classList.add('speaking'); setOrbLabel('SPEAKING');
  u.onend=()=>{ window.__jSpeak=false; if(z) z.classList.remove('speaking'); setOrbLabel(currentTitle()); };
  speechSynthesis.speak(u);
}
function populateVoiceSelect(){
  const sel=$('#setVoice'); if(!sel) return;
  sel.innerHTML='<option value="">Auto</option>'+
    voices.filter(v=>/^en/i.test(v.lang)).map(v=>'<option value="'+esc(v.name)+'">'+esc(v.name+' — '+v.lang)+'</option>').join('');
  sel.value=state.voiceName||'';
}

// ---- VOICE INPUT (STT) ------------------------------------------------------
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let recog=null, recognizing=false;
if(SR){
  recog=new SR(); recog.lang='en-US'; recog.interimResults=true; recog.continuous=false;
  recog.onstart=()=>{ recognizing=true; $('#micBtn').classList.add('listening'); $('#cmdBox').classList.add('listening');
    $('#orbZone').classList.add('listening'); setOrbLabel('LISTENING'); };
  recog.onend=()=>{ recognizing=false; $('#micBtn').classList.remove('listening'); $('#cmdBox').classList.remove('listening');
    $('#orbZone').classList.remove('listening'); setOrbLabel(currentTitle()); };
  recog.onerror=()=>{ recognizing=false; $('#micBtn').classList.remove('listening'); $('#cmdBox').classList.remove('listening'); };
  recog.onresult=e=>{
    let txt=''; for(let i=0;i<e.results.length;i++) txt+=e.results[i][0].transcript;
    input.value=txt; autoGrow();
    if(e.results[e.results.length-1].isFinal){ setTimeout(()=>{ if(input.value.trim()) submit(); },350); }
  };
}
function toggleMic(){
  if(!SR){ addNote('Voice input not supported in this browser (try Chrome).',true); return; }
  if(recognizing){ recog.stop(); } else { try{ recog.start(); }catch(e){} }
}

// ---- CONFIRM DIALOG ---------------------------------------------------------
let _confirmCb=null;
function showConfirm(msg,cb){
  _confirmCb=cb;
  $('#confirmMsg').textContent=msg;
  $('#confirmModal').classList.remove('hidden');
}
$('#confirmOk').onclick=()=>{ $('#confirmModal').classList.add('hidden'); if(_confirmCb) _confirmCb(); _confirmCb=null; };
$('#confirmCancel').onclick=()=>{ $('#confirmModal').classList.add('hidden'); _confirmCb=null; };
$('#confirmClose').onclick=()=>{ $('#confirmModal').classList.add('hidden'); _confirmCb=null; };
$('#confirmModal').addEventListener('click',e=>{ if(e.target.id==='confirmModal'){ $('#confirmModal').classList.add('hidden'); _confirmCb=null; } });

// ---- CONTEXT MENU (⋮) -------------------------------------------------------
let _ctxChatId=null, _projCtxId=null, _ctxMode='chat';
function showCtx(e,chatId){
  _ctxChatId=chatId; _ctxMode='chat';
  const m=$('#ctxMenu');
  m.innerHTML='<div class="ctx-item" data-action="rename">&#9998; Rename</div>'+
    '<div class="ctx-item" data-action="project">&#128193; Add to Project</div>'+
    '<div class="ctx-sep"></div>'+
    '<div class="ctx-item ctx-danger" data-action="delete">&#128465; Delete</div>';
  m.classList.remove('hidden');
  m.style.left=Math.min(e.clientX,window.innerWidth-170)+'px';
  m.style.top=Math.min(e.clientY,window.innerHeight-120)+'px';
}
function showNewProjectModal(){
  $('#newProjectInput').value='';
  $('#newProjectModal').classList.remove('hidden');
  setTimeout(()=>$('#newProjectInput').focus(),50);
}
function closeNewProjectModal(){ $('#newProjectModal').classList.add('hidden'); }

function showProjectCtx(e,projectId){
  _projCtxId=projectId; _ctxMode='project';
  const m=$('#ctxMenu');
  m.innerHTML='<div class="ctx-item" data-action="proj-config">&#9881; Config</div>'+
    '<div class="ctx-item" data-action="proj-rename">&#9998; Rename</div>'+
    '<div class="ctx-item ctx-danger" data-action="proj-delete">&#128465; Delete</div>';
  m.classList.remove('hidden');
  m.style.left=Math.min(e.clientX,window.innerWidth-170)+'px';
  m.style.top=Math.min(e.clientY,window.innerHeight-120)+'px';
}
function hideCtx(){ $('#ctxMenu').classList.add('hidden'); _ctxChatId=null; _projCtxId=null; }
document.addEventListener('click',e=>{
  const m=$('#ctxMenu');
  if(m.contains(e.target)){
    e.stopPropagation();
    const action=e.target.dataset.action; if(!action) return;
    const chatId=_ctxChatId, projId=_projCtxId;
    hideCtx();
    if(action==='delete'){ showConfirm('Delete this chat?',()=>deleteChat(chatId)); }
    else if(action==='rename'){ showRename(chatId); }
    else if(action==='project'){ showProjectPicker(chatId); }
    else if(action==='proj-rename'){ showRenameProject(projId); }
    else if(action==='proj-delete'){
      showConfirm('Delete this project?',async()=>{
        const r=await api('/api/projects/delete',{id:projId});
        state.projects=r.projects; state.activeProjectId=null; renderSessions();
      });
    }
    else if(action==='proj-config'){ showProjectConfig(projId); }
  } else { hideCtx(); }
});

// ---- RENAME MODAL -----------------------------------------------------------
let _renameId=null;
let _renameMode='chat'; // 'chat' or 'project'
function showRename(chatId){
  _renameId=chatId; _renameMode='chat';
  const c=state.chats.find(c=>c.id===chatId);
  $('#renameInput').value=c?c.title:'';
  $('#renameModal').classList.remove('hidden');
  setTimeout(()=>$('#renameInput').focus(),50);
}
function showRenameProject(projectId){
  _renameId=projectId; _renameMode='project';
  const p=state.projects.find(p=>p.id===projectId);
  $('#renameInput').value=p?p.name:'';
  $('#renameModal').classList.remove('hidden');
  setTimeout(()=>$('#renameInput').focus(),50);
}
function closeRename(){ $('#renameModal').classList.add('hidden'); _renameId=null; _renameMode='chat'; }
$('#renameClose').onclick=closeRename;
$('#renameCancel').onclick=closeRename;
$('#renameOk').onclick=async()=>{
  const val=$('#renameInput').value.trim(); if(!val||!_renameId) return;
  if(_renameMode==='project'){
    const r=await api('/api/projects/rename',{id:_renameId,name:val});
    state.projects=r.projects; renderSessions(); closeRename();
  } else {
    const r=await api('/api/chats/rename',{id:_renameId,title:val});
    state.chats=r.chats; renderSessions(); closeRename();
  }
};
$('#renameInput').addEventListener('keydown',e=>{ if(e.key==='Enter') $('#renameOk').click(); });
$('#renameModal').addEventListener('click',e=>{ if(e.target.id==='renameModal') closeRename(); });
$('#newProjectModal').addEventListener('click',e=>{ if(e.target.id==='newProjectModal') closeNewProjectModal(); });

// ---- PROJECT PICKER MODAL ---------------------------------------------------
let _projPickChatId=null;
async function showProjectPicker(chatId){
  _projPickChatId=chatId;
  const body=$('#projectModalBody'); body.innerHTML='';
  if(!state.projects.length){
     body.innerHTML='<div class="empty-hint md">No projects yet. Create one below.</div>';
  } else {
    state.projects.forEach(p=>{
      const d=document.createElement('div');
      d.className='proj-item-j';
      d.innerHTML='<span class="proj-dot" style="background:'+esc(p.color)+'"></span><span class="proj-name">'+esc(p.name)+'</span>';
      d.onclick=async()=>{
        const r=await api('/api/projects/add_chat',{project_id:p.id,chat_id:_projPickChatId});
        state.projects=r.projects; state.chats=r.chats; renderSessions();
        closeProjectPicker();
      };
      body.appendChild(d);
    });
  }
  $('#projectModal').classList.remove('hidden');
}
function closeProjectPicker(){ $('#projectModal').classList.add('hidden'); _projPickChatId=null; }
$('#projectModalClose').onclick=closeProjectPicker;
$('#projectModalCancel').onclick=closeProjectPicker;
$('#projectModal').addEventListener('click',e=>{ if(e.target.id==='projectModal') closeProjectPicker(); });
$('#projectModalNewBtn').onclick=()=>{
  closeProjectPicker();
  showNewProjectModal();
  // After creating, the new project modal handler will refresh
};

// ---- PROJECT CONFIG MODAL ---------------------------------------------------
let _projConfigId=null;
function showProjectConfig(projectId){
  _projConfigId=projectId;
  const p=state.projects.find(p=>p.id===projectId);
  $('#projConfigPrompt').value=p?(p.system_prompt||''):'';
  $('#projConfigContext').value=p?(p.context||''):'';
  $('#projConfigModal').classList.remove('hidden');
  setTimeout(()=>$('#projConfigPrompt').focus(),50);
}
function closeProjectConfig(){ $('#projConfigModal').classList.add('hidden'); _projConfigId=null; }
$('#projConfigClose').onclick=closeProjectConfig;
$('#projConfigCancel').onclick=closeProjectConfig;
$('#projConfigModal').addEventListener('click',e=>{ if(e.target.id==='projConfigModal') closeProjectConfig(); });
$('#projConfigSave').onclick=async()=>{
  if(!_projConfigId) return;
  const r=await api('/api/projects/config',{
    id:_projConfigId,
    system_prompt:$('#projConfigPrompt').value,
    context:$('#projConfigContext').value
  });
  state.projects=r.projects; closeProjectConfig();
};

// ---- SESSIONS ---------------------------------------------------------------
function currentTitle(){ const c=state.chats.find(c=>c.id===state.currentId); return c?c.title:'New Chat'; }
function renderSessions(){
  const list=$('#sessionList'); if(!list) return; list.innerHTML='';
  // Build a map of project_id -> chat list
  const projChats={};
  state.projects.forEach(p=>{ projChats[p.id]=state.chats.filter(c=>c.project_id===p.id); });
  const unaffiliated=state.chats.filter(c=>!c.project_id);
  // --- Projects expandable group ---
  const projGrp=document.createElement('div'); projGrp.className='sess-group';
  const projHead=document.createElement('div'); projHead.className='sess-group-head'+(state._openProjectsRoot?' open':'');
  projHead.innerHTML='<span class="sg-caret">&#9654;</span><span class="sg-dot" style="background:var(--accent)"></span><span class="sg-name">Projects</span><span class="sg-count">'+state.projects.length+'</span><button class="sg-add" title="New Project">+</button>';
  projHead.querySelector('.sg-add').onclick=e=>{ e.stopPropagation(); showNewProjectModal(); };
  projHead.onclick=e=>{
    if(e.target.closest('.sg-add')) return;
    state._openProjectsRoot=!state._openProjectsRoot;
    renderSessions();
  };
  projGrp.appendChild(projHead);
  const projBody=document.createElement('div'); projBody.className='sess-group-chats'+(state._openProjectsRoot?' open':'');
  if(!state.projects.length){
     projBody.innerHTML='<div class="empty-hint">No projects yet</div>';
  } else {
    state.projects.forEach(p=>{
      const chats=projChats[p.id]||[];
      const isOpen=state._openProjects.has(p.id);
      const pGrp=document.createElement('div'); pGrp.className='sess-group';
      const pHead=document.createElement('div'); pHead.className='sess-group-head'+(isOpen?' open':'');
      pHead.innerHTML='<span class="sg-caret">&#9654;</span><span class="sg-dot" style="background:'+esc(p.color)+'"></span><span class="sg-name">'+esc(p.name)+'</span><span class="sg-count">'+chats.length+'</span><button class="sg-menu" title="Menu">&#8942;</button>';
      pHead.querySelector('.sg-menu').onclick=e=>{ e.stopPropagation(); showProjectCtx(e,p.id); };
      pHead.onclick=e=>{
        if(e.target.closest('.sg-menu')) return;
        if(state._openProjects.has(p.id)) state._openProjects.delete(p.id); else state._openProjects.add(p.id);
        renderSessions();
      };
      pGrp.appendChild(pHead);
      const pBody=document.createElement('div'); pBody.className='sess-group-chats'+(isOpen?' open':'');
      chats.forEach(c=>{ pBody.appendChild(makeChatItem(c)); });
      if(!chats.length) pBody.innerHTML='<div class="empty-hint">No chats</div>';
      pGrp.appendChild(pBody);
      projBody.appendChild(pGrp);
    });
  }
  projGrp.appendChild(projBody);
  list.appendChild(projGrp);
  // --- Chats expandable group ---
  const chatGrp=document.createElement('div'); chatGrp.className='sess-group';
  const chatHead=document.createElement('div'); chatHead.className='sess-group-head'+(state._openUnaffiliated!==false?' open':'');
  chatHead.innerHTML='<span class="sg-caret">&#9654;</span><span class="sg-dot" style="background:var(--text-dim)"></span><span class="sg-name">Chats</span><span class="sg-count">'+unaffiliated.length+'</span>';
  chatHead.onclick=()=>{
    state._openUnaffiliated=state._openUnaffiliated===false?true:false;
    renderSessions();
  };
  chatGrp.appendChild(chatHead);
  const chatBody=document.createElement('div'); chatBody.className='sess-group-chats'+(state._openUnaffiliated!==false?' open':'');
  if(!unaffiliated.length){
    chatBody.innerHTML='<div class="empty-hint">No chats yet</div>';
  } else {
    unaffiliated.forEach(c=>{ chatBody.appendChild(makeChatItem(c)); });
  }
  chatGrp.appendChild(chatBody);
  list.appendChild(chatGrp);
}
function makeChatItem(c){
  const item=document.createElement('div'); item.className='chat-item-j'+(c.id===state.currentId?' active':'');
  item.innerHTML='<span class="ci-arrow">&#9658;</span><span class="ci-title">'+esc(c.title)+'</span><button class="ci-menu-btn" title="Menu">&#8942;</button>';
  item.querySelector('.ci-title').onclick=()=>loadChat(c.id);
  item.querySelector('.ci-menu-btn').onclick=e=>{ e.stopPropagation(); showCtx(e,c.id); };
  return item;
}
function setCurrent(cur){
  state.currentId=cur.id;
  _userMsgIdx=0;
  const s=$('#jSession'); if(s) s.textContent=(cur.id||'--------').slice(0,8).toUpperCase();
  setOrbLabel(cur.title||'New Chat');
  clearLog();
  if(!cur.messages||!cur.messages.length){ showEmpty(); compactOrb(false); return; }
  compactOrb(true);
  let idx=0;
  cur.messages.forEach(m=>{
    let row;
    if(m.role==='user'){ row=addUser(m.content); idx++; }
    else {
      const html=md(stripHud(m.content));
      const hasContent=html&&html.trim();
      if(hasContent){ row=addAssistant(html,m.tools); renderPanels(m.content); idx++; }
      else { (m.tools||[]).forEach(t=>{ const tr=addToolRow({name:t},true); if(tr) tr.style.setProperty('--i',idx++); }); }
    }
    if(row) row.style.setProperty('--i',idx-1);
  });
  scrollDown();
}

// ---- NETWORK ----------------------------------------------------------------
async function api(path,body){
  const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  return r.json();
}
function setModelBadge(m){ const n=$('#msName'); if(n) n.textContent=(m||'').toUpperCase(); }
async function boot(){
  const b=await api('/api/bootstrap');
  state.chats=b.chats; state.settings=b.settings; state.projects=b.projects||[]; state.os=b.os||null;
  state.userName=b.user_name||'';
  setModelBadge(b.model);
  const vs=$('#versionSpan'); if(vs) vs.textContent=b.version||'--';
  renderModelMenu();
  renderSessions(); setCurrent(b.current); renderPersonalOS();
  let initial='today'; try{initial=localStorage.getItem('cagentic_os_view')||'today';}catch(e){}
  showOsView(initial);
  setInterval(refreshPersonalOS,60000);
}
async function newChat(){
  const r=await api('/api/chats/new',{}); state.chats=r.chats; renderSessions(); setCurrent(r.current);
  if(r.current&&r.current.model){state.settings.model=r.current.model;setModelBadge(r.current.model);renderModelMenu();}
  clearViewport(); closeSessions(); showOsView('assistant'); input.focus();
}
async function loadChat(id){
  const r=await api('/api/chats/load',{id}); state.chats=r.chats; clearViewport(); renderSessions(); setCurrent(r.current); if(r.current&&r.current.model){state.settings.model=r.current.model;setModelBadge(r.current.model);renderModelMenu();} closeSessions(); showOsView('assistant');
}
async function deleteChat(id){
  const r=await api('/api/chats/delete',{id}); state.chats=r.chats; state.projects=r.projects||state.projects; renderSessions(); setCurrent(r.current); closeSessions();
}
async function refreshChats(){
  const b=await api('/api/bootstrap'); state.chats=b.chats; state.projects=b.projects||[]; state.os=b.os||state.os; renderSessions(); setOrbLabel(b.current.title||'New Chat'); renderPersonalOS();
}

// ---- MODEL SWITCHER ---------------------------------------------------------
function renderModelMenu(){
  const menu=$('#modelMenu'); const models=state.settings.models||[];
  if(!models.length){ menu.innerHTML='<div class="mm-item">'+esc(state.settings.model||'no models')+'</div>'; return; }
  menu.innerHTML=models.map(m=>'<div class="mm-item'+(m===state.settings.model?' active':'')+'" data-m="'+esc(m)+'">'+
    '<span class="mm-tick">'+(m===state.settings.model?'✓':'')+'</span>'+esc(m)+'</div>').join('');
  menu.querySelectorAll('.mm-item').forEach(it=>{ if(it.dataset.m) it.onclick=()=>switchModel(it.dataset.m); });
}
async function switchModel(m){
  $('#modelMenu').classList.add('hidden');
  const r=await api('/api/model',{model:m});
  state.settings.model=r.model; setModelBadge(r.model); renderModelMenu();
  addNote('Model switched to '+r.model);
}
$('#modelSwitch').onclick=e=>{ e.stopPropagation(); $('#modelMenu').classList.toggle('hidden'); };
document.addEventListener('click',()=>$('#modelMenu').classList.add('hidden'));
$('#modelMenu').onclick=e=>e.stopPropagation();

// ---- DRAWER / MODAL ---------------------------------------------------------
function openSessions(){ $('#sessionsPanel').classList.add('open'); $('#backdrop').classList.remove('hidden'); }
function closeSessions(){ $('#sessionsPanel').classList.remove('open'); $('#backdrop').classList.add('hidden'); }
function openSettings(){
  closeSessions();
  const s=state.settings, sel=$('#setModel'); sel.innerHTML='';
  (s.models&&s.models.length?s.models:[s.model]).forEach(m=>{
    const o=document.createElement('option'); o.value=m; o.textContent=m; if(m===s.model) o.selected=true; sel.appendChild(o);
  });
  $('#setName').value=s.user_name||'';
  $('#setTemp').value=s.temperature; $('#tempVal').textContent=(+s.temperature).toFixed(2);
  $('#setStream').checked=!!s.stream; $('#setYolo').checked=!!s.yolo;
  $('#setGwPort').value=s.gateway_port||8700;
  $('#setGwAuto').checked=!!(s.gateway_auto_start!==false);
  $('#setProactive').checked=!!(s.proactive_enabled!==false);
  $('#setDesktopNotifications').checked=!!(s.desktop_notifications!==false);
  $('#setSysPrompt').value=s.system_prompt||'';
  populateVoiceSelect();
  $('#settingsModal').classList.remove('hidden');
}
function closeSettings(){ $('#settingsModal').classList.add('hidden'); }
async function saveSettings(){
  state.voiceName=$('#setVoice').value||'';
  try{ localStorage.setItem('cagentic_voice',state.voiceName); }catch(e){}
  state.settings=await api('/api/settings',{
    model:$('#setModel').value, user_name:$('#setName').value, temperature:parseFloat($('#setTemp').value),
    stream:$('#setStream').checked, yolo:$('#setYolo').checked,
    gateway_port:parseInt($('#setGwPort').value)||8700,
    gateway_auto_start:$('#setGwAuto').checked,
    proactive_enabled:$('#setProactive').checked,
    desktop_notifications:$('#setDesktopNotifications').checked,
    system_prompt:$('#setSysPrompt').value });
  setModelBadge(state.settings.model); renderModelMenu(); closeSettings();
}

// ---- VOICE OUT TOGGLE -------------------------------------------------------
function toggleVoiceOut(){
  state.voiceOut=!state.voiceOut;
  $('#voiceOutBtn').classList.toggle('active', state.voiceOut);
  $('#voiceOutBtn').innerHTML='[ &#128264; Voice: '+(state.voiceOut?'ON':'OFF')+' ]';
  if(!state.voiceOut && window.speechSynthesis) speechSynthesis.cancel();
  try{ localStorage.setItem('cagentic_voiceout', state.voiceOut?'1':'0'); }catch(e){}
}

// ---- SEND -------------------------------------------------------------------
function setBusy(on){ state.busy=on; sendBtn.disabled=on; input.disabled=on; const bl=$('#busyLabel'); if(bl){bl.textContent='\u25CF '+_curVerb+'\u2026';bl.classList.toggle('hidden',!on);} $('#stopBtn').classList.toggle('hidden',!on); const core=$('#vaultCoreState');if(core){const alive=state.os?.proactive_running?'ALIVE':'STANDBY';core.textContent='CORE · '+(on?'ACTIVE':'IDLE')+'   LINK · ONLINE   RUNNER · '+alive;} }
function finishTurn(){ setBusy(false); const ts=$('#tokenStats'); if(ts) ts.classList.add('hidden'); input.focus(); refreshChats(); }
let _abortCtrl=null;
async function abortGeneration(){
  if(!state.busy) return;
  try{ await fetch('/api/abort',{method:'POST'}); }catch(e){}
  if(_abortCtrl) try{ _abortCtrl.abort(); }catch(e){}
  clearThinking(); addNote('Generation stopped.',false); finishTurn();
}
async function send(text){
  if(state.busy) return;
  showOsView('assistant');
  // Slash commands → /api/cmd instead of /api/chat
  if(text.startsWith('/')){
    const parts=text.split(/\s+/);
    const cmd=parts[0].slice(1);
    const arg1=parts[1]||'';
    const arg2=parts.slice(2).join(' ')||'';
    showThinking(); setOrbLabel(_curVerb+'\u2026'); setBusy(true);
    try{
      const r=await fetch('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd,arg1,arg2})});
      const d=await r.json();
      if(d.current) setCurrent(d.current);
      if(d.model) { state.settings.model=d.model; setModelBadge(d.model); renderModelMenu(); }
      addNote(d.text||'Done',!d.ok);
    }catch(e){ addNote('Command failed: '+e,true); }
    clearThinking(); setBusy(false);
    return;
  }
  if(log.querySelector('.j-empty')) clearLog();
  addUser(text);
  live={body:null,raw:'',toolRow:null,thinking:null,turnStart:null,tokensIn:0,tokensOut:0};
  showThinking(); setOrbLabel(_curVerb+'\u2026'); setBusy(true); compactOrb(true);
  _abortCtrl=new AbortController();
  let res;
  try{ res=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text}),signal:_abortCtrl.signal}); }
  catch(e){ if(e.name==='AbortError'){finishTurn();return;} clearThinking(); addNote('CONNECTION FAILURE',true); finishTurn(); return; }
  if(!res||!res.ok||!res.body){ clearThinking(); addNote('REQUEST FAILED: '+(res?res.status:'no response'),true); finishTurn(); return; }
  try{
    await readSSE(res, handle);
  }catch(e){ console.error('Stream read error:',e); }
  clearThinking(); if(state.busy) finishTurn();
}

// ---- COMPOSER + WIRING ------------------------------------------------------
function autoGrow(){ input.style.height='auto'; input.style.height=Math.min(input.scrollHeight,130)+'px'; }
function submit(){ const t=input.value.trim(); if(!t||state.busy)return; input.value=''; autoGrow(); send(t); }
input.addEventListener('input', autoGrow);
input.addEventListener('keydown', e=>{ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submit();} });
sendBtn.onclick=submit;
$('#stopBtn').onclick=abortGeneration;
$('#micBtn').onclick=toggleMic;
$('#logsBtn').onclick=openSessions;
$('#newMissionBtn').onclick=newChat;
$('#configBtn').onclick=openSettings;
$('#voiceOutBtn').onclick=toggleVoiceOut;
$('#quickAddBtn').onclick=()=>openCapture('inbox');
$('#notificationBtn').onclick=openNotifications;
$('#closeNotifications').onclick=closeNotifications;
$('#markNotificationsRead').onclick=async()=>{const r=await api('/api/os/notifications/action',{action:'read_all'});state.os.notifications=r.notifications||[];state.os.unread_notifications=r.unread_notifications||0;renderNotifications();bindOsActions();};
document.querySelectorAll('.os-nav-item').forEach(el=>el.onclick=()=>showOsView(el.dataset.view));
document.querySelectorAll('[data-capture-type]').forEach(el=>el.onclick=()=>selectCaptureType(el.dataset.captureType));
$('#captureClose').onclick=closeCapture;
$('#captureCancel').onclick=closeCapture;
$('#captureSave').onclick=saveCapture;
$('#captureModal').addEventListener('click',e=>{if(e.target.id==='captureModal')closeCapture();});
$('#captureTitle').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();saveCapture();}});
$('#connectionSettingsBtn').onclick=openSettings;
$('#addConnectionBtn').onclick=openConnectionModal;
$('#connectionModalClose').onclick=closeConnectionModal;
$('#connectionCancel').onclick=closeConnectionModal;
$('#connectionSave').onclick=saveConnection;
$('#connectionKind').onchange=selectConnectionKind;
$('#connectionModal').addEventListener('click',e=>{if(e.target.id==='connectionModal')closeConnectionModal();});
$('#addEmailBtn').onclick=openEmailModal;
$('#inboxConnectEmailBtn').onclick=openEmailModal;
$('#connectionsEmailBtn').onclick=openEmailModal;
$('#emailModalClose').onclick=closeEmailModal;
$('#emailCancel').onclick=closeEmailModal;
$('#emailSave').onclick=saveEmailConnection;
$('#emailModal').addEventListener('click',e=>{if(e.target.id==='emailModal')closeEmailModal();});
$('#addRoutineBtn').onclick=openRoutineModal;
$('#routineModalClose').onclick=closeRoutineModal;
$('#routineCancel').onclick=closeRoutineModal;
$('#routineSave').onclick=saveRoutine;
$('#routineModal').addEventListener('click',e=>{if(e.target.id==='routineModal')closeRoutineModal();});
$('#exportCalendarBtn').onclick=exportCalendar;

$('#closeSessionsBtn').onclick=closeSessions;
$('#newProjectModalClose').onclick=closeNewProjectModal;
$('#newProjectCancel').onclick=closeNewProjectModal;
$('#newProjectOk').onclick=async()=>{
  const name=$('#newProjectInput').value.trim(); if(!name) return;
  closeNewProjectModal();
  const r=await api('/api/projects/create',{name});
  state.projects=r.projects; state._openProjectsRoot=true; state._openProjects.add(r.projects[r.projects.length-1].id); renderSessions();
};
$('#newProjectInput').addEventListener('keydown',e=>{ if(e.key==='Enter') $('#newProjectOk').click(); });
$('#backdrop').onclick=()=>{closeSessions();};
$('#closeSettings').onclick=closeSettings;
$('#cancelSettings').onclick=closeSettings;
$('#saveSettings').onclick=saveSettings;
$('#setTemp').addEventListener('input',e=>{$('#tempVal').textContent=(+e.target.value).toFixed(2);});
$('#settingsModal').addEventListener('click',e=>{if(e.target.id==='settingsModal')closeSettings();});
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='k'){ e.preventDefault(); newChat(); return; }
  if((e.ctrlKey||e.metaKey)&&e.key==='m'){ e.preventDefault(); toggleMic(); return; }
  if((e.ctrlKey||e.metaKey)&&e.key==='s'){ e.preventDefault(); openSettings(); return; }
  if(e.key==='Escape'){
    if(!$('#confirmModal').classList.contains('hidden')){ $('#confirmModal').classList.add('hidden'); _confirmCb=null; }
    else if(!$('#captureModal').classList.contains('hidden')) closeCapture();
    else if(!$('#connectionModal').classList.contains('hidden')) closeConnectionModal();
    else if($('#notificationPanel').classList.contains('open')) closeNotifications();
    else if(!$('#newProjectModal').classList.contains('hidden')) closeNewProjectModal();
    else if(!$('#renameModal').classList.contains('hidden')) closeRename();
    else if(!$('#projectModal').classList.contains('hidden')) closeProjectPicker();
    else if(!$('#projConfigModal').classList.contains('hidden')) closeProjectConfig();
    else if(!$('#settingsModal').classList.contains('hidden')) closeSettings();
    else if($('#sessionsPanel').classList.contains('open')) closeSessions();
    else $('#modelMenu').classList.add('hidden');
  }
});


try{
  state.voiceName=localStorage.getItem('cagentic_voice')||'';
  if(localStorage.getItem('cagentic_voiceout')==='1') toggleVoiceOut();
}catch(e){}

boot();
