
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
  userName: '',
  _openProjects: new Set(), _openUnaffiliated: true, _openProjectsRoot: true,
};

// ---- CLOCK / ORB (retired) --------------------------------------------------
// A wall clock and a 220-particle canvas orb were the two loudest pieces of
// HUD decoration: neither told the user anything about their conversation, and
// together they took roughly 40% of the viewport before a single message. The
// orb's animation loop also ran continuously, which is a real cost on a
// laptop. The greeting that replaced them lives in renderEmptyState().
function setOrbLabel(){ /* no-op: the orb label is gone */ }


// ---- HELPERS ----------------------------------------------------------------
function esc(s){ return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
/* Mirrors fmt.fmt_tokens / fmt.fmt_cost so the two front ends read the same. */
function fmtTokens(n){
  n=Number(n)||0;
  if(Math.abs(n)<1000) return String(n);
  if(Math.abs(n)<1e6) return (n/1000).toFixed(1).replace(/\.0$/,'')+'k';
  return (n/1e6).toFixed(1).replace(/\.0$/,'')+'M';
}
function fmtCost(v){
  v=Number(v)||0;
  if(v===0) return '$0.00';
  /* Cached traffic makes sub-cent turns the common case; '$0.00' would hide them. */
  if(v<0.01) return '$'+v.toFixed(4);
  return '$'+v.toFixed(2);
}
// Allow only http(s) (and data:image for <img>) URLs into href/src attributes;
// anything else (javascript:, data:text/html, etc.) is dropped to '#'.
function safeUrl(u){ u=(u||'').trim(); return /^https?:\/\//i.test(u)?u:'#'; }
function safeImgUrl(u){ u=(u||'').trim(); return (/^https?:\/\//i.test(u)||/^data:image\//i.test(u))?u:''; }
// ---- SYNTAX HIGHLIGHTING ----------------------------------------------------
// Self-contained on purpose: the gateway ships its own assets with no build
// step and a strict same-origin posture, so pulling in highlight.js from a CDN
// isn't an option. This is a small tokenizer — good enough to make code
// scannable, deliberately not a parser.
const HL_ALIASES={js:'js',jsx:'js',javascript:'js',ts:'js',tsx:'js',typescript:'js',json:'json',
  py:'py',python:'py',rb:'py',sh:'sh',bash:'sh',zsh:'sh',shell:'sh',console:'sh',
  html:'xml',xml:'xml',svg:'xml',css:'css',scss:'css',go:'js',rust:'js',rs:'js',
  java:'js',c:'js',cpp:'js','c++':'js',cs:'js',php:'js',sql:'sql',yaml:'yaml',yml:'yaml',
  toml:'yaml',ini:'yaml',diff:'diff',patch:'diff',md:'',markdown:'',text:'',txt:''};
const HL_KEYWORDS={
  js:/\b(const|let|var|function|return|if|else|for|while|class|extends|new|import|from|export|default|async|await|try|catch|finally|throw|typeof|instanceof|null|undefined|true|false|this|super|switch|case|break|continue|delete|in|of|yield|static|public|private|struct|impl|fn|pub|use|mut|match|package|type|interface|func|defer|go|nil|void|int|string|bool|float|double|final|abstract)\b/g,
  py:/\b(def|class|return|if|elif|else|for|while|import|from|as|with|try|except|finally|raise|lambda|yield|pass|break|continue|and|or|not|in|is|None|True|False|self|async|await|global|nonlocal|assert|del)\b/g,
  sh:/\b(if|then|else|elif|fi|for|while|do|done|case|esac|function|return|export|local|echo|cd|set|source|sudo|exit)\b/g,
  sql:/\b(SELECT|FROM|WHERE|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|TABLE|DROP|ALTER|JOIN|LEFT|RIGHT|INNER|OUTER|ON|GROUP|BY|ORDER|HAVING|LIMIT|AS|AND|OR|NOT|NULL|DISTINCT|UNION|INDEX|PRIMARY|KEY|FOREIGN|REFERENCES)\b/gi,
};
// Ordered: whichever pattern matches earliest wins, so a keyword inside a
// string is never highlighted as a keyword.
function _hlRules(lang){
  const r=[];
  if(lang==='diff') return [[/^[+][^+\n].*$/gm,'ha'],[/^[-][^-\n].*$/gm,'hr'],[/^@@.*$/gm,'hm'],[/^[+-]{3}.*$/gm,'hc']];
  if(lang==='xml'){ return [[/&lt;!--[\s\S]*?--&gt;/g,'hc'],[/&lt;\/?[\w:-]+/g,'hk'],[/[\w:-]+=/g,'hf'],[/"[^"\n]*"/g,'hs']]; }
  if(lang==='css'){ return [[/\/\*[\s\S]*?\*\//g,'hc'],[/[.#]?[\w-]+(?=\s*\{)/g,'hk'],[/[\w-]+(?=\s*:)/g,'hf'],[/:[^;{}]+/g,'hs']]; }
  if(lang==='json'||lang==='yaml'){
    r.push([/"(?:[^"\\]|\\.)*?"|'(?:[^'\\]|\\.)*?'/g,'hs']);
    if(lang==='yaml') r.push([/#.*$/gm,'hc'],[/^\s*[\w.-]+(?=\s*:)/gm,'hf']);
    else r.push([/"[\w .-]+"(?=\s*:)/g,'hf']);
    r.push([/\b-?\d+(?:\.\d+)?\b/g,'hn'],[/\b(true|false|null|yes|no)\b/gi,'hk']);
    return r;
  }
  // Comments and strings first — they swallow anything inside them.
  r.push([/\/\*[\s\S]*?\*\/|(?:^|\s)(?:\/\/|#).*$/gm,'hc']);
  r.push([/"(?:[^"\\\n]|\\.)*?"|'(?:[^'\\\n]|\\.)*?'|`(?:[^`\\]|\\.)*?`/g,'hs']);
  if(HL_KEYWORDS[lang]) r.push([HL_KEYWORDS[lang],'hk']);
  r.push([/\b-?\d+(?:\.\d+)?\b/g,'hn']);
  r.push([/\b([A-Za-z_$][\w$]*)(?=\s*\()/g,'hf']);
  return r;
}
// Operates on ALREADY-ESCAPED text and only ever inserts <span class="h*">,
// so it cannot introduce markup the escaper removed.
function escText(s){
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function highlight(escaped, lang){
  const kind=HL_ALIASES[(lang||'').toLowerCase()];
  if(kind===undefined||kind==='') return escaped;
  const rules=_hlRules(kind);
  const spans=[];
  for(const [rx,cls] of rules){
    rx.lastIndex=0; let m;
    while((m=rx.exec(escaped))!==null){
      if(m[0]==='') { rx.lastIndex++; continue; }
      // Keep the match offset honest when the pattern used a lookbehind-ish
      // leading space (the comment rule does).
      let start=m.index, text=m[0];
      const lead=text.match(/^\s+/);
      if(lead && cls==='hc'){ start+=lead[0].length; text=text.slice(lead[0].length); }
      spans.push({start,end:start+text.length,cls});
    }
  }
  spans.sort((a,b)=>a.start-b.start||b.end-a.end);
  let out='',at=0;
  for(const sp of spans){
    if(sp.start<at) continue;            // already inside an earlier match
    if(sp.start>=escaped.length) break;
    out+=escaped.slice(at,sp.start)+'<span class="'+sp.cls+'">'+escaped.slice(sp.start,sp.end)+'</span>';
    at=sp.end;
  }
  return out+escaped.slice(at);
}

// ---- MARKDOWN ---------------------------------------------------------------
function _codeBlockHTML(lang, code){
  const clean=code.replace(/\n$/,'');
  const label=esc((lang||'text').toLowerCase());
  // data-raw carries the *unhighlighted* source so COPY yields real code, not
  // markup, no matter what the highlighter did to it.
  return '<div class="codeblock" data-raw="'+esc(clean)+'">'+
    '<div class="cb-head"><span class="cb-lang">'+label+'</span>'+
    '<button class="cb-copy" title="Copy code">Copy</button></div>'+
    '<pre><code>'+highlight(escText(clean),lang)+'</code></pre></div>';
}
function _tableHTML(rows, aligns){
  const cell=(text,i,tag)=>{
    const a=aligns[i]?' style="text-align:'+aligns[i]+'"':'';
    return '<'+tag+a+'>'+_inline(text.trim())+'</'+tag+'>';
  };
  let out='<div class="md-table-wrap"><table class="md-table"><thead><tr>';
  rows[0].forEach((c,i)=>{ out+=cell(c,i,'th'); });
  out+='</tr></thead><tbody>';
  rows.slice(1).forEach(r=>{
    out+='<tr>'; r.forEach((c,i)=>{ out+=cell(c,i,'td'); }); out+='</tr>';
  });
  return out+'</tbody></table></div>';
}
function _splitRow(line){
  return line.replace(/^\s*\|/,'').replace(/\|\s*$/,'').split('|');
}
// Inline formatting only — the caller has already escaped and de-blocked.
function _inline(s){
  s=s.replace(/`([^`\n]+)`/g,(m,c)=>'<code>'+c+'</code>');
  s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g,'$1<em>$2</em>');
  s=s.replace(/(^|\s)_([^_\n]+)_(?!\w)/g,'$1<em>$2</em>');
  s=s.replace(/~~([^~]+)~~/g,'<del>$1</del>');
  // Images before links — otherwise the link rule eats the ](…) and leaves a
  // stray "!". safeImgUrl drops anything that isn't http(s) or a data:image.
  s=s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,(m,alt,u)=>{
    const safe=safeImgUrl(u.replace(/&amp;/g,'&'));
    return safe?'<img class="md-img" src="'+esc(safe)+'" alt="'+alt+'" loading="lazy">':alt;
  });
  s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    (m,t,u)=>'<a href="'+esc(safeUrl(u.replace(/&amp;/g,'&')))+'" target="_blank" rel="noopener noreferrer">'+t+'</a>');
  return s;
}
// Block-level renderer. Line-oriented rather than one big regex chain, which
// is what makes nested lists, tables and blockquotes tractable at all.
function md(src) {
  const blocks=[];
  let s=(src||'').replace(/```(\w*)\n?([\s\S]*?)```/g,(m,lang,code)=>{
    blocks.push(_codeBlockHTML(lang,code));
    return '\x00B'+(blocks.length-1)+'\x00';
  });
  s=esc(s);
  let html=_mdBlocks(s.split('\n'));
  html=html.replace(/\x00B(\d+)\x00/g,(m,idx)=>blocks[+idx]);
  return html;
}
// Takes ALREADY-ESCAPED lines. Blockquotes recurse straight back in here
// rather than un-escaping and re-running md(), which used to turn a literal
// "&amp;" in the source into a visible "&amp;".
function _mdBlocks(lines) {
  const out=[];
  let para=[];
  const flushPara=()=>{ if(para.length){ out.push('<p>'+_inline(para.join('<br>'))+'</p>'); para=[]; } };

  for(let i=0;i<lines.length;i++){
    const line=lines[i];
    const bare=line.trim();

    if(!bare){ flushPara(); continue; }
    if(/^\x00B\d+\x00$/.test(bare)){ flushPara(); out.push(bare); continue; }

    // Table: a header row followed by a |---|---| delimiter.
    if(bare.includes('|') && i+1<lines.length && /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i+1]) && lines[i+1].includes('-')){
      const header=_splitRow(bare);
      const aligns=_splitRow(lines[i+1].trim()).map(c=>{
        const t=c.trim();
        if(t.startsWith(':')&&t.endsWith(':')) return 'center';
        if(t.endsWith(':')) return 'right';
        return '';
      });
      const rows=[header]; i+=2;
      while(i<lines.length && lines[i].trim() && lines[i].includes('|')){ rows.push(_splitRow(lines[i].trim())); i++; }
      i--;
      flushPara(); out.push(_tableHTML(rows,aligns)); continue;
    }

    // Headings — real levels, not everything flattened to h3.
    const h=bare.match(/^(#{1,6})\s+(.+)$/);
    if(h){ flushPara(); const lv=Math.min(6,h[1].length); out.push('<h'+lv+'>'+_inline(h[2])+'</h'+lv+'>'); continue; }

    if(/^(?:---|\*\*\*|___)$/.test(bare)){ flushPara(); out.push('<hr>'); continue; }

    // Blockquote — consecutive "> " lines become one quote.
    if(/^&gt;\s?/.test(bare)){
      flushPara(); const buf=[];
      while(i<lines.length && /^\s*&gt;\s?/.test(lines[i])){ buf.push(lines[i].replace(/^\s*&gt;\s?/,'')); i++; }
      i--;
      out.push('<blockquote>'+_mdBlocks(buf)+'</blockquote>');
      continue;
    }

    // Lists, with indentation-driven nesting for both bullets and numbers.
    const li=line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if(li){
      flushPara();
      const items=[];
      while(i<lines.length){
        const m2=lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if(!m2){
          // A plain indented line continues the previous item.
          if(items.length && /^\s{2,}\S/.test(lines[i]) && lines[i].trim()){ items[items.length-1].text+='<br>'+lines[i].trim(); i++; continue; }
          break;
        }
        items.push({depth:Math.floor(m2[1].replace(/\t/g,'  ').length/2), ordered:/\d/.test(m2[2]), text:m2[3]});
        i++;
      }
      i--;
      out.push(_renderList(items,0,{i:0}));
      continue;
    }

    para.push(bare);
  }
  flushPara();
  return out.join('');
}
function _renderList(items, depth, cur){
  const ordered=items[cur.i] && items[cur.i].ordered;
  let out='<'+(ordered?'ol':'ul')+'>';
  while(cur.i<items.length){
    const it=items[cur.i];
    if(it.depth<depth) break;
    if(it.depth>depth){ out+=_renderList(items,it.depth,cur); continue; }
    cur.i++;
    let inner='<li>'+_inline(it.text);
    if(cur.i<items.length && items[cur.i].depth>depth) inner+=_renderList(items,items[cur.i].depth,cur);
    out+=inner+'</li>';
  }
  return out+'</'+(ordered?'ol':'ul')+'>';
}
// strip plain text of markdown/HUD for speech
function plain(text){
  return stripHud(text).replace(/```[\s\S]*?```/g,' code block ')
    .replace(/[#*`>_]/g,'').replace(/\[([^\]]+)\]\([^)]+\)/g,'$1').replace(/\s+/g,' ').trim();
}
// Pin-to-bottom, but only when the user is already there. Unconditionally
// yanking the viewport down on every streamed token makes it impossible to
// read back through a long reply while it is still generating.
const STICK_SLACK=80;   // px from the bottom that still counts as "at bottom"
let _stick=true;
function atBottom(){ return log.scrollHeight-log.scrollTop-log.clientHeight<=STICK_SLACK; }
log.addEventListener('scroll',()=>{
  _stick=atBottom();
  const btn=$('#jumpBtn'); if(btn) btn.classList.toggle('hidden',_stick);
});
function scrollDown(force){
  if(force===true) _stick=true;
  if(!_stick) return;
  log.scrollTop=log.scrollHeight;
}
// Long sessions otherwise grow an unbounded DOM: a few hundred tool rows and
// code blocks visibly degrade scrolling. Trim from the top, which is the part
// already scrolled past, and say so rather than silently dropping history —
// the full transcript is still on the server.
const MAX_THREAD_NODES=320;
const TRIM_TO=240;
function trimThread(){
  const thread=log.querySelector('.j-thread');
  if(!thread||thread.children.length<=MAX_THREAD_NODES) return;
  const before=log.scrollHeight;
  let drop=thread.children.length-TRIM_TO;
  while(drop-- > 0 && thread.firstElementChild){
    if(thread.firstElementChild.classList.contains('thread-trimmed')) break;
    thread.firstElementChild.remove();
  }
  if(!thread.querySelector('.thread-trimmed')){
    const note=document.createElement('div');
    note.className='note-row thread-trimmed';
    note.textContent='Earlier messages hidden to keep this page fast — reopen the chat to see them all.';
    thread.insertBefore(note,thread.firstChild);
  }
  // Keep the viewport where the user was reading rather than jumping.
  if(!_stick) log.scrollTop=Math.max(0,log.scrollTop-(before-log.scrollHeight));
}
function getThread(){ let t=log.querySelector('.j-thread'); if(!t){t=document.createElement('div');t.className='j-thread';log.appendChild(t);} return t; }
function clearLog(){ log.innerHTML=''; }
function avatarHTML(){
  return '<div class="j-avatar" aria-hidden="true"><svg class="ico"><use href="#i-spark"/></svg></div>';
}
/* setOrbLabel is a no-op defined near the top of the file; compactOrb went
   with the orb it collapsed. Both are kept callable so the call sites that
   only ever drove decoration don't need touching. */
function compactOrb(){ /* no-op: the orb is gone */ }

// Dialog focus belongs inside the dialog until it closes. The gateway has no
// framework, so this small shared helper provides initial focus, trapping and
// return-to-opener behavior for every modal instead of duplicating it six times.
const _dialogReturnFocus=new WeakMap();
function _visibleDialog(){ return document.querySelector('.modal:not(.hidden)'); }
function _focusableIn(el){
  return [...el.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
    .filter(node=>!node.hidden&&node.getClientRects().length);
}
function openDialog(target, initialSelector, returnFocus){
  const modal=typeof target==='string'?$(target):target; if(!modal) return;
  hideCtx(); closeModelMenu();
  $('#restorePill').classList.remove('open');
  $('#restorePillBtn').setAttribute('aria-expanded','false');
  _dialogReturnFocus.set(modal,returnFocus||document.activeElement);
  modal.classList.remove('hidden');
  syncSidebarUI();
  requestAnimationFrame(()=>{
    const initial=initialSelector?modal.querySelector(initialSelector):null;
    (initial||_focusableIn(modal)[0]||modal).focus();
  });
}
function closeDialog(target){
  const modal=typeof target==='string'?$(target):target; if(!modal) return;
  modal.classList.add('hidden');
  const opener=_dialogReturnFocus.get(modal); _dialogReturnFocus.delete(modal);
  syncSidebarUI();
  if(!_visibleDialog()&&opener&&opener.isConnected) requestAnimationFrame(()=>opener.focus());
}
document.addEventListener('keydown',e=>{
  if(e.key!=='Tab') return;
  const modal=_visibleDialog(); if(!modal) return;
  const items=_focusableIn(modal); if(!items.length){ e.preventDefault(); modal.focus(); return; }
  const first=items[0], last=items[items.length-1];
  if(e.shiftKey&&document.activeElement===first){ e.preventDefault(); last.focus(); }
  else if(!e.shiftKey&&document.activeElement===last){ e.preventDefault(); first.focus(); }
});

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
const THINK_RX=/<think(?:ing)?>([\s\S]*?)<\/think(?:ing)?>/gi;
/* Local reasoning models (DeepSeek-R1, QwQ, ...) wrap their reasoning in
   <think> tags inside the ordinary content stream. Nothing here handled them,
   so the tags were escaped and rendered as literal text in the answer body.
   The terminal has shown this as dim italic since day one; the web UI showed
   the raw markup. */
function stripThink(text){ return (text||'').replace(THINK_RX,'').trim(); }
function extractThink(text){
  const out=[]; let m; THINK_RX.lastIndex=0;
  while((m=THINK_RX.exec(text||''))!==null){ const t=(m[1]||'').trim(); if(t) out.push(t); }
  return out;
}
function addThinkingBlock(text){
  text=(text||'').trim(); if(!text) return null;
  const box=document.createElement('details'); box.className='think-box';
  box.innerHTML='<summary>reasoning</summary><div class="think-body">'+esc(text)+'</div>';
  getThread().appendChild(box); scrollDown(); return box;
}
/* Reasoning arrives incrementally on the delta stream, so the live block is
   rewritten in place rather than appended once per token. */
function syncThinking(raw){
  const blocks=extractThink(raw);
  if(!blocks.length) return;
  const text=blocks.join('\n\n');
  if(!live.thinkBox||!live.thinkBox.isConnected) live.thinkBox=addThinkingBlock(text);
  else { const body=live.thinkBox.querySelector('.think-body'); if(body) body.textContent=text; }
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
      '<button class="hud-win-close" type="button" title="Close" aria-label="Close '+esc(title)+' panel"><svg class="ico sm" aria-hidden="true"><use href="#i-close"/></svg></button></div>'+
      '<div class="hud-win-body">'+inner+'</div>'+
      '<div class="hud-win-resize"></div>';
    win.querySelector('.hud-win-close').addEventListener('click',e=>{e.stopPropagation();_closeWindow(win);});
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
      return '<button class="ix-choice" data-i="'+i+'"><span class="ix-choice-mark"><svg class="ico sm" aria-hidden="true"><use href="#i-chevron-right"/></svg></span>'+esc(label)+'</button>';
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
    '<button class="hud-win-close" type="button" title="Close" aria-label="Close '+esc(title)+' panel"><svg class="ico sm" aria-hidden="true"><use href="#i-close"/></svg></button></div>'+
    '<div class="hud-win-body sw-body">'+inner+'</div>'+
    '<div class="hud-win-resize"></div>';
  win.querySelector('.hud-win-close').addEventListener('click',e=>{e.stopPropagation();_closeWindow(win);});
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
  return {
    x: Math.max(8,Math.min(x,Math.max(8,lw-260))),
    y: Math.max(8,Math.min(y,Math.max(8,lh-200)))
  };
}
// One shared pointer manager drives every window's drag + resize, so windows
// don't each leak a set of document-level listeners. Pointer events unify
// mouse and touch in a single path.
let _drag=null; // {win, mode:'move'|'resize', sx, sy, ox, oy, ow, oh}
function _initWindow(win){
  const head=win.querySelector('.hud-win-head');
  if(head) head.addEventListener('pointerdown',e=>{
    if(e.target.closest('.hud-win-close')) return;
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
  clampHudWindows();
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
  requestAnimationFrame(clampHudWindows);
}
function clampHudWindows(){
  const layer=$('#windowLayer'); if(!layer) return;
  layer.querySelectorAll('.hud-window').forEach(win=>{
    if(win.style.display==='none') return;
    const edge=8;
    const maxX=Math.max(edge,layer.clientWidth-win.offsetWidth-edge);
    const maxY=Math.max(edge,layer.clientHeight-win.offsetHeight-edge);
    const x=Number.parseFloat(win.style.left)||edge;
    const y=Number.parseFloat(win.style.top)||edge;
    win.style.left=Math.max(edge,Math.min(x,maxX))+'px';
    win.style.top=Math.max(edge,Math.min(y,maxY))+'px';
  });
}
function _updateRestorePill(){
  const pill=$('#restorePill');
  const dd=$('#restoreDropdown');
  if(!pill) return;
  if(state.closedWindows.length===0){
    pill.style.display='none';
    pill.classList.remove('open');
    $('#restorePillBtn').setAttribute('aria-expanded','false');
    return;
  }
  pill.style.display='';
  pill.querySelector('.restore-pill-count').textContent=state.closedWindows.length;
  dd.innerHTML=state.closedWindows.map((w,i)=>{
    const t=w.querySelector('.hud-win-title');
    const label=t?t.textContent:('Panel '+(i+1));
    return '<button class="restore-item" type="button" role="menuitem" data-ri="'+i+'">'+
      '<svg class="ico sm" aria-hidden="true"><use href="#i-retry"/></svg>'+esc(label)+'</button>';
  }).join('');
  dd.querySelectorAll('.restore-item').forEach(el=>{
    el.onclick=()=>{
      const idx=+el.dataset.ri, win=state.closedWindows[idx]; _restoreWindow(win);
      requestAnimationFrame(()=>{
        const remaining=[...dd.querySelectorAll('.restore-item')];
        if(remaining.length) remaining[Math.min(idx,remaining.length-1)].focus();
        else win.querySelector('.hud-win-close')?.focus();
      });
    };
  });
}
$('#restorePillBtn').addEventListener('click',e=>{
  const pill=$('#restorePill'); pill.classList.toggle('open');
  e.currentTarget.setAttribute('aria-expanded',pill.classList.contains('open')?'true':'false');
});
$('#restorePillBtn').addEventListener('keydown',e=>{
  if(e.key==='ArrowUp'||e.key==='ArrowDown'){
    e.preventDefault();
    const pill=$('#restorePill'); pill.classList.add('open');
    e.currentTarget.setAttribute('aria-expanded','true');
    const items=[...$('#restoreDropdown').querySelectorAll('.restore-item')];
    (e.key==='ArrowUp'?items[items.length-1]:items[0])?.focus();
  }
});
$('#restoreDropdown').addEventListener('keydown',e=>{
  const items=[...e.currentTarget.querySelectorAll('.restore-item')]; if(!items.length) return;
  const at=Math.max(0,items.indexOf(document.activeElement));
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){
    e.preventDefault(); items[(at+(e.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();
  } else if(e.key==='Home'){ e.preventDefault(); items[0].focus(); }
  else if(e.key==='End'){ e.preventDefault(); items[items.length-1].focus(); }
  else if(e.key==='Escape'){
    e.preventDefault(); $('#restorePill').classList.remove('open');
    $('#restorePillBtn').setAttribute('aria-expanded','false'); $('#restorePillBtn').focus();
  }
});
// Close dropdown when clicking outside
document.addEventListener('pointerdown',e=>{
  const pill=$('#restorePill');
  if(pill && !pill.contains(e.target)){
    pill.classList.remove('open'); $('#restorePillBtn').setAttribute('aria-expanded','false');
  }
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
  if(win && !e.target.closest('.hud-win-close')){
    // move to end of DOM = top of stack
    e.currentTarget.appendChild(win);
  }
});

// ---- EMPTY STATE ------------------------------------------------------------
/* Suggestions. The emoji were the loudest hobby-project signal on the first
   screen a user ever sees — they render differently on every platform and sit
   at a different weight and colour from everything around them. These use the
   same sprite as the rest of the UI, and the list is shorter: eight cards is a
   menu, four is a suggestion. */
const QUICK = [
  {icon:'i-search', title:'Research a topic', sub:'Search, compare, and summarize sources', prompt:'Research and summarize '},
  {icon:'i-folder', title:'Explore this workspace', sub:'Understand files, structure, and recent work', prompt:'Help me understand this workspace'},
  {icon:'i-edit', title:'Draft something', sub:'Turn an idea into clear, polished writing', prompt:'Help me draft '},
  {icon:'i-more', title:'Create an interactive view', sub:'Build a useful panel with controls', prompt:'Create an interactive panel for '},
];
function showEmpty() {
  clearLog();
  const wrap=document.createElement('div'); wrap.className='j-empty';
  const hour=new Date().getHours();
  const greet=hour<5?'Still up?':hour<12?'Good morning':hour<18?'Good afternoon':'Good evening';
  const who=(state.userName||'').trim();
  wrap.innerHTML=
    '<div class="empty-mark" aria-hidden="true"><svg class="ico"><use href="#i-spark"/></svg></div>'+
    '<h1 class="j-empty-title">'+esc(greet)+(who?', '+esc(who):'')+'</h1>'+
    '<p class="j-empty-sub">How can I help you today?</p>'+
    '<div class="quick-cards">'+
    QUICK.map((q,i)=>`<button class="qcard" style="--i:${i}" data-prompt="${esc(q.prompt)}">`+
      `<svg class="ico qcard-icon" aria-hidden="true"><use href="#${q.icon}"/></svg>`+
      `<span class="qcard-text"><span class="qcard-title">${esc(q.title)}</span>`+
      `<span class="qcard-sub">${esc(q.sub)}</span></span></button>`).join('')+'</div>';
  log.appendChild(wrap);
  wrap.querySelectorAll('.qcard').forEach(c=>{
    c.onclick=()=>{ input.value=c.dataset.prompt; autoGrow(); syncComposerState(); input.focus(); };
  });
}

// ---- RENDERING --------------------------------------------------------------
let _userMsgIdx=0;
const _ICO={
  copy:'<svg class="ico sm" aria-hidden="true"><use href="#i-copy"/></svg>',
  redo:'<svg class="ico sm" aria-hidden="true"><use href="#i-retry"/></svg>',
  edit:'<svg class="ico sm" aria-hidden="true"><use href="#i-edit"/></svg>',
  trash:'<svg class="ico sm" aria-hidden="true"><use href="#i-trash"/></svg>',
};const MSG_ACTIONS_HTML=
  '<button class="msg-act-btn" type="button" data-act="copy" title="Copy message" aria-label="Copy message">'+_ICO.copy+'<span>Copy</span></button>'+
  '<button class="msg-act-btn" type="button" data-act="resend" title="Send again" aria-label="Send again">'+_ICO.redo+'<span>Retry</span></button>'+
  '<button class="msg-act-btn" type="button" data-act="edit" title="Edit and resend" aria-label="Edit and resend">'+_ICO.edit+'<span>Edit</span></button>'+
  '<button class="msg-act-btn del-btn" type="button" data-act="delete" title="Delete from here" aria-label="Delete from here">'+_ICO.trash+'<span>Delete</span></button>';
// The assistant's own toolbar: copy the reply, or regenerate it from the user
// turn above. Regenerate is what the REPL's /retry does, finally reachable here.
const REPLY_ACTIONS_HTML=
  '<button class="msg-act-btn" type="button" data-act="copy" title="Copy reply" aria-label="Copy reply">'+_ICO.copy+'<span>Copy</span></button>'+
  '<button class="msg-act-btn" type="button" data-act="regen" title="Regenerate this reply" aria-label="Regenerate this reply">'+_ICO.redo+'<span>Regenerate</span></button>';
function _wireCopy(btn, getText){
  btn.onclick=()=>{
    const label=btn.querySelector('span');
    copyText(getText()).then(()=>{
      const was=label.textContent; label.textContent='Copied'; btn.classList.add('did-copy');
      setTimeout(()=>{label.textContent=was;btn.classList.remove('did-copy');},1400);
    }).catch(()=>{ label.textContent='Failed'; });
  };
}
function wireMsgActions(row, idx, text){
  _wireCopy(row.querySelector('[data-act="copy"]'), ()=>row.querySelector('.bubble').textContent||'');
  row.querySelector('[data-act="resend"]').onclick=()=>resendMsg(idx,row);
  row.querySelector('[data-act="edit"]').onclick=()=>editMsg(idx,row,text);
  row.querySelector('[data-act="delete"]').onclick=()=>deleteMsg(idx,row);
}
// Regenerate: find the user turn this reply answered and re-run it.
function regenerate(replyRow){
  if(state.busy) return;
  const thread=getThread();
  const kids=[...thread.children];
  let userRow=null;
  for(let i=kids.indexOf(replyRow)-1;i>=0;i--){
    if(kids[i].classList.contains('msg-row')&&kids[i].classList.contains('user')){ userRow=kids[i]; break; }
  }
  if(!userRow){ addNote('Nothing to regenerate — no user message above this reply.',true); return; }
  const rows=[...thread.querySelectorAll('.msg-row.user')];
  const idx=rows.indexOf(userRow);
  const text=userRow.querySelector('.bubble').textContent||'';
  truncateAfter(userRow,false);
  streamEdit(idx,text);
}
function addUser(text){
    const idx=_userMsgIdx++;
    const r=document.createElement('div'); r.className='msg-row user'; r.dataset.idx=idx;
    r.innerHTML='<div class="bubble">'+esc(text)+'</div><div class="msg-actions">'+MSG_ACTIONS_HTML+'</div>';
    wireMsgActions(r, idx, text);
    getThread().appendChild(r); trimThread(); scrollDown(true); return r;
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
  // Backend edit/delete endpoints address visible user messages by their
  // current zero-based position. Keep the next locally assigned index aligned
  // after truncating an earlier branch of the conversation.
  _userMsgIdx=thread.querySelectorAll('.msg-row.user').length;
}

function resendMsg(idx,row){
  if(state.busy) return;
  const bubble=row.querySelector('.bubble');
  const text=bubble.textContent||'';
  truncateAfter(row, false);
  streamEdit(idx,text);
}
function streamEdit(idx,text){
  live={body:null,raw:'',toolRow:null,thinking:null,thinkBox:null,turnStart:null,tokensIn:0,tokensOut:0};
  showThinking(); setOrbLabel(_curVerb+'\u2026'); setBusy(true); compactOrb(true);
  fetch('/api/chat/edit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:idx,message:text})})
  .then(r=>{ if(!r.ok||!r.body) throw new Error(r.status); return readSSE(r,handle); })
  .then(()=>{clearThinking();if(state.busy)finishTurn();})
  .catch(()=>{clearThinking();addNote('Connection lost. Try again.',true);finishTurn();});
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
  actions.innerHTML='<button class="msg-act-btn edit-save" title="Save &amp; send"><svg class="ico sm" aria-hidden="true"><use href="#i-check"/></svg><span>Save</span></button>'+'<button class="msg-act-btn edit-cancel" title="Cancel"><svg class="ico sm" aria-hidden="true"><use href="#i-close"/></svg><span>Cancel</span></button>';
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
    r.innerHTML=avatarHTML()+'<div class="msg-body">'+(html||'')+'</div>'+
      '<div class="msg-actions reply">'+REPLY_ACTIONS_HTML+'</div>';
    wireReplyActions(r);
    getThread().appendChild(r);
    (tools||[]).forEach(t=>addToolRow({name:t},true));
    trimThread(); scrollDown(); return r;
  }
function wireReplyActions(row){
  const acts=row.querySelector('.msg-actions.reply'); if(!acts) return;
  _wireCopy(acts.querySelector('[data-act="copy"]'), ()=>{
    // Copy the reply as text the user can paste elsewhere — code blocks keep
    // their fences, everything else loses its markup.
    const body=row.querySelector('.msg-body'); if(!body) return '';
    const parts=[];
    body.childNodes.forEach(n=>{
      if(n.nodeType===1&&n.classList&&n.classList.contains('codeblock')){
        const lang=(n.querySelector('.cb-lang')||{}).textContent||'';
        parts.push('```'+lang+'\n'+(n.dataset.raw||'')+'\n```');
      } else parts.push((n.textContent||'').trim());
    });
    return parts.filter(Boolean).join('\n\n');
  });
  acts.querySelector('[data-act="regen"]').onclick=()=>regenerate(row);
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
  row.setAttribute('role','status'); row.setAttribute('aria-live','polite');
  row.dataset.name=t.name||'';
  const isCmd=(t.name||'').startsWith('run_')||(t.name||'').startsWith('bash');
  const icon='<svg class="ico sm" aria-hidden="true"><use href="#'+(isCmd?'i-chevron-right':'i-check')+'"/></svg>';
  const iconColor=isCmd?'var(--warn)':'var(--accent)';
  row.innerHTML='<span class="tool-icon" style="color:'+iconColor+'">'+icon+'</span>'+
    '<span class="tname">'+esc(t.name||'')+'</span>'+
    (t.summary?'<span class="tsum">'+esc(t.summary)+'</span>':'')+
    (done?'':'<span class="tres">Running&#8230;</span>');
  getThread().appendChild(row); scrollDown(); return row;
}
function addNote(text, isErr){
  const n=document.createElement('div'); n.className='note-row'+(isErr?' err':'');
  n.setAttribute('role',isErr?'alert':'status');
  n.textContent=text||''; getThread().appendChild(n); scrollDown();
}
function showPermission(d){
  const box=document.createElement('div'); box.className='perm-box';
  box.setAttribute('role','group'); box.setAttribute('aria-label','Approval needed');
  box.innerHTML='<div class="pq">Approval needed for <code>'+esc(d.tool)+'</code>'+(d.summary?' &mdash; '+esc(d.summary):'')+' </div>';
  // Show the patch before the buttons — approving a file change you can't see
  // is exactly what this preview exists to prevent.
  if(d.diff){
    const pre=document.createElement('pre'); pre.className='perm-diff';
    d.diff.split('\n').forEach(line=>{
      const span=document.createElement('span');
      span.className = line.startsWith('+++')||line.startsWith('---') ? 'dh'
                     : line.startsWith('@@') ? 'dm'
                     : line.startsWith('+') ? 'da'
                     : line.startsWith('-') ? 'dd' : '';
      span.textContent=line+'\n'; pre.appendChild(span);
    });
    box.appendChild(pre);
  }
  // What the user needs to know before deciding, not after: which sandbox a
  // command will run under, and whether it asked for network access.
  const facts=[];
  if(d.sandbox) facts.push({text:d.sandbox, warn:/unconfined/.test(d.sandbox)});
  if(d.network) facts.push({text:'requests network access', warn:true});
  if(facts.length){
    const row=document.createElement('div'); row.className='perm-facts';
    facts.forEach(f=>{
      const chip=document.createElement('span');
      chip.className='perm-fact'+(f.warn?' warn':'');
      chip.textContent=f.text; row.appendChild(chip);
    });
    box.appendChild(row);
  }
  const btns=document.createElement('div'); btns.className='perm-btns';
  const answer=(a,past,rule)=>{
    box.innerHTML='<div class="pq"><code>'+esc(d.tool)+'</code></div><div class="perm-decided">'+esc(past)+'</div>';
    fetch('/api/permission',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(rule?{answer:a,id:d.id,rule}:{answer:a,id:d.id})});
  };
  const choices=[['yes','Approve','approved',null]];
  // Offer the narrow standing grant ahead of the blanket one — "always allow
  // git status" is a far smaller decision than "always allow every command".
  if(d.rule) choices.push(['rule','Always allow '+d.rule,'rule added',d.rule]);
  choices.push(['always','Always allow '+d.tool,'always allowed',null]);
  choices.push(['no','Deny','denied',null]);
  choices.forEach(([a,l,p,rule])=>{
    const b=document.createElement('button');
    b.className=a==='rule'?'rule':a; b.textContent=l;
    b.onclick=()=>answer(a,p,rule); btns.appendChild(b);
  });
  box.appendChild(btns); getThread().appendChild(box); scrollDown(true);
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
let live={body:null,raw:'',toolRow:null,thinking:null,thinkBox:null,turnStart:null,tokensIn:0,tokensOut:0};
let _thinkTimer=null;
const _VERBS=['Thinking','Reading context','Working','Reviewing'];
let _curVerb='thinking';
function randomVerb(){ return _VERBS[Math.floor(Math.random()*_VERBS.length)]; }
function showThinking(){
  _curVerb=randomVerb();
  const t=document.createElement('div'); t.className='thinking-row';
  t.setAttribute('role','status'); t.setAttribute('aria-live','polite');
  t.innerHTML=avatarHTML()+'<span>'+_curVerb+'\u2026</span><div class="thinking-dots" aria-hidden="true"><span></span><span></span><span></span></div><span class="thinking-timer" id="thinkTimer"></span>';
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
    syncThinking(live.raw); live.body.innerHTML=md(stripThink(stripHud(live.raw)));
    live.body.classList.add('cursor'); scrollDown();
  } else if(k==='assistant'){
    const txt=(d.text||'');
    syncThinking(txt);
    if(!live.body&&stripThink(stripHud(txt)).trim()){const _r=addAssistant(md(stripThink(stripHud(txt))));live.body=_r.querySelector('.msg-body');live.raw=txt;}
    else if(live.body){ live.raw=txt; live.body.innerHTML=md(stripThink(stripHud(txt))); }
    if(live.body) live.body.classList.remove('cursor');
    renderPanels(txt);
    if(state.voiceOut){ const p=plain(txt); if(p) speak(p); }
  } else if(k==='thinking'){
    /* The engine emits this for models that wrap reasoning in <think> tags.
       Nothing rendered it before, so on the web the reasoning was either lost
       (non-streaming) or dumped raw into the answer body (streaming). */
    addThinkingBlock(d.text||'');
    live.body=null;
  } else if(k==='plan'){
    const p=document.createElement('div'); p.className='plan-box';
    p.innerHTML='<div class="ph">Plan</div><ol>'+(d.steps||[]).map(s=>'<li>'+esc(s)+'</li>').join('')+'</ol>';
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
  } else if(k==='error'){ addNote(d.text||'Something went wrong. Try again.',true); live.body=null;
  } else if(k==='widget'){
    // show_widget SSE event — render a dedicated specialty card. We translate
    // the (type, title, data) payload into a HUD panel so it benefits from
    // dragging/resizing like the inline ```hud``` panels.
    live.body=null;
    renderWidget(d);
  } else if(k==='done'){
    if(live.body) live.body.classList.remove('cursor'); live.body=null;
    if(_thinkTimer){clearInterval(_thinkTimer);_thinkTimer=null;}
    /* turn_usage is this turn alone; usage is the session running total, which
       read like a per-turn cost while silently growing every turn. */
    const usage=d.turn_usage||d.usage||{};
    const hasStats=usage.input||usage.output||usage.ms;
    if(hasStats||live.turnStart){
      const row=document.createElement('div'); row.className='done-stats';
      let parts=[];
      if(usage.input||usage.output) parts.push('\u2191'+fmtTokens(usage.input)+' \u2193'+fmtTokens(usage.output));
      if(usage.cache_read) parts.push(fmtTokens(usage.cache_read)+' cached');
      /* spent is null when the model has no known rate \u2014 show nothing rather
         than implying the turn was free. */
      if(d.cost&&d.cost.spent!=null){
        let money=fmtCost(d.cost.spent);
        if(d.cost.saved>=0.005) money+=' (saved '+fmtCost(d.cost.saved)+')';
        parts.push(money);
      }
      if(live.turnStart){
        const elapsed=((Date.now()-live.turnStart)/1000).toFixed(1);
        parts.push(elapsed+'s');
      }
      row.innerHTML=parts.map(esc).join('<span class="ds-sep">\u00b7</span>');
      getThread().appendChild(row); scrollDown();
    }
    /* Show token stats in footer */
    const ts=$('#tokenStats');
    if(ts){ let tp=[]; if(usage.input) tp.push('\u2193'+usage.input); if(usage.output) tp.push('\u2191'+usage.output); if(tp.length){ts.textContent=tp.join(' ');ts.classList.remove('hidden');}else{ts.classList.add('hidden');} }
    live.turnStart=null;
  } else if(k==='end'){ finishTurn(); }
  scrollDown();
}
// Clipboard helper that survives a non-secure origin (the gateway is plain
// http on localhost, where navigator.clipboard is available in Chrome but not
// everywhere) — falls back to a hidden textarea + execCommand.
function copyText(text){
  if(navigator.clipboard && window.isSecureContext){
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve,reject)=>{
    try{
      const ta=document.createElement('textarea');
      ta.value=text; ta.setAttribute('readonly','');
      ta.style.cssText='position:fixed;top:-1000px;opacity:0';
      document.body.appendChild(ta); ta.select();
      const ok=document.execCommand('copy');
      document.body.removeChild(ta);
      ok?resolve():reject(new Error('copy rejected'));
    }catch(err){ reject(err); }
  });
}
function flashBtn(btn, done, revert){
  const original=btn.textContent;
  btn.textContent=done; btn.classList.add('did-copy');
  setTimeout(()=>{ btn.textContent=revert||original; btn.classList.remove('did-copy'); },1400);
}
log.addEventListener('click',e=>{
  const btn=e.target.closest('.cb-copy'); if(!btn) return;
  const block=btn.closest('.codeblock');
  // Prefer the stashed raw source: textContent of a highlighted <code> is the
  // same string today, but only because every span is inline — don't rely on it.
  const raw=block.dataset.raw!==undefined ? block.dataset.raw
    : (block.querySelector('pre code').textContent||'');
  copyText(raw).then(()=>flashBtn(btn,'Copied','Copy')).catch(()=>flashBtn(btn,'Failed','Copy'));
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
  window.__jSpeak=true;
  u.onend=()=>{ window.__jSpeak=false; };
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
    document.body.classList.add('listening'); };
  recog.onend=()=>{ recognizing=false; $('#micBtn').classList.remove('listening'); $('#cmdBox').classList.remove('listening');
    document.body.classList.remove('listening'); };
  recog.onerror=()=>{ recognizing=false; $('#micBtn').classList.remove('listening'); $('#cmdBox').classList.remove('listening'); };
  recog.onresult=e=>{
    let txt=''; for(let i=0;i<e.results.length;i++) txt+=e.results[i][0].transcript;
    input.value=txt; autoGrow(); syncComposerState();
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
  openDialog('#confirmModal','#confirmCancel');
}
$('#confirmOk').onclick=()=>{ closeDialog('#confirmModal'); if(_confirmCb) _confirmCb(); _confirmCb=null; };
$('#confirmCancel').onclick=()=>{ closeDialog('#confirmModal'); _confirmCb=null; };
$('#confirmClose').onclick=()=>{ closeDialog('#confirmModal'); _confirmCb=null; };
$('#confirmModal').addEventListener('click',e=>{ if(e.target.id==='confirmModal'){ closeDialog('#confirmModal'); _confirmCb=null; } });

// ---- CONTEXT MENU (⋮) -------------------------------------------------------
let _ctxChatId=null, _projCtxId=null, _ctxMode='chat', _ctxOpener=null;
function positionCtxMenu(menu,event){
  const anchor=event.currentTarget?.getBoundingClientRect();
  const keyboard=!event.clientX&&!event.clientY&&anchor;
  const x=keyboard?anchor.right-24:event.clientX;
  const y=keyboard?anchor.bottom:event.clientY;
  menu.style.left=Math.max(8,Math.min(x,window.innerWidth-170))+'px';
  menu.style.top=Math.max(8,Math.min(y,window.innerHeight-140))+'px';
}
function showCtx(e,chatId){
  _ctxChatId=chatId; _ctxMode='chat';
  if(_ctxOpener&&_ctxOpener!==e.currentTarget) _ctxOpener.setAttribute('aria-expanded','false');
  _ctxOpener=e.currentTarget; _ctxOpener?.setAttribute('aria-expanded','true');
  const m=$('#ctxMenu');
  m.innerHTML='<button class="ctx-item" type="button" role="menuitem" data-action="rename"><svg class="ico sm" aria-hidden="true"><use href="#i-edit"/></svg> Rename</button>'+
    '<button class="ctx-item" type="button" role="menuitem" data-action="project"><svg class="ico sm" aria-hidden="true"><use href="#i-folder"/></svg> Add to project</button>'+
    '<div class="ctx-sep"></div>'+
    '<button class="ctx-item ctx-danger" type="button" role="menuitem" data-action="delete"><svg class="ico sm" aria-hidden="true"><use href="#i-trash"/></svg> Delete</button>';
  m.classList.remove('hidden');
  positionCtxMenu(m,e);
  requestAnimationFrame(()=>m.querySelector('.ctx-item')?.focus());
}
function showNewProjectModal(){
  $('#newProjectInput').value='';
  openDialog('#newProjectModal','#newProjectInput');
}
function closeNewProjectModal(){ closeDialog('#newProjectModal'); }

function showProjectCtx(e,projectId){
  _projCtxId=projectId; _ctxMode='project';
  if(_ctxOpener&&_ctxOpener!==e.currentTarget) _ctxOpener.setAttribute('aria-expanded','false');
  _ctxOpener=e.currentTarget; _ctxOpener?.setAttribute('aria-expanded','true');
  const m=$('#ctxMenu');
  m.innerHTML='<button class="ctx-item" type="button" role="menuitem" data-action="proj-config"><svg class="ico sm" aria-hidden="true"><use href="#i-settings"/></svg> Project settings</button>'+
    '<button class="ctx-item" type="button" role="menuitem" data-action="proj-rename"><svg class="ico sm" aria-hidden="true"><use href="#i-edit"/></svg> Rename</button>'+
    '<button class="ctx-item ctx-danger" type="button" role="menuitem" data-action="proj-delete"><svg class="ico sm" aria-hidden="true"><use href="#i-trash"/></svg> Delete</button>';
  m.classList.remove('hidden');
  positionCtxMenu(m,e);
  requestAnimationFrame(()=>m.querySelector('.ctx-item')?.focus());
}
function hideCtx(restoreFocus=false){
  const opener=_ctxOpener;
  $('#ctxMenu').classList.add('hidden');
  opener?.setAttribute('aria-expanded','false');
  _ctxChatId=null; _projCtxId=null; _ctxOpener=null;
  if(restoreFocus&&opener?.isConnected) opener.focus();
}
document.addEventListener('click',e=>{
  const m=$('#ctxMenu');
  if(m.contains(e.target)){
    e.stopPropagation();
    const item=e.target.closest('[data-action]');
    const action=item&&item.dataset.action; if(!action) return;
    const chatId=_ctxChatId, projId=_projCtxId;
    hideCtx(true);
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
$('#ctxMenu').addEventListener('keydown',e=>{
  const items=[...e.currentTarget.querySelectorAll('.ctx-item')]; if(!items.length) return;
  const at=Math.max(0,items.indexOf(document.activeElement));
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){
    e.preventDefault(); items[(at+(e.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();
  } else if(e.key==='Home'){ e.preventDefault(); items[0].focus(); }
  else if(e.key==='End'){ e.preventDefault(); items[items.length-1].focus(); }
  else if(e.key==='Escape'){ e.preventDefault(); hideCtx(true); }
});
$('#ctxMenu').addEventListener('focusout',e=>{
  if(e.relatedTarget&&!e.currentTarget.contains(e.relatedTarget)) hideCtx();
});

// ---- RENAME MODAL -----------------------------------------------------------
let _renameId=null;
let _renameMode='chat'; // 'chat' or 'project'
function showRename(chatId){
  _renameId=chatId; _renameMode='chat';
  const c=state.chats.find(c=>c.id===chatId);
  $('#renameInput').value=c?c.title:'';
  openDialog('#renameModal','#renameInput');
}
function showRenameProject(projectId){
  _renameId=projectId; _renameMode='project';
  const p=state.projects.find(p=>p.id===projectId);
  $('#renameInput').value=p?p.name:'';
  openDialog('#renameModal','#renameInput');
}
function closeRename(){ closeDialog('#renameModal'); _renameId=null; _renameMode='chat'; }
$('#renameClose').onclick=closeRename;
$('#renameCancel').onclick=closeRename;
$('#renameOk').onclick=async()=>{
  const val=$('#renameInput').value.trim(); if(!val||!_renameId) return;
  if(_renameMode==='project'){
    const r=await api('/api/projects/rename',{id:_renameId,name:val});
    state.projects=r.projects; renderSessions(); closeRename();
  } else {
    const r=await api('/api/chats/rename',{id:_renameId,title:val});
    state.chats=r.chats;
    if(_renameId===state.currentId) setConversationTitle(val);
    renderSessions(); closeRename();
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
      const d=document.createElement('button');
      d.type='button';
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
  openDialog('#projectModal','.proj-item-j, #projectModalNewBtn');
}
function closeProjectPicker(){ closeDialog('#projectModal'); _projPickChatId=null; }
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
  openDialog('#projConfigModal','#projConfigPrompt');
}
function closeProjectConfig(){ closeDialog('#projConfigModal'); _projConfigId=null; }
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
function setConversationTitle(value){
  let title=(value||'New chat').trim()||'New chat';
  if(title.toLowerCase()==='new chat') title='New chat';
  const heading=$('#conversationTitle'); if(heading) heading.textContent=title;
  document.title=title==='New chat'?'Cagentic':title+' · Cagentic';
}
function _highlightSnippet(text, q){
  // esc() first, then wrap matches — never the other way round.
  const safe=esc(text||'');
  if(!q) return safe;
  const needle=esc(q).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  return safe.replace(new RegExp(needle,'gi'), m=>'<mark>'+m+'</mark>');
}
function renderSearchResults(list){
  const q=$('#chatSearch').value.trim();
  const meta=document.createElement('div'); meta.className='search-meta';
  meta.textContent=_searchResults.length
    ? _searchResults.length+' chat'+(_searchResults.length===1?'':'s')+' matching "'+q+'"'
    : 'No chats match "'+q+'"';
  list.appendChild(meta);
  _searchResults.forEach(r=>{
    const el=document.createElement('button'); el.type='button';
    el.className='search-result'+(r.id===state.currentId?' active':'');
    if(r.id===state.currentId) el.setAttribute('aria-current','true');
    el.innerHTML='<div class="sr-title">'+_highlightSnippet(r.title,q)+'</div>'+
      (r.snippet?'<div class="sr-snip">'+_highlightSnippet(r.snippet,q)+'</div>':'');
    el.onclick=()=>{ clearChatSearch(); loadChat(r.id); };
    list.appendChild(el);
  });
}
function wireDisclosure(el,isOpen,label,onToggle){
  el.setAttribute('aria-expanded',isOpen?'true':'false');
  el.setAttribute('aria-label',(isOpen?'Collapse ':'Expand ')+label);
  el.onclick=onToggle;
}
function renderSessions(){
  const list=$('#sessionList'); if(!list) return; list.innerHTML='';
  // Search replaces the tree entirely while active — a filtered tree with
  // half-empty project groups reads worse than a flat ranked list.
  if(_searchResults!==null){ renderSearchResults(list); return; }
  // Build a map of project_id -> chat list
  const projChats={};
  state.projects.forEach(p=>{ projChats[p.id]=state.chats.filter(c=>c.project_id===p.id); });
  const unaffiliated=state.chats.filter(c=>!c.project_id);
  // --- Projects expandable group ---
  const projGrp=document.createElement('div'); projGrp.className='sess-group';
  const projHead=document.createElement('div'); projHead.className='sess-group-head'+(state._openProjectsRoot?' open':'');
  projHead.innerHTML='<button class="sg-disclosure" type="button"><svg class="ico sm sg-caret" aria-hidden="true"><use href="#i-chevron-right"/></svg><span class="sg-dot" style="background:var(--accent)"></span><span class="sg-name">Projects</span><span class="sg-count">'+state.projects.length+'</span></button><button class="sg-add" type="button" title="New project" aria-label="New project"><svg class="ico sm" aria-hidden="true"><use href="#i-plus"/></svg></button>';
  wireDisclosure(projHead.querySelector('.sg-disclosure'),state._openProjectsRoot,'projects',()=>{
    state._openProjectsRoot=!state._openProjectsRoot; renderSessions();
  });
  projHead.querySelector('.sg-add').onclick=e=>{ e.stopPropagation(); showNewProjectModal(); };
  projGrp.appendChild(projHead);
  const projBody=document.createElement('div'); projBody.id='projectGroups';
  projBody.className='sess-group-chats'+(state._openProjectsRoot?' open':'');
  projHead.querySelector('.sg-disclosure').setAttribute('aria-controls',projBody.id);
  if(!state.projects.length){
     projBody.innerHTML='<div class="empty-hint">No projects yet</div>';
  } else {
    state.projects.forEach((p,projectIndex)=>{
      const chats=projChats[p.id]||[];
      const isOpen=state._openProjects.has(p.id);
      const pGrp=document.createElement('div'); pGrp.className='sess-group';
      const pHead=document.createElement('div'); pHead.className='sess-group-head'+(isOpen?' open':'');
      pHead.innerHTML='<button class="sg-disclosure" type="button"><svg class="ico sm sg-caret" aria-hidden="true"><use href="#i-chevron-right"/></svg><span class="sg-dot" style="background:'+esc(p.color)+'"></span><span class="sg-name">'+esc(p.name)+'</span><span class="sg-count">'+chats.length+'</span></button><button class="sg-menu" type="button" title="Project menu" aria-label="Open menu for '+esc(p.name)+'" aria-haspopup="menu" aria-expanded="false"><svg class="ico sm" aria-hidden="true"><use href="#i-more"/></svg></button>';
      wireDisclosure(pHead.querySelector('.sg-disclosure'),isOpen,p.name,()=>{
        if(state._openProjects.has(p.id)) state._openProjects.delete(p.id); else state._openProjects.add(p.id);
        renderSessions();
      });
      pHead.querySelector('.sg-menu').onclick=e=>{ e.stopPropagation(); showProjectCtx(e,p.id); };
      pGrp.appendChild(pHead);
      const pBody=document.createElement('div'); pBody.id='projectChats'+projectIndex;
      pBody.className='sess-group-chats'+(isOpen?' open':'');
      pHead.querySelector('.sg-disclosure').setAttribute('aria-controls',pBody.id);
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
  chatHead.innerHTML='<button class="sg-disclosure" type="button"><svg class="ico sm sg-caret" aria-hidden="true"><use href="#i-chevron-right"/></svg><span class="sg-dot" style="background:var(--text-dim)"></span><span class="sg-name">Chats</span><span class="sg-count">'+unaffiliated.length+'</span></button>';
  wireDisclosure(chatHead.querySelector('.sg-disclosure'),state._openUnaffiliated!==false,'chats',()=>{
    state._openUnaffiliated=state._openUnaffiliated===false?true:false;
    renderSessions();
  });
  chatGrp.appendChild(chatHead);
  const chatBody=document.createElement('div'); chatBody.id='unfiledChats';
  chatBody.className='sess-group-chats'+(state._openUnaffiliated!==false?' open':'');
  chatHead.querySelector('.sg-disclosure').setAttribute('aria-controls',chatBody.id);
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
  item.innerHTML='<button class="ci-title" type="button"'+(c.id===state.currentId?' aria-current="page"':'')+'>'+esc(c.title)+'</button><button class="ci-menu-btn" type="button" title="Chat menu" aria-label="Open menu for '+esc(c.title)+'" aria-haspopup="menu" aria-expanded="false"><svg class="ico sm" aria-hidden="true"><use href="#i-more"/></svg></button>';
  item.querySelector('.ci-title').onclick=()=>loadChat(c.id);
  item.querySelector('.ci-menu-btn').onclick=e=>{ e.stopPropagation(); showCtx(e,c.id); };
  return item;
}
function setCurrent(cur){
  state.currentId=cur.id;
  _userMsgIdx=0;
  setConversationTitle(cur.title);
  const s=$('#jSession'); if(s) s.textContent=(cur.id||'--------').slice(0,8).toUpperCase();
  setOrbLabel(cur.title||'New Chat');
  clearLog();
  if(!cur.messages||!cur.messages.length){ showEmpty(); compactOrb(false); return; }
  compactOrb(true);
  let idx=0;
  _stick=true;
  cur.messages.forEach(m=>{
    let row;
    if(m.role==='user'){ row=addUser(m.content); idx++; }
    else {
      const html=md(stripThink(stripHud(m.content)));
      const hasContent=html&&html.trim();
      if(hasContent){ row=addAssistant(html,m.tools); renderPanels(m.content); idx++; }
      else { (m.tools||[]).forEach(t=>{ const tr=addToolRow({name:t},true); if(tr) tr.style.setProperty('--i',idx++); }); }
    }
    if(row) row.style.setProperty('--i',Math.min(idx-1,5));
  });
  scrollDown(true);
}

// ---- NETWORK ----------------------------------------------------------------
async function api(path,body){
  const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  return r.json();
}
function setModelBadge(m){ const n=$('#msName'); if(n) n.textContent=(m||'').replace(/^(anthropic|openai|ollama):/,''); }
async function boot(){
  const b=await api('/api/bootstrap');
  state.chats=b.chats; state.settings=b.settings; state.projects=b.projects||[];
  state.userName=b.user_name||'';
  setModelBadge(b.model);
  const vs=$('#versionSpan'); if(vs) vs.textContent=b.version||'--';
  renderModelMenu();
  setCurrent(b.current); renderSessions();
}
async function newChat(){
  const r=await api('/api/chats/new',{}); state.chats=r.chats; setCurrent(r.current); renderSessions();
  if(r.current&&r.current.model){state.settings.model=r.current.model;setModelBadge(r.current.model);renderModelMenu();}
  clearViewport(); closeSessions(); input.focus();
}
async function loadChat(id){
  const r=await api('/api/chats/load',{id}); state.chats=r.chats; clearViewport(); setCurrent(r.current); renderSessions(); if(r.current&&r.current.model){state.settings.model=r.current.model;setModelBadge(r.current.model);renderModelMenu();} closeSessions();
}
async function deleteChat(id){
  const r=await api('/api/chats/delete',{id}); state.chats=r.chats; state.projects=r.projects||state.projects; setCurrent(r.current); renderSessions(); closeSessions();
}
async function refreshChats(){
  const b=await api('/api/bootstrap'); state.chats=b.chats; state.projects=b.projects||[];
  renderSessions(); setConversationTitle(b.current.title); setOrbLabel(b.current.title||'New Chat');
}

// ---- MODEL SWITCHER ---------------------------------------------------------
function renderModelMenu(){
  const menu=$('#modelMenu'); const models=state.settings.models||[];
  if(!models.length){ menu.innerHTML='<div class="mm-item">'+esc(state.settings.model||'No models available')+'</div>'; return; }
  menu.innerHTML=models.map(m=>'<button class="mm-item'+(m===state.settings.model?' active':'')+'" type="button" role="menuitemradio" aria-checked="'+(m===state.settings.model?'true':'false')+'" data-m="'+esc(m)+'">'+
    '<span class="mm-tick" aria-hidden="true">'+(m===state.settings.model?'<svg class="ico sm"><use href="#i-check"/></svg>':'')+'</span>'+esc(m)+'</button>').join('');
  menu.querySelectorAll('.mm-item').forEach(it=>{ if(it.dataset.m) it.onclick=()=>switchModel(it.dataset.m); });
}
async function switchModel(m){
  closeModelMenu();
  const r=await api('/api/model',{model:m});
  state.settings.model=r.model; setModelBadge(r.model); renderModelMenu();
  addNote('Model switched to '+r.model);
}
function closeModelMenu(){
  $('#modelMenu').classList.add('hidden');
  $('#modelSwitch').setAttribute('aria-expanded','false');
}
function toggleModelMenu(){
  const menu=$('#modelMenu'), opening=menu.classList.contains('hidden');
  menu.classList.toggle('hidden',!opening);
  $('#modelSwitch').setAttribute('aria-expanded',opening?'true':'false');
  if(opening) requestAnimationFrame(()=>menu.querySelector('.mm-item.active, .mm-item')?.focus());
}
$('#modelSwitch').onclick=e=>{ e.stopPropagation(); toggleModelMenu(); };
$('#modelSwitch').addEventListener('keydown',e=>{
  if(e.target!==e.currentTarget) return;
  if(e.key==='Enter'||e.key===' '||e.key==='ArrowDown'){
    e.preventDefault(); if($('#modelMenu').classList.contains('hidden')) toggleModelMenu();
  }
});
document.addEventListener('click',closeModelMenu);
$('#modelMenu').onclick=e=>e.stopPropagation();
$('#modelMenu').addEventListener('keydown',e=>{
  const items=[...e.currentTarget.querySelectorAll('.mm-item[data-m]')]; if(!items.length) return;
  const at=Math.max(0,items.indexOf(document.activeElement));
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){
    e.preventDefault(); items[(at+(e.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();
  } else if(e.key==='Home'){ e.preventDefault(); items[0].focus(); }
  else if(e.key==='End'){ e.preventDefault(); items[items.length-1].focus(); }
  else if(e.key==='Escape'){ e.preventDefault(); closeModelMenu(); $('#modelSwitch').focus(); }
});

// ---- DRAWER / MODAL ---------------------------------------------------------
const SIDEBAR_MEDIA=window.matchMedia('(min-width: 901px)');
let _sidebarCollapsed=false;
try{ _sidebarCollapsed=localStorage.getItem('cagentic_sidebar_collapsed')==='1'; }catch(e){}
function _saveSidebarState(){
  try{ localStorage.setItem('cagentic_sidebar_collapsed',_sidebarCollapsed?'1':'0'); }catch(e){}
}
function syncSidebarUI(){
  const panel=$('#sessionsPanel'), backdrop=$('#backdrop'), app=$('#app');
  if(!panel||!backdrop||!app) return;
  const desktop=SIDEBAR_MEDIA.matches;
  document.body.classList.toggle('sidebar-collapsed',desktop&&_sidebarCollapsed);
  if(desktop){ panel.classList.remove('open'); backdrop.classList.add('hidden'); }
  const open=desktop?!_sidebarCollapsed:panel.classList.contains('open');
  $('#logsBtn').setAttribute('aria-expanded',open?'true':'false');
  const dialogOpen=!!_visibleDialog();
  const drawerOpen=!desktop&&open;
  panel.inert=dialogOpen||!open;
  app.inert=dialogOpen||drawerOpen;
  $('#windowLayer').inert=dialogOpen||drawerOpen;
  $('#restorePill').inert=dialogOpen||drawerOpen;
  $('#ctxMenu').inert=dialogOpen;
  if(!desktop) backdrop.classList.toggle('hidden',!open);
  requestAnimationFrame(clampHudWindows);
}
function openSessions(){
  if(SIDEBAR_MEDIA.matches){ _sidebarCollapsed=false; _saveSidebarState(); }
  else $('#sessionsPanel').classList.add('open');
  syncSidebarUI();
}
function closeSessions(collapseDesktop=false){
  if(SIDEBAR_MEDIA.matches){
    if(collapseDesktop){ _sidebarCollapsed=true; _saveSidebarState(); }
  } else $('#sessionsPanel').classList.remove('open');
  syncSidebarUI();
}
function toggleSessions(){
  if(SIDEBAR_MEDIA.matches){
    if(_sidebarCollapsed) openSessions(); else closeSessions(true);
  } else if($('#sessionsPanel').classList.contains('open')) closeSessions(); else openSessions();
}
if(SIDEBAR_MEDIA.addEventListener) SIDEBAR_MEDIA.addEventListener('change',syncSidebarUI);
else SIDEBAR_MEDIA.addListener(syncSidebarUI);
window.addEventListener('resize',()=>requestAnimationFrame(clampHudWindows));
function openSettings(){
  let returnFocus=document.activeElement;
  if(!SIDEBAR_MEDIA.matches&&$('#sessionsPanel').contains(returnFocus)) returnFocus=$('#configBtn');
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
  openDialog('#settingsModal','#setModel',returnFocus);
}
function closeSettings(){ closeDialog('#settingsModal'); }
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
  const vb=$('#voiceOutBtn');
  vb.classList.toggle('active', state.voiceOut);
  vb.setAttribute('aria-pressed', state.voiceOut?'true':'false');
  vb.title='Read replies aloud: '+(state.voiceOut?'on':'off');
  if(!state.voiceOut && window.speechSynthesis) speechSynthesis.cancel();
  try{ localStorage.setItem('cagentic_voiceout', state.voiceOut?'1':'0'); }catch(e){}
}

// ---- SEND -------------------------------------------------------------------
function syncComposerState(){
  const ready=_attachments.some(a=>a.path&&!a.error&&!a.pending);
  const uploading=_attachments.some(a=>a.pending);
  sendBtn.disabled=state.busy||uploading||(!input.value.trim()&&!ready);
}
function setBusy(on){
  state.busy=on; input.disabled=on; log.setAttribute('aria-busy',on?'true':'false');
  const bl=$('#busyLabel');
  if(bl){ bl.textContent=_curVerb+'\u2026'; bl.classList.toggle('hidden',!on); }
  $('#stopBtn').classList.toggle('hidden',!on); syncComposerState();
}
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
      if(d.ok && d.action && d.action.type==='retry'){
        clearThinking(); setBusy(false);
        const rows=[...getThread().querySelectorAll('.msg-row.user')];
        const idx=Number(d.action.index), row=rows[idx];
        if(!Number.isInteger(idx)||!row){ addNote('Nothing to retry in this chat.',true); return; }
        truncateAfter(row,false);
        streamEdit(idx,String(d.action.message||''));
        return;
      }
      addNote(d.text||'Done',!d.ok);
    }catch(e){ addNote('Command failed: '+e,true); }
    clearThinking(); setBusy(false);
    return;
  }
  if(log.querySelector('.j-empty')) clearLog();
  addUser(text);
  live={body:null,raw:'',toolRow:null,thinking:null,thinkBox:null,turnStart:null,tokensIn:0,tokensOut:0};
  showThinking(); setOrbLabel(_curVerb+'\u2026'); setBusy(true); compactOrb(true);
  _abortCtrl=new AbortController();
  let res;
  try{ res=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text}),signal:_abortCtrl.signal}); }
  catch(e){ if(e.name==='AbortError'){finishTurn();return;} clearThinking(); addNote('Connection lost. Try again.',true); finishTurn(); return; }
  if(!res||!res.ok||!res.body){ clearThinking(); addNote('Request failed ('+(res?res.status:'no response')+').',true); finishTurn(); return; }
  try{
    await readSSE(res, handle);
  }catch(e){ console.error('Stream read error:',e); }
  clearThinking(); if(state.busy) finishTurn();
}

// ---- ATTACHMENTS ------------------------------------------------------------
// Uploads land inside the workspace and are referenced with @path, which is the
// same mention pipeline the terminal uses — so PDF/DOCX text extraction and
// image-to-vision plumbing come for free instead of being reimplemented here.
const MAX_ATTACH_BYTES=16*1024*1024;
let _attachments=[];
function fmtBytes(n){
  if(n<1024) return n+' B';
  if(n<1024*1024) return (n/1024).toFixed(0)+' KB';
  return (n/(1024*1024)).toFixed(1)+' MB';
}
function _attachIcon(kind){
  if(kind==='image') return '<svg viewBox="0 0 24 24" width="13" height="13"><path fill="none" stroke="currentColor" stroke-width="1.7" d="M3 5h18v14H3zM3 16l5-5 4 4 3-3 6 6"/></svg>';
  if(kind==='document') return '<svg viewBox="0 0 24 24" width="13" height="13"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M14 3H6v18h12V7z M14 3v4h4"/></svg>';
  return '<svg viewBox="0 0 24 24" width="13" height="13"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" d="M9 9h6M9 13h6M9 17h4M6 3h8l4 4v14H6z"/></svg>';
}
function renderAttachments(){
  const bar=$('#attachBar');
  bar.classList.toggle('hidden', !_attachments.length);
  bar.innerHTML='';
  _attachments.forEach(a=>{
    const chip=document.createElement('div');
    chip.className='attach-chip'+(a.pending?' pending':'')+(a.error?' failed':'');
    const thumb=a.thumb?'<img class="ac-thumb" src="'+esc(a.thumb)+'" alt="">':_attachIcon(a.kind);
    chip.innerHTML=thumb+'<span class="ac-name">'+esc(a.name)+'</span>'+
      '<span class="ac-meta">'+(a.error?esc(a.error):(a.pending?'uploading…':fmtBytes(a.size)))+'</span>'+
      '<button class="ac-x" title="Remove" aria-label="Remove attachment"><svg class="ico sm" aria-hidden="true"><use href="#i-close"/></svg></button>';
    chip.querySelector('.ac-x').onclick=()=>{ _attachments=_attachments.filter(x=>x!==a); renderAttachments(); };
    bar.appendChild(chip);
  });
  syncComposerState();
}
function _readAsBase64(file){
  return new Promise((resolve,reject)=>{
    const fr=new FileReader();
    fr.onerror=()=>reject(new Error('read failed'));
    fr.onload=()=>{ const s=String(fr.result||''); resolve(s.slice(s.indexOf(',')+1)); };
    fr.readAsDataURL(file);
  });
}
async function addFiles(files){
  for(const file of Array.from(files||[])){
    if(file.size>MAX_ATTACH_BYTES){
      addNote(file.name+' is larger than '+fmtBytes(MAX_ATTACH_BYTES)+' — not attached.',true);
      continue;
    }
    const entry={name:file.name,size:file.size,kind:'text',pending:true,path:null,thumb:null};
    if(/^image\//.test(file.type)){
      entry.kind='image';
      try{ entry.thumb=URL.createObjectURL(file); }catch(e){}
    }
    _attachments.push(entry); renderAttachments();
    try{
      const data=await _readAsBase64(file);
      const r=await api('/api/upload',{name:file.name,data});
      if(r&&r.ok){ entry.path=r.path; entry.kind=r.kind||entry.kind; entry.size=r.size; entry.pending=false; }
      else { entry.pending=false; entry.error=(r&&r.error)||'upload failed'; }
    }catch(e){ entry.pending=false; entry.error='upload failed'; }
    renderAttachments();
  }
}
// Turn ready attachments into @path mentions appended to the outgoing message.
function consumeAttachments(text){
  const ready=_attachments.filter(a=>a.path&&!a.error);
  if(!ready.length) return text;
  // Quote paths containing spaces so the mention regex takes the whole path.
  const mentions=ready.map(a=>a.path.includes(' ')?'@"'+a.path+'"':'@'+a.path).join(' ');
  _attachments=[]; renderAttachments();
  return text?text+'\n\n'+mentions:mentions;
}
$('#attachBtn').onclick=()=>$('#fileInput').click();
$('#fileInput').addEventListener('change',e=>{ addFiles(e.target.files); e.target.value=''; });
input.addEventListener('paste',e=>{
  const items=[...(e.clipboardData&&e.clipboardData.items||[])];
  const files=items.filter(it=>it.kind==='file').map(it=>it.getAsFile()).filter(Boolean);
  if(files.length){ e.preventDefault(); addFiles(files); }
});
// Drag/drop over the whole page, with a counter so nested dragleave events
// (every child element fires one) don't flicker the overlay off.
let _dragDepth=0;
const dropOverlay=$('#dropOverlay');
window.addEventListener('dragenter',e=>{
  if(!e.dataTransfer||![...e.dataTransfer.types].includes('Files')) return;
  e.preventDefault(); _dragDepth++; dropOverlay.classList.remove('hidden');
});
window.addEventListener('dragover',e=>{ if(e.dataTransfer&&[...e.dataTransfer.types].includes('Files')) e.preventDefault(); });
window.addEventListener('dragleave',e=>{ if(--_dragDepth<=0){ _dragDepth=0; dropOverlay.classList.add('hidden'); } });
window.addEventListener('drop',e=>{
  if(!e.dataTransfer||!e.dataTransfer.files.length) return;
  e.preventDefault(); _dragDepth=0; dropOverlay.classList.add('hidden');
  addFiles(e.dataTransfer.files);
});

// ---- CHAT SEARCH ------------------------------------------------------------
let _searchTimer=null, _searchResults=null;
async function runChatSearch(q){
  if(!q.trim()){ _searchResults=null; renderSessions(); return; }
  try{
    const r=await api('/api/chats/search',{query:q});
    _searchResults=r.results||[];
  }catch(e){ _searchResults=[]; }
  renderSessions();
}
$('#chatSearch').addEventListener('input',e=>{
  const q=e.target.value;
  $('#chatSearchClear').classList.toggle('hidden',!q);
  clearTimeout(_searchTimer);
  _searchTimer=setTimeout(()=>runChatSearch(q),180);
});
$('#chatSearch').addEventListener('keydown',e=>{ if(e.key==='Escape'){ e.stopPropagation(); clearChatSearch(); } });
function clearChatSearch(){
  $('#chatSearch').value=''; $('#chatSearchClear').classList.add('hidden');
  _searchResults=null; renderSessions();
}
$('#chatSearchClear').onclick=clearChatSearch;

// ---- THEME ------------------------------------------------------------------
// Three states so "follow the OS" stays reachable after an explicit choice.
const THEMES=['auto','dark','light'];
const THEME_MEDIA=window.matchMedia('(prefers-color-scheme: dark)');
function syncThemeChrome(t){
  const meta=document.querySelector('meta[name="theme-color"]'); if(!meta) return;
  const dark=t==='dark'||(t==='auto'&&THEME_MEDIA.matches);
  meta.content=dark?'#151118':'#f7f4f9';
}
function applyTheme(t){
  document.documentElement.setAttribute('data-theme',t);
  syncThemeChrome(t);
  const btn=$('#themeBtn');
  if(btn){
    const label=t==='auto'?'Theme: follow system':t==='dark'?'Theme: dark':'Theme: light';
    btn.title=label+' \u2014 click to change'; btn.setAttribute('aria-label',label);
    btn.classList.toggle('active', t!=='auto');
  }
  try{ localStorage.setItem('cagentic_theme',t); }catch(e){}
}
function cycleTheme(){
  const cur=document.documentElement.getAttribute('data-theme')||'auto';
  applyTheme(THEMES[(THEMES.indexOf(cur)+1)%THEMES.length]);
}
(function initTheme(){
  let saved='auto';
  try{ saved=localStorage.getItem('cagentic_theme')||'auto'; }catch(e){}
  applyTheme(THEMES.includes(saved)?saved:'auto');
})();
if(THEME_MEDIA.addEventListener){
  THEME_MEDIA.addEventListener('change',()=>{
    if((document.documentElement.dataset.theme||'auto')==='auto') syncThemeChrome('auto');
  });
}

// ---- COMPOSER + WIRING ------------------------------------------------------
function autoGrow(){ input.style.height='auto'; input.style.height=Math.min(input.scrollHeight,130)+'px'; }
function submit(){
  const typed=input.value.trim();
  const t=consumeAttachments(typed);
  if(!t||state.busy)return;
  input.value=''; autoGrow(); syncComposerState(); send(t);
}
input.addEventListener('input',()=>{ autoGrow(); syncComposerState(); });
input.addEventListener('keydown', e=>{ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submit();} });
sendBtn.onclick=submit;
$('#stopBtn').onclick=abortGeneration;
$('#micBtn').onclick=toggleMic;
$('#logsBtn').onclick=toggleSessions;
$('#newMissionBtn').onclick=newChat;
$('#configBtn').onclick=openSettings;
$('#themeBtn').onclick=cycleTheme;
$('#jumpBtn').onclick=()=>{ scrollDown(true); $('#jumpBtn').classList.add('hidden'); };
$('#voiceOutBtn').onclick=toggleVoiceOut;

$('#closeSessionsBtn').onclick=()=>closeSessions(true);
$('#sidebarNewChatBtn').onclick=newChat;
$('#sidebarSettingsBtn').onclick=openSettings;
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
  // Dialogs own the keyboard while open; global chat shortcuts must not
  // create a conversation or open another surface behind the modal.
  if(_visibleDialog()&&e.key!=='Escape') return;
  if((e.ctrlKey||e.metaKey)&&e.key==='k'){ e.preventDefault(); newChat(); return; }
  if((e.ctrlKey||e.metaKey)&&e.key==='m'){ e.preventDefault(); toggleMic(); return; }
  if((e.ctrlKey||e.metaKey)&&e.key==='s'){ e.preventDefault(); openSettings(); return; }
  if((e.ctrlKey||e.metaKey)&&e.key==='f'){ e.preventDefault(); openSessions(); $('#chatSearch').focus(); return; }
  if(e.key==='Escape'){
    // Stop takes priority: mid-generation, Escape should mean "stop", which is
    // what it means in both competitors.
    if(state.busy){ abortGeneration(); return; }
    if(!$('#confirmModal').classList.contains('hidden')){ closeDialog('#confirmModal'); _confirmCb=null; }
    else if(!$('#newProjectModal').classList.contains('hidden')) closeNewProjectModal();
    else if(!$('#renameModal').classList.contains('hidden')) closeRename();
    else if(!$('#projectModal').classList.contains('hidden')) closeProjectPicker();
    else if(!$('#projConfigModal').classList.contains('hidden')) closeProjectConfig();
    else if(!$('#settingsModal').classList.contains('hidden')) closeSettings();
    else if(!$('#ctxMenu').classList.contains('hidden')) hideCtx(true);
    else if($('#sessionsPanel').classList.contains('open')) closeSessions();
    else closeModelMenu();
  }
});


try{
  state.voiceName=localStorage.getItem('cagentic_voice')||'';
  if(localStorage.getItem('cagentic_voiceout')==='1') toggleVoiceOut();
}catch(e){}

syncSidebarUI();
syncComposerState();
boot();
