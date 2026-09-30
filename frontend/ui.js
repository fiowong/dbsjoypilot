/* 2/5 ui.js, load 2nd. DOM plumbing: chat/panel element refs, tech-toggle, buildStatic() (needs STAGES/BRANCHES from translations.js, called immediately), bubble/auntie/attacker/iface/sysLine, agent/stage/state/draft/ledger/sig/outcome/hit/audit cards, the popup() system, fieldReview(), and the canonical/hmac/newDraft signing helpers. */

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

/* ==========================================================================
   VOICE MODE - a DEMO SWITCH for the "this is not Auntie" escalation

   What it does: when ON, every turn is submitted with a low ASR confidence and an explicit
   SPEAKER_MISMATCH verdict, which is the audio-risk adapter's signal that the registered
   customer could NOT be confirmed from the voice.

   How the real decision is made (unchanged - this switch does not weaken anything):
     stage 1 sees voice_risk=SPEAKER_MISMATCH
       -> result STEP_UP_REQUIRED, session.step_up_required = True
       -> VOICE_STEP_UP_REQUIRED audit event at stage 1
       -> the customer is told identity could not be established from voice alone, and that
          secondary out-of-band confirmation is required before anything proceeds.

   What it deliberately does NOT do: it never sets the verdict to SYNTHETIC. That is a
   different, harsher branch (REJECTED, the transcript never reaches the agents), and
   conflating the two would overstate what the demo is showing. A mismatched but real
   speaker is exactly the step-up case.

   When OFF the behaviour is byte-for-byte what it was: voice_risk is left null and the
   backend's own heuristic applies (assume the registered customer).

   DISCREET BY DESIGN: Voice Mode is a backstage rehearsal switch, so turning it on must NOT
   announce itself anywhere the audience can see. It changes ONLY what the backend is told
   about the speaker - no banner, no chat line, no state readout, no coloured edge.
   ========================================================================== */
const voiceToggle = $('voiceToggle');
// Read at submit time, not cached, so toggling mid-conversation takes effect on the next turn.
function voiceModeOn(){
  return !!(voiceToggle && voiceToggle.checked);
}
if(voiceToggle){
  voiceToggle.onchange = () => {
    // Intentionally silent: no state(), no sysLine(), no body class, no visual change at all.
    // Anything that announced the mode would have to be hidden again before a demo, so there
    // is nothing to hide.
  };
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
  // "me" (right column) = the person holding the phone: the customer, or an attacker who
  // took it. Everything Auntie/the system says lands on the left ("them"). The customer's
  // own words therefore go right, under a "YOU" label, never "AUNTIE".
  d.className='row '+(kind==='auntie'||kind==='attacker'||kind==='you'?'me':'them');
  d.innerHTML=`<span class="lab ${label}">${label}</span><div class="bub ${kind}">${text}</div>`;
  chatEl.appendChild(d); scroll(); return d;
}
const auntie   = t => bubble('auntie','AUNTIE',t);
const attacker = t => bubble('attacker','ATTACKER',t);
// The customer's own spoken/typed words. Echoed in the transcript (right column, "YOU" label)
// so the conversation reads like a real voice-first exchange. Distinct from the AUNTIE bubble:
// the customer is the one whose voice we just heard, not the assistant.
const customer = t => bubble('you','YOU',t);
// The one placeholder for the customer's name, everywhere in the system. The backend's
// messages.py catalogue renders {name} server-side for live turns (so the live path arrives
// already addressed); this fills it for the offline scripted fallback, whose strings are
// hand-written. One convention, one substitution point, and no regex rewriting of the word
// "Auntie", which used to mangle any sentence that legitimately contained it.
const fill     = t => String(t==null?'':t).replace(/\{name\}/g, CUSTOMER_NAME);
const iface    = t => bubble('iface','INTERFACE', fill(t));
const sysLine  = t => bubble('sys','SYSTEM',t);

/* Chrome strings that address the customer by name carry an empty `<span data-nslot></span>`
   which is filled from the TRUSTED session name the moment it arrives, never from anything
   the customer said, and never invented. The intro card is drawn before the first turn, when
   the name is not yet known, so the slot renders as nothing and the sentence still reads
   correctly ("Hello! Tell me who to pay…"). A data attribute, not a class: it is a behaviour
   hook, and section 12 of shell.test.js requires every class to carry a rule in styles.css. */
function refreshNameSlots(root){
  const known = (typeof CUSTOMER_NAME_KNOWN === 'boolean') && CUSTOMER_NAME_KNOWN;
  (root || document).querySelectorAll('[data-nslot]').forEach(el=>{
    el.textContent = known ? ' ' + CUSTOMER_NAME : '';
  });
}

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
   stated reason, with the untouched payload one click away underneath.
   ========================================================================== */
function renderVerdict(name, p){
  const esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const row = (k,v) => `<div class="vr"><span class="vk">${k}</span><span class="vv">${v}</span></div>`;
  const eng = p.engine ? `<span class="veng">${esc(p.engine)}</span>` : '';

  if(name==='GUARDRAILS AGENT'){
    const d    = String(p.decision||'-');
    const dCls = d==='ALLOW' ? 'ok' : (d==='BLOCK'||d==='STOP') ? 'stop' : 'warn';
    const lvl  = String(p.risk_level||'-');
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
        <span class="vbadge ${pass?'ok':'warn'}">${esc(p.intent||'-')}</span>
        <span class="vtag ${pass?'ok':'warn'}">${pass?'ABOVE':'BELOW'} THRESHOLD</span>
        ${eng}
      </div>
      <div class="vbar">
        <div class="vbar-t"><span>Confidence</span><b>${pct}%</b></div>
        <div class="vtrack"><i style="width:${pct}%"></i><u style="left:${Math.round(thr*100)}%"></u></div>
        <div class="vthr">threshold ${thr}: below this, AuntieJoy asks you to say it again</div>
      </div>
      ${row('Detected language', esc(p.detected_language))}
      ${p.reason?`<div class="vwhy">“${esc(p.reason)}”</div>`:''}`;
  }

  return null;   // every other agent keeps the plain payload card
}

function agent(name,obj){
  // Agent output is what DBS/engineers see, never what the customer sees (BRD: "the
  // customer only ever sees one warm conversation"), so this goes to the TYPED AGENT
  // CONTRACTS panel only, never into the chat bubble stream.
  agentCalls++; $('acnt').textContent = agentCalls+' call'+(agentCalls>1?'s':'');
  const c=document.createElement('div');
  c.className='acard';
  const verdict = renderVerdict(name, obj || {});
  if(verdict){
    // Verdict on top, the untouched payload one disclosure away underneath, so the
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
   DBS payment RECEIPT, shown in-chat once a payment has actually executed.
   Deliberately reads like a digibank confirmation, not like a chat bubble: the
   customer should recognise this as DBS, not as "the AI said it worked".

   It only ever SHOWS what the backend already decided. The spoken confirmation
   remains the backend's own reviewed INTERFACE_LINE (messages.py), and every
   value on this card comes from the canonical draft and the execution record,
   the client composes no wording of its own here.

   Its labels are chrome, so they follow the conversation's language: a Mandarin
   payment gets a Mandarin receipt. `lang` comes from session.interface_language,
   the language the backend settled on for this session.
   ========================================================================== */
function receiptCard(o, lang){
  const c = tt(lang || 'EN').chrome.receipt;
  const esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const d=document.createElement('div');
  d.className='row them';
  d.innerHTML=
    `<div class="rcpt">
       <div class="rhead"><img class="dbslogo" src="assets/dbs-logo-white.png" alt="DBS"><span class="rt">${c.title}</span></div>
       <div class="rok">
         <div class="rtick">${icon('check')}</div>
         <div class="rstatus">${c.status}</div>
         <div class="rsub">${esc(o.stamp)}</div>
       </div>
       <div class="ramt">${esc(o.amount)}</div>
       <div class="rrows">
         <div class="rr"><span class="k">${c.to}</span><span class="v">${esc(o.to)}</span></div>
         <div class="rr"><span class="k">${c.from}</span><span class="v">${esc(o.from)}</span></div>
         <div class="rr"><span class="k">${c.reference}</span><span class="v">${esc(o.ref)}</span></div>
         <div class="rr"><span class="k">${c.statusLabel}</span><span class="v">${c.statusDone}</span></div>
       </div>
       <div class="rfoot">${c.foot}</div>
     </div>`;
  chatEl.appendChild(d); scroll();
  return d;
}

/* ==========================================================================
   INLINE DRAFT CARD: "Voice Command Recognized"
   What AuntieJoy THINKS it heard, shown in the transcript before any approval
   starts. Deliberately not a modal: the customer should be able to read it
   against the conversation that produced it, and it stays on screen as the
   record of what was recognised.
   The card renders only what the canonical draft already says; it composes no
   figure of its own. It accepts both draft shapes: the live /turn payload uses
   payee_display / payee_masked / account_masked, while the offline scripted
   fallback carries a payee{} object.

   The labels are chrome, so they follow the conversation's language; `lang`
   defaults to EN for the callers that have none (the offline scripted path and
   the test harness).
   ========================================================================== */
let draftResolve = null;
function closeDraftCard(v){
  const r = draftResolve; draftResolve = null;
  if(r) r(v || 'cancelled');
}
// The account string now passes through as the backend gave it ("ending 1234").
// It used to be rewritten to "•••• 1234" here, which put two different renderings of the SAME
// account on screen at once; the INTERFACE line said "ending 1234" while the draft card said
// "•••• 1234". The backend owns the masked string, so the client renders it verbatim; the
// dashboard's bullet style is a separate display choice, not something to re-derive here.
const maskAcct = s => String(s == null ? '' : s);

function draftCard(d, lang){
  const c = tt(lang || 'EN').chrome.card;
  const esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const payeeName   = d.payee_display || (d.payee && d.payee.displayName) || '-';
  const payeeDetail = d.payee_masked  || (d.payee && d.payee.masked)      || '';
  // The real source-account name comes from the backend (source_account_display), so a Current
  // Account transfer is never mislabelled "DBS Savings". Fall back to the global only if absent.
  const acctName    = d.source_account_display
                      || (typeof ACCOUNT!=='undefined' && ACCOUNT.displayName) || 'DBS Savings';
  const acctDetail  = maskAcct(d.account_masked || d.source_account_id
                      || (typeof ACCOUNT!=='undefined' && ACCOUNT.masked));
  // d.currency travels with the draft, so a non-SGD draft renders US$ rather than silently
  // falling back to S$, the same rule the backend's config.money() applies.
  const amt         = (typeof d.amount_minor === 'number')
    ? money(d.amount_minor, d.currency) : '-';
  const rows =
      `<div class="dr"><span class="k">${c.recipient}</span>
        <span class="v">${esc(payeeName)}${payeeDetail?' · '+esc(payeeDetail):''}</span></div>
       <div class="dr"><span class="k">${c.amount}</span>
        <span class="v amt">${esc(amt)}</span></div>
       <div class="dr"><span class="k">${c.from}</span>
        <span class="v">${esc(acctName)} ${esc(acctDetail)}</span></div>`;

  // If a confirm card for this exact draft is already on screen (e.g. a split-and-confirm merge
  // turn re-enters the review flow for the same draft), update it in place rather than stacking a
  // duplicate. The new review walkthrough owns the button via a fresh draftResolve.
  const id = d.draft_id || '';
  const existing = id ? chatEl.querySelector('.dcard[data-draft="'+id+'"]') : null;
  if(existing){
    existing.querySelector('.drows').innerHTML = rows;
    // Re-rendering the same draft can happen in a DIFFERENT language (a language switch
    // mid-review), so the static chrome is refreshed too. The button is left alone once the
    // customer has pressed it, or its "approval started" state would be overwritten.
    const ttl = existing.querySelector('.dtitle'); if(ttl) ttl.textContent = c.title;
    const tag = existing.querySelector('.dtag');   if(tag) tag.textContent = c.tag;
    const btn = existing.querySelector('.dbtn');   if(btn && !btn.disabled) btn.textContent = c.confirm;
    return new Promise(res=>{ draftResolve = res; });
  }

  return new Promise(res=>{
    draftResolve = res;
    const el=document.createElement('div');
    el.className='row them full';
    el.innerHTML=
      `<div class="dcard" data-draft="${esc(id)}">
         <div class="dhead">
           <span class="dtick">${icon('checkCircle')}</span>
           <span class="dtitle">${c.title}</span>
           <span class="dtag">${c.tag}</span>
         </div>
         <div class="drows">${rows}</div>
         <button class="dbtn" type="button">${c.confirm}</button>
       </div>`;
    chatEl.appendChild(el); scroll();

    // The button is the customer's decision, not decoration: the approval walkthrough
    // only starts here. A new run resolves it as 'cancelled' via closeDraftCard().
    const b=el.querySelector('.dbtn');
    if(b) b.addEventListener('click', ev=>{
      ev.preventDefault(); ev.stopPropagation();
      b.disabled = true;
      b.textContent = c.started;
      closeDraftCard('confirm');
    });
  });
}

/* ==========================================================================
   INLINE CLARIFICATION CARD
   A clarification is rendered as a card INSIDE the transcript rather than as a
   full-screen modal, so the conversation stays visible behind it.
   The wording on the card is the backend's own INTERFACE_LINE text, passed in,
   the client invents no sentence of its own here. The frame AROUND that line
   (kicker, heading, button, note) is chrome, so it follows the conversation's
   language; `lang` defaults to EN for the test harness.
   ========================================================================== */
function clarifyCard(text, onRetry, lang){
  const c = tt(lang || 'EN').chrome.clarify;
  const esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const d=document.createElement('div');
  d.className='row them';
  d.innerHTML=
    `<span class="lab IFACE">INTERFACE</span>
     <div class="ccard">
       <span class="ckick">${c.kicker}</span>
       <div class="ctitle">${c.title}</div>
       <div class="ctext">${esc(text)}</div>
       <button class="cbtn" type="button">${icon('mic')} ${c.retry}</button>
       <div class="cnote">${c.note}</div>
     </div>`;
  chatEl.appendChild(d); scroll();

  // "Let me try again" must only ever re-arm the input; it must never reload the page
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
      b.innerHTML = icon('mic') + ' ' + c.retried;
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

/* ---------- popup: a DBS verification sheet ----------
   The header carries a clean icon badge (cfg.icon, default a shield) instead of a large
   cartoon illustration, so the FaceID prompt reads like a biometric approval and the
   field-by-field review reads like a confirmation sheet. */
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
      `<div class="mtop"><span class="kicker">${cfg.kicker}</span>`+
        `<div class="micon">${icon(cfg.icon||'shieldCheck')}</div></div>`+
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
  const t=T[lang] || T.EN;
  // Popup chrome (kickers, field titles, the draft-version label, the explanatory note) comes
  // from the same per-language `chrome` catalogue the live path uses, so the scripted fallback
  // and a real turn look identical in all three languages.
  const c=t.chrome;
  const F=[
    ['RECIPIENT',c.fieldTitle.recipient, t.recipient(d.payee.displayName+' · '+d.payee.masked), 1, 'user'],
    ['AMOUNT',   c.fieldTitle.amount,    t.amount(amountWord[lang]||amountWord.EN),              2, 'receipt'],
    ['SOURCE_ACCOUNT',c.fieldTitle.source,t.source(ACCOUNT.masked),                              3, 'card']
  ];
  // Show the recognised draft first; the field walkthrough starts when the customer acts
  // on that card. Nothing has been signed at this point either way.
  const go = await draftCard(d, lang);
  if(go!=='confirm') return {cancelled:true};
  iface(t.intro); await sleep(600);
  for(let i=0;i<F.length;i++){
    const f=F[i];
    stage(11,'run');
    const a=await popup({
      kicker:`${c.fieldKicker(f[3])} · ${f[0]}`, stepIdx:i, steps:F,
      icon:f[4] || 'shieldCheck',
      title:f[1],
      body:f[2],
      fields:`<div class="fr"><span class="k">${c.draftVersion}</span><span class="v">v${d.version} · ${d.draft_id}</span></div>`,
      acts:[{id:'yes',label:t.yes,kind:'yes'},{id:'no',label:t.no,kind:'no'}],
      note:c.fieldNote
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
    payee, amount_minor:amountMinor, currency:'SGD', reference:reference||'',
    confirmation_id:'cnf_'+String(draftSeq).padStart(3,'0'),
    nonce:'nonce_'+String(draftSeq).padStart(3,'0')+'_a1',
    idempotency_key:'idem_'+String(draftSeq).padStart(3,'0') };
}

/* ---------- script interpreter ---------- */
