/* 2/5 ui.js — load 2nd. DOM plumbing: chat/panel element refs, tech-toggle, buildStatic() (needs STAGES/BRANCHES from translations.js, called immediately), bubble/auntie/attacker/iface/sysLine, agent/stage/state/draft/ledger/sig/outcome/hit/audit cards, the popup() system, fieldReview(), and the canonical/hmac/newDraft signing helpers. */

/* ============================ UI plumbing ============================ */
const $ = id => document.getElementById(id);
const chatEl=$('chat'), chainEl=$('chain'), agentsEl=$('agents'), auditEl=$('audit'), meceEl=$('mece');
let agentCalls=0, lastAct=null;

// Full-screen chat view: hides the technical inspector panels so the chat card
// can be judged on its own, e.g. for screenshots or a "what the customer sees" demo.
const techToggle = $('techToggle');
if(techToggle){
  techToggle.onchange = () => document.body.classList.toggle('tech-hidden', !techToggle.checked);
}

function buildStatic(){
  chainEl.innerHTML = STAGES.map((s,i)=>
    `<div class="stg" id="stg${i}"><span class="no">${String(i).padStart(2,'0')}</span>`+
    `<span class="tx">${s[0]}<div class="ty">${s[1]}</div></span><span class="sd"></span></div>`).join('');
  meceEl.innerHTML = BRANCHES.map(b=>
    `<div class="br" id="br${b[0]}"><div class="bh"><span class="bl">${b[0]}</span><span class="bt">${b[1]}</span></div>`+
    `<div class="nds">${b[2].map(n=>`<span class="nd" id="nd${n}">${n}</span>`).join('')}</div></div>`).join('');
}
buildStatic();

function scroll(){ chatEl.scrollTop = chatEl.scrollHeight; }

function bubble(kind,label,text){
  const d=document.createElement('div');
  d.className='row '+(kind==='auntie'||kind==='attacker'?'me':'them');
  d.innerHTML=`<span class="lab ${label}">${label}</span><div class="bub ${kind}">${text}</div>`;
  chatEl.appendChild(d); scroll(); return d;
}
const auntie   = t => bubble('auntie','AUNTIE',t);
const attacker = t => bubble('attacker','ATTACKER',t);
// The one placeholder for the customer's name, everywhere in the system. The backend's
// messages.py catalogue renders {name} server-side for live turns (so the live path arrives
// already addressed); this fills it for the offline scripted fallback, whose strings are
// hand-written. One convention, one substitution point — and no regex rewriting of the word
// "Auntie", which used to mangle any sentence that legitimately contained it.
const fill     = t => String(t==null?'':t).replace(/\{name\}/g, CUSTOMER_NAME);
const iface    = t => bubble('iface','INTERFACE', fill(t));
const sysLine  = t => bubble('sys','SYSTEM',t);

function hl(o){
  return JSON.stringify(o,null,2)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/"([^"]+)":/g,'<span class="jk">"$1"</span>:')
    .replace(/: "([^"]*)"/g,': <span class="js">"$1"</span>')
    .replace(/: (true|false|null)/g,': <span class="jb">$1</span>')
    .replace(/: (-?\d+\.?\d*)/g,': <span class="jn2">$1</span>');
}
/* ==========================================================================
   AGENT VERDICT CARDS
   Raw JSON is technically complete but tells a judge nothing at a glance. For
   the two agents whose whole job is to reach a yes/no, the panel renders a
   verdict card instead: the decision, the numbers behind it, and the agent's own
   stated reason — with the untouched payload one click away underneath.
   ========================================================================== */
function renderVerdict(name, p){
  const esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const row = (k,v) => `<div class="vr"><span class="vk">${k}</span><span class="vv">${v}</span></div>`;
  const eng = p.engine ? `<span class="veng">${esc(p.engine)}</span>` : '';

  if(name==='GUARDRAILS AGENT'){
    const d    = String(p.decision||'—');
    const dCls = d==='ALLOW' ? 'ok' : (d==='BLOCK'||d==='STOP') ? 'stop' : 'warn';
    const lvl  = String(p.risk_level||'—');
    const lCls = /HIGH|CRIT/.test(lvl) ? 'stop' : /MED/.test(lvl) ? 'warn' : 'ok';
    return `
      <div class="vhead">
        <span class="vbadge ${dCls}">${esc(d)}</span>
        <span class="vtag ${lCls}">RISK ${esc(lvl)}</span>
        ${eng}
      </div>
      ${row('Risk category', `<code>${esc(p.risk_category)}</code>`)}
      ${row('Detected language', esc(p.detected_language))}
      ${row('Gate', `<code>${esc(p.message_key)}</code>`)}
      ${p.developer_reason?`<div class="vwhy">“${esc(p.developer_reason)}”</div>`:''}`;
  }

  if(name==='INTENT CLASSIFIER'){
    const c    = (typeof p.confidence==='number') ? p.confidence : null;
    const thr  = (typeof p.threshold==='number')  ? p.threshold  : 0.75;
    const pct  = c==null ? 0 : Math.round(c*100);
    const pass = c!=null && c>=thr;
    return `
      <div class="vhead">
        <span class="vbadge ${pass?'ok':'warn'}">${esc(p.intent||'—')}</span>
        <span class="vtag ${pass?'ok':'warn'}">${pass?'ABOVE':'BELOW'} THRESHOLD</span>
        ${eng}
      </div>
      <div class="vbar">
        <div class="vbar-t"><span>Confidence</span><b>${pct}%</b></div>
        <div class="vtrack"><i style="width:${pct}%"></i><u style="left:${Math.round(thr*100)}%"></u></div>
        <div class="vthr">threshold ${thr} — below this, AuntieJoy asks you to say it again</div>
      </div>
      ${row('Detected language', esc(p.detected_language))}
      ${p.reason?`<div class="vwhy">“${esc(p.reason)}”</div>`:''}`;
  }

  return null;   // every other agent keeps the plain payload card
}

function agent(name,obj){
  // Agent output is what DBS/engineers see, never what the customer sees (BRD: "the
  // customer only ever sees one warm conversation") — so this goes to the TYPED AGENT
  // CONTRACTS panel only, never into the chat bubble stream.
  agentCalls++; $('acnt').textContent = agentCalls+' call'+(agentCalls>1?'s':'');
  const c=document.createElement('div');
  c.className='acard';
  const verdict = renderVerdict(name, obj || {});
  if(verdict){
    // Verdict on top, the untouched payload one disclosure away underneath — so the
    // readable summary never becomes a substitute for the evidence it came from.
    c.classList.add('verdict');
    c.innerHTML = `<div class="ah">${name}</div>${verdict}`+
      `<details class="raw"><summary>raw payload</summary><pre>${hl(obj)}</pre></details>`;
  }else{
    c.innerHTML=`<div class="ah">${name}</div><pre>${hl(obj)}</pre>`;
  }
  agentsEl.appendChild(c); agentsEl.scrollTop=agentsEl.scrollHeight;
}

/* ==========================================================================
   DBS payment RECEIPT — shown in-chat once a payment has actually executed.
   Deliberately reads like a digibank confirmation, not like a chat bubble: the
   customer should recognise this as DBS, not as "the AI said it worked".

   It only ever SHOWS what the backend already decided. The spoken confirmation
   remains the backend's own reviewed INTERFACE_LINE (messages.py), and every
   value on this card comes from the canonical draft and the execution record —
   the client composes no wording of its own here.
   ========================================================================== */
function receiptCard(o){
  const esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const d=document.createElement('div');
  d.className='row them';
  d.innerHTML=
    `<div class="rcpt">
       <div class="rhead"><img class="dbslogo" src="assets/dbs-logo-white.png" alt="DBS"><span class="rt">PAYMENT RECEIPT</span></div>
       <div class="rok">
         <div class="rtick">✓</div>
         <div class="rstatus">Payment completed</div>
         <div class="rsub">${esc(o.stamp)}</div>
       </div>
       <div class="ramt">${esc(o.amount)}</div>
       <div class="rrows">
         <div class="rr"><span class="k">To</span><span class="v">${esc(o.to)}</span></div>
         <div class="rr"><span class="k">From</span><span class="v">${esc(o.from)}</span></div>
         <div class="rr"><span class="k">Reference</span><span class="v">${esc(o.ref)}</span></div>
         <div class="rr"><span class="k">Status</span><span class="v">Completed</span></div>
       </div>
       <div class="rfoot">Prepared by AuntieJoy · approved by you with a transaction-specific
         biometric · signed and recorded by DBS. Synthetic ledger entry — no real money moved.</div>
     </div>`;
  chatEl.appendChild(d); scroll();
  return d;
}

/* ==========================================================================
   INLINE CLARIFICATION CARD
   A clarification is rendered as a card INSIDE the transcript rather than as a
   full-screen modal, so the conversation stays visible behind it.
   The wording on the card is the backend's own INTERFACE_LINE text, passed in —
   the client invents no sentence of its own here.
   ========================================================================== */
function clarifyCard(text, onRetry){
  const esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const d=document.createElement('div');
  d.className='row them';
  d.innerHTML=
    `<span class="lab IFACE">INTERFACE</span>
     <div class="ccard">
       <span class="ckick">NEEDS CLARIFICATION</span>
       <div class="ctitle">Could you help me with this?</div>
       <div class="ctext">${esc(text)}</div>
       <button class="cbtn" type="button">Let me try again</button>
       <div class="cnote">Nothing has been sent, and no payment has been created yet.</div>
     </div>`;
  chatEl.appendChild(d); scroll();

  // "Let me try again" must only ever re-arm the input — it must never reload the page
  // or re-fire the previous turn. Three separate guards, because a page refresh here
  // would silently throw away the whole conversation:
  //   1. type="button" (set above) so it can never act as an implicit form submit;
  //   2. preventDefault() + stopPropagation() so no ancestor handler can pick it up;
  //   3. a per-card `retried` flag so a double-tap can't queue two turns.
  const b=d.querySelector('.cbtn');
  if(b) b.addEventListener('click', ev=>{
    ev.preventDefault();
    ev.stopPropagation();
    const f=$('fld');
    if(f){ f.focus(); f.classList.add('flash'); setTimeout(()=>f.classList.remove('flash'), 1000); }
    if(typeof onRetry==='function') onRetry();
    if(!d.dataset.retried){
      d.dataset.retried='1';
      d.classList.add('done');
      b.textContent='Try again below, or tap 🎤';
    }
  });
  return d;
}

// Drops the most recent plain interface bubble, so re-presenting that same backend
// line as a clarification card never shows the sentence twice.
function removeLastIfaceBubble(){
  const rows=chatEl.querySelectorAll('.row.them');
  for(let i=rows.length-1;i>=0;i--){
    if(rows[i].querySelector('.bub.iface')){ rows[i].remove(); return true; }
  }
  return false;
}

const stage=(n,st)=>{const e=$('stg'+n); if(e){e.className='stg '+st;}};
const state=v=>$('stState').textContent=v;
const draft=v=>$('stDraft').textContent=v;
const ledger=v=>{const e=$('stLedger');e.textContent=v;e.className='v '+(v==='NOT_INVOKED'?'':'ok');};
const sig=v=>{const e=$('stSig');e.textContent=v;e.className='v '+(v==='NONE'?'':'ok');};
function outcome(v,cls){const e=$('stOut');e.textContent=v;e.className='outbadge '+(cls||'brand');}
function hit(branch,node){
  const b=$('br'+branch); if(b)b.classList.add('hit');
  const n=$('nd'+node); if(n)n.classList.add('hit');
}
function audit(text,cls){
  if(auditEl.querySelector('.placeholder'))auditEl.innerHTML='';
  const t=new Date().toISOString().slice(11,23);
  const d=document.createElement('div'); d.className='ae '+(cls||'');
  d.innerHTML=`<span class="t">${t}</span><span>${text}</span>`;
  auditEl.appendChild(d); auditEl.scrollTop=auditEl.scrollHeight;
}

/* ---------- popup: Auntie appears in every popup ---------- */
let popupResolve=null;
function closePopup(v){
  const r=popupResolve; popupResolve=null;
  $('modal').classList.remove('show'); $('mcard').innerHTML='';
  if(r) r(v||'cancelled');
}
function popup(cfg){
  return new Promise(res=>{
    popupResolve=res;
    const steps=cfg.steps;
    $('mcard').innerHTML=
      `<div class="mtop"><span class="kicker">${cfg.kicker}</span><img src="${MASCOT}" alt="Auntie"></div>`+
      `<div class="mbody">
        <h3>${fill(cfg.title)}</h3>
        <p>${fill(cfg.body)}</p>
        ${cfg.fields?`<div class="mfields">${fill(cfg.fields)}</div>`:''}
        ${steps?`<div class="fstep">${steps.map((s,i)=>
            `<i class="${i<cfg.stepIdx?'done':i===cfg.stepIdx?'on':''}"></i>`).join('')}</div>`:''}
        <div class="macts">${cfg.acts.map(a=>`<button class="${a.kind||''}" data-a="${a.id}">${a.label}</button>`).join('')}</div>
        ${cfg.note?`<div class="mnote">${fill(cfg.note)}</div>`:''}
      </div>`;
    $('modal').classList.add('show');
    const close=v=>{ popupResolve=null; $('modal').classList.remove('show'); $('mcard').innerHTML=''; res(v); };
    $('mcard').querySelectorAll('.macts button').forEach(b=>b.onclick=()=>close(b.dataset.a));
    if(cfg.auto) setTimeout(()=>close(cfg.autoResult||'auto'), cfg.auto);
  });
}

/* ---------- the deterministic field-by-field review (BRD §8.11.2 / A.8) ---------- */
async function fieldReview(lang, d){
  const t=T[lang];
  const F=[
    ['RECIPIENT','Recipient',  t.recipient(d.payee.displayName+' · '+d.payee.masked), 1],
    ['AMOUNT','Amount',        t.amount(amountWord[lang]||amountWord.EN),              2],
    ['SOURCE_ACCOUNT','Source',t.source(ACCOUNT.masked),                               3]
  ];
  iface(t.intro); await sleep(600);
  for(let i=0;i<F.length;i++){
    const f=F[i];
    stage(11,'run');
    const a=await popup({
      kicker:`FIELD ${f[3]} OF 3 · ${f[0]}`, stepIdx:i, steps:F,
      title:f[1],
      body:f[2],
      fields: lang==='ZH'
        ? `<div class="fr"><span class="k">草稿版本</span><span class="v">v${d.version} · ${d.draft_id}</span></div>`
        : `<div class="fr"><span class="k">Draft version</span><span class="v">v${d.version} · ${d.draft_id}</span></div>`,
      acts:[{id:'yes',label:t.yes,kind:'yes'},{id:'no',label:t.no,kind:'no'}],
      note:'One “yes” confirms only the field on this screen. It does not sign, and it does not call the gateway.'
    });
    if(a==='cancelled'){ return {cancelled:true}; }
    if(a==='no'){
      stage(11,'fail'); audit(`FIELD_REJECTED_${f[0]}`,'warn'); outcome('EDIT_REQUESTED','warn');
      return {edit:true,field:f[0]};
    }
    audit(`FIELD_CONFIRMED_${f[0]}`,'ok');
  }
  stage(11,'pass');
  return {edit:false};
}

/* ---------- signing payload (BRD §8.11.4) ---------- */
function canonical(d){
  return {
    draft_id:d.draft_id, draft_version:d.version, confirmation_id:d.confirmation_id,
    transaction_type:'SAVED_PAYEE_TRANSFER', source_account_id:ACCOUNT.account_id,
    beneficiary_id:d.payee.beneficiary_id, destination_token:d.payee.token,
    amount_minor:d.amount_minor, currency:'SGD', reference:d.reference,
    nonce:d.nonce, idempotency_key:d.idempotency_key,
    issued_at:'2026-09-19T07:41:02Z', expires_at:'2026-09-19T07:43:02Z'
  };
}
function hmac(p){ let h=2166136261; const s=JSON.stringify(p);
  for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}
  return 'sig_'+((h>>>0).toString(16)).padStart(8,'0')+((h*31>>>0).toString(16)).padStart(8,'0'); }
const hashOf=p=>'hash_'+(hmac(p).slice(4,16));

/* ---------- fresh draft factory ---------- */
let draftSeq=0;
function newDraft(payee,amountMinor,version,reference){
  draftSeq++;
  return { draft_id:'dr_'+String(draftSeq).padStart(3,'0'), version:version||1,
    payee, amount_minor:amountMinor, currency:'SGD', reference:reference||'Lunch',
    confirmation_id:'cnf_'+String(draftSeq).padStart(3,'0'),
    nonce:'nonce_'+String(draftSeq).padStart(3,'0')+'_a1',
    idempotency_key:'idem_'+String(draftSeq).padStart(3,'0') };
}

/* ---------- script interpreter ---------- */
