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
const iface    = t => bubble('iface','JOY', fill(t));
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
        <div class="vthr">threshold ${thr}: below this, Joy asks you to say it again</div>
      </div>
      ${row('Detected language', esc(p.detected_language))}
      ${p.reason?`<div class="vwhy">“${esc(p.reason)}”</div>`:''}`;
  }

  return null;   // every other agent keeps the plain payload card
}

// The five bounded agents are always on the table for a customer turn. While the backend
// spends 7-27s screening, classifying, drafting and validating, the panel used to sit idle and
// then dump every card at once when the response landed. agentThinking() paints the five
// specialists as "reviewing…" the moment a turn is sent, so the panel thinks out loud DURING
// the wait (the reframed-latency trust signal), and each card locks in — in place, never
// stacked — when its verdict arrives. Any specialist the backend did not actually consult this
// turn is left marked "not invoked this turn", so the panel never over-states the pipeline.
let agentCards = {};
const AGENT_THINK_ORDER = ['GUARDRAILS AGENT','INTENT CLASSIFIER','PAYMENT AGENT',
                           'INDEPENDENT VALIDATION AGENT','PERSONALIZATION AGENT'];

function agentThinking(){
  agentCards = {};
  agentsEl.innerHTML = '';
  for(const name of AGENT_THINK_ORDER){
    const c = document.createElement('div');
    c.className = 'acard thinking';
    c.innerHTML = `<div class="ah">${name}</div><div class="athink"><span class="pulse"></span>reviewing…</div>`;
    agentsEl.appendChild(c);
    agentCards[name] = c;
  }
  capline('Joy thinks out loud through 5 specialists so the payment is provably safe.');
}

function agentThinkingDone(){
  for(const name in agentCards){
    const c = agentCards[name];
    c.classList.remove('thinking'); c.classList.add('idle');
    const t = c.querySelector('.athink'); if(t) t.textContent = 'not invoked this turn';
  }
  agentCards = {};
}

function agent(name,obj){
  // Agent output is what DBS/engineers see, never what the customer sees (BRD: "the
  // customer only ever sees one warm conversation"), so this goes to the TYPED AGENT
  // CONTRACTS panel only, never into the chat bubble stream.
  agentCalls++; $('acnt').textContent = agentCalls+' call'+(agentCalls>1?'s':'');
  const verdict = renderVerdict(name, obj || {});
  // If a placeholder is waiting for this specialist, resolve it IN PLACE (the lock-in
  // animation) instead of stacking a second card — the panel shows one card per agent.
  const slot = agentCards[name];
  if(slot){
    delete agentCards[name];
    slot.classList.remove('thinking'); slot.classList.add('settled');
    if(verdict){
      slot.classList.add('verdict');
      slot.innerHTML = `<div class="ah">${name}</div>${verdict}`+
        `<details class="raw"><summary>raw payload</summary><pre>${hl(obj)}</pre></details>`;
    }else{
      slot.innerHTML = `<div class="ah">${name}</div><pre>${hl(obj)}</pre>`;
    }
    agentsEl.scrollTop = agentsEl.scrollHeight;
    return;
  }
  const c=document.createElement('div');
  c.className='acard';
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
/* ==========================================================================
   SAVING THE RECEIPT AS AN IMAGE
   A canvas cannot photograph a DOM node without a rendering library, so the
   same numbers are drawn a second time straight onto a canvas, from the very
   same `o` this card was built from. No dependency, no server round trip, and
   nothing leaves the device. It renders at 2x, because a receipt saved at 1x
   comes back as a blurry upscale on a phone screen.
   ========================================================================== */
let _dbsLogoImg = null;
function loadDbsLogo(){
  if(_dbsLogoImg !== null) return Promise.resolve(_dbsLogoImg || null);
  return new Promise(res=>{
    const im = new Image();
    im.onload  = ()=>{ _dbsLogoImg = im;   res(im); };
    im.onerror = ()=>{ _dbsLogoImg = false; res(null); };   // degrade to the wordmark
    im.src = 'assets/dbs-logo-white.png';
  });
}

function roundRectPath(g, x, y, w, h, r){
  g.beginPath();
  if(typeof g.roundRect === 'function'){ g.roundRect(x, y, w, h, r); return; }
  g.moveTo(x+r, y);
  g.arcTo(x+w, y,   x+w, y+h, r); g.arcTo(x+w, y+h, x,   y+h, r);
  g.arcTo(x,   y+h, x,   y,   r); g.arcTo(x,   y,   x+w, y,   r);
  g.closePath();
}

function wrapText(g, text, maxW){
  const out = [];
  for(const word of String(text==null?'':text).split(' ')){
    const last = out[out.length-1];
    const trial = last ? last + ' ' + word : word;
    if(last && g.measureText(trial).width > maxW) out.push(word);
    else if(last) out[out.length-1] = trial;
    else out.push(word);
  }
  return out.length ? out : [''];
}

async function receiptImageBlob(o, lang){
  const c = tt(lang || 'EN').chrome.receipt;
  const logo = await loadDbsLogo();
  const W = 420, S = 2, pad = 10, cardW = W - pad*2, headH = 42, tickR = 25;
  const F = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';
  const rows = [[c.to, o.to], [c.from, o.from], [c.reference, o.ref],
                [c.statusLabel, c.statusDone]];

  // The footnote is the only part whose height is not known up front, and it is measured
  // BEFORE the canvas is sized, because its wrapping decides how tall the card has to be.
  const probe = document.createElement('canvas').getContext('2d');
  probe.font = '400 10px ' + F;
  const footLines = wrapText(probe, c.foot, cardW - 48);
  const footH = 20 + footLines.length * 15;

  let y = pad + headH;
  y += 17; const tickCY = y + tickR; y += tickR*2;
  y += 10; const statusY = y;        y += 18;
  y += 6;  const stampY  = y;        y += 14;
  y += 13; const amtY    = y;        y += 36;
  y += 13; const rowsY   = y;        y += rows.length * 36;
  y += 12; const footY   = y;        y += footH + 14;
  const H = y + pad;

  const cv = document.createElement('canvas');
  cv.width = W * S; cv.height = H * S;
  const g = cv.getContext('2d');
  if(!g) return null;
  g.scale(S, S);

  g.fillStyle = '#F4F5F7'; g.fillRect(0, 0, W, H);
  g.fillStyle = '#FFFFFF'; roundRectPath(g, pad, pad, cardW, H - pad*2, 16); g.fill();
  g.save(); roundRectPath(g, pad, pad, cardW, H - pad*2, 16); g.clip();

  g.fillStyle = '#111111'; g.fillRect(pad, pad, cardW, headH);
  g.fillStyle = '#EC1C24'; g.fillRect(pad, pad + headH - 2, cardW, 2);
  if(logo){
    const lh = 17, lw = lh * (logo.width / Math.max(1, logo.height));
    g.drawImage(logo, pad + 14, pad + (headH - lh)/2, lw, lh);
  } else {
    g.fillStyle = '#FFFFFF'; g.font = '700 15px ' + F;
    g.textAlign = 'left'; g.textBaseline = 'middle';
    g.fillText('DBS', pad + 14, pad + headH/2);
  }
  g.fillStyle = '#FFFFFF'; g.font = '700 10.5px ' + F;
  g.textAlign = 'right'; g.textBaseline = 'middle';
  g.fillText(String(c.title || ''), pad + cardW - 14, pad + headH/2);

  g.textAlign = 'center'; g.textBaseline = 'top';
  g.beginPath(); g.arc(W/2, tickCY, tickR, 0, Math.PI*2);
  g.fillStyle = '#0E9F6E'; g.fill();
  g.strokeStyle = '#FFFFFF'; g.lineWidth = 3; g.lineCap = 'round'; g.lineJoin = 'round';
  g.beginPath();
  g.moveTo(W/2 - 10, tickCY); g.lineTo(W/2 - 3, tickCY + 7); g.lineTo(W/2 + 11, tickCY - 8);
  g.stroke();

  g.fillStyle = '#111111'; g.font = '800 15px ' + F;
  g.fillText(String(c.status || ''), W/2, statusY);
  g.fillStyle = '#71717A'; g.font = '400 11.5px ' + F;
  g.fillText(String(o.stamp || ''), W/2, stampY);
  g.fillStyle = '#111111'; g.font = '800 28px ' + F;
  g.fillText(String(o.amount || ''), W/2, amtY);

  g.font = '400 12.5px ' + F; g.strokeStyle = '#E5E7EB'; g.lineWidth = 1;
  g.beginPath(); g.moveTo(pad + 14, rowsY); g.lineTo(pad + cardW - 14, rowsY); g.stroke();
  rows.forEach((row, i) => {
    const ry = rowsY + i * 36;
    g.fillStyle = '#71717A'; g.textAlign = 'left';  g.textBaseline = 'top';
    g.font = '400 12.5px ' + F;
    g.fillText(String(row[0] || ''), pad + 14, ry + 10);
    g.fillStyle = '#111111'; g.textAlign = 'right'; g.font = '700 12.5px ' + F;
    g.fillText(String(row[1] || ''), pad + cardW - 14, ry + 10);
    g.strokeStyle = '#E5E7EB';
    g.beginPath(); g.moveTo(pad + 14, ry + 36); g.lineTo(pad + cardW - 14, ry + 36); g.stroke();
  });

  g.fillStyle = '#F4F5F7';
  roundRectPath(g, pad + 14, footY, cardW - 28, footH, 11); g.fill();
  g.fillStyle = '#71717A'; g.font = '400 10px ' + F;
  g.textAlign = 'left'; g.textBaseline = 'top';
  footLines.forEach((ln, i) => g.fillText(ln, pad + 24, footY + 10 + i * 15));

  g.restore();
  return new Promise(res => cv.toBlob(b => res(b), 'image/png'));
}

/* Hands the finished PNG to the device. On a phone the share sheet IS the save action -
   it offers Save Image, WhatsApp, Mail and the rest - so it is tried first wherever the
   browser can share files. Everywhere else (desktop, or no file sharing) it falls back to
   a plain download, so the button always does something real. */
async function saveReceiptImage(o, lang){
  const c = tt(lang || 'EN').chrome.receipt;
  const blob = await receiptImageBlob(o, lang);
  if(!blob) return false;
  const name = 'DBS-receipt-'
    + String(o.ref || 'payment').replace(/[^A-Za-z0-9_-]/g, '') + '.png';
  const file = (typeof File === 'function')
    ? new File([blob], name, { type:'image/png' }) : null;
  if(file && navigator.canShare && navigator.canShare({ files:[file] })){
    try{
      await navigator.share({ files:[file], title: c.title || 'Receipt' });
      return true;
    }catch(err){
      // The customer backing out of the share sheet is not a failure worth reporting.
      if(err && err.name === 'AbortError') return false;
      /* anything else falls through to the download */
    }
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(url), 4000);
  return true;
}

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
       <div class="rverify" data-state="pending">Verifying against the append-only audit log…</div>
       <div class="rsave"><button type="button" class="rsbtn">${icon('download')}<span>${c.save}</span></button></div>
     </div>`;
  chatEl.appendChild(d); scroll();
  // A receipt a customer can keep is worth more than one they can only look at. The button
  // renders the same figures already on this card - nothing is re-typed, nothing is uploaded.
  const btn = d.querySelector('.rsbtn');
  if(btn) btn.addEventListener('click', async ()=>{
    if(btn.disabled) return;
    const label = btn.querySelector('span');
    const back  = label ? label.textContent : '';
    btn.disabled = true;
    if(label) label.textContent = c.saving || 'Preparing…';
    try{
      const saved = await saveReceiptImage(o, lang);
      if(label){
        label.textContent = saved ? (c.saved || 'Saved') : back;
        btn.classList.toggle('done', !!saved);
        if(saved) setTimeout(()=>{ label.textContent = back; btn.classList.remove('done'); }, 2500);
      }
    }catch(err){
      if(label) label.textContent = back;
    }finally{
      btn.disabled = false;
    }
  });
  return d;
}

/* ==========================================================================
   INLINE DRAFT CARD: "Ready to confirm"
   What Joy prepared, shown in the transcript before any approval starts. It is
   the customer's choice to begin the approval walkthrough: clicking it proceeds
   to the 3x field-by-field review below. Deliberately not a modal: the customer
   should be able to read it against the conversation that produced it, and it
   stays on screen as the record of what was recognised.
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

/* ==========================================================================
   INLINE FACE ID CARD: "Ready to confirm"
   The chat-level choice to verify with Face ID, shown AFTER the 3x field review
   and BEFORE the biometric prompt. Deliberately not a modal: the customer clicks
   it in the transcript, then the FaceID popup (the actual biometric) opens. The
   two clicks are distinct — "I'm ready to verify" and "here is my Face ID" — so
   the step reads as a real, customer-driven gate rather than an auto-advance.
   It renders only what the draft already says; it composes no figure of its own.
   ========================================================================== */
let faceIdResolve = null;
function closeFaceIdCard(v){
  const r = faceIdResolve; faceIdResolve = null;
  if(r) r(v || 'cancelled');
}
function faceIdCard(d, lang){
  const c = tt(lang || 'EN').chrome.faceidCard;
  const esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const amt = (typeof d.amount_minor === 'number')
    ? money(d.amount_minor, d.currency) : '-';
  const rows =
      `<div class="dr"><span class="k">${c.amount}</span><span class="v amt">${esc(amt)}</span></div>
       <div class="dr"><span class="k">${c.draft}</span><span class="v">${esc(d.draft_id)} · v${esc(d.version)}</span></div>`;
  return new Promise(res=>{
    faceIdResolve = res;
    const el=document.createElement('div');
    el.className='row them full';
    el.innerHTML=
      `<div class="dcard fid-ready" data-draft="${esc(d.draft_id)}">
         <div class="dhead">
           <span class="dtick">${icon('faceid')}</span>
           <span class="dtitle">${c.title}</span>
           <span class="dtag">${c.tag}</span>
         </div>
         <div class="drows">${rows}</div>
         <button class="dbtn" type="button">${c.confirm}</button>
       </div>`;
    chatEl.appendChild(el); scroll();
    // The button is the customer's decision, not decoration: the biometric prompt only
    // opens here. A new run resolves it as 'cancelled' via closeFaceIdCard().
    const b=el.querySelector('.dbtn');
    if(b) b.addEventListener('click', ev=>{
      ev.preventDefault(); ev.stopPropagation();
      b.disabled = true;
      b.textContent = c.started;
      const r=faceIdResolve; faceIdResolve=null; if(r) r('confirm');
    });
  });
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
    `<span class="lab JOY">JOY</span>
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

/* The caption under the control chain. It is PER TURN and re-written from what that turn
   actually did, because a sentence nobody can check is just marketing. It answers the three
   things a judge is really deciding: did a model draft anything, who made the call, and did
   money move (or who stopped it). */
function captionFor(resp){
  const s   = (resp && resp.session) || {};
  const evs = (resp && resp.events) || [];
  const eng = evs.map(e => (e && e.payload) ? e.payload.engine : null).filter(Boolean);
  const live = eng.filter(e => e === 'live').length;
  const mock = eng.filter(e => e === 'mock').length;
  const det  = eng.filter(e => String(e).indexOf('deterministic') !== -1).length;

  let first;
  if(live)      first = `The model drafted${live > 1 ? ` across ${live} agents` : ''}.`;
  else if(det)  first = 'No model was needed, this turn was deterministic by design.';
  else if(mock) first = `The model was unreachable, so ${mock} agent${mock > 1 ? 's' : ''} fell back.`;
  else          first = 'Nothing was drafted on this turn.';

  const stopped = s.frozen || ['BLOCK','FREEZE','SAFETY_INTERVENTION','REJECTED']
                    .indexOf(String(s.outcome || '')) !== -1;
  const third = s.ledger === 'RECORDED' ? 'Nothing moved without your biometric.'
              : stopped                 ? 'Nothing moved, DBS stopped it.'
              :                           'Nothing moved.';
  return `${first} Determinism decided. ${third}`;
}
const capline = v => { const e = $('capline'); if(e) e.textContent = v; };

/* VOICE ENGINE row. Hidden until the real engine actually fails: an indicator that only ever
   appears on a fallback cannot accidentally claim Tencent ASR was used, and stays silent
   entirely when everything went to plan. Once revealed it is never hidden again, so the panel
   cannot quietly un-say that a fallback happened. */
function voiceEngine(text, reveal){
  const row = $('vengRow'), val = $('vengVal');
  if(!row || !val) return;
  val.textContent = text;
  if(reveal) row.hidden = false;
}

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
/* The moving Auntie shown on a card that is waiting on the backend, or null when the card
   on screen has none. Read from the DOM rather than cached, so a caller can always tell
   WHICH popup is currently up before it closes one. */
function popMedia(){ return $('mcard').querySelector('.mmedia'); }

/* Which state Auntie is in for this card. An explicit cfg.gif always wins; otherwise read the
   card itself, so most call sites never have to think about it:
     - the faceid icon       → the biometric prompt
     - a field table         → the field-by-field review
     - an act that says "no" → a refusal or a correction, so she must not be smiling at them
   Returns null when nothing matches, and the stage then falls back to the still portrait. */
function popupGif(cfg){
  if(cfg.gif) return cfg.gif;
  const acts = cfg.acts || [];
  if(cfg.icon === 'faceid')          return STAGE_GIF.faceid;
  if(cfg.fields)                     return STAGE_GIF.confirm;
  if(acts.some(a => a.kind==='dan')) return STAGE_GIF.notallow;
  if(acts.some(a => a.kind==='no'))  return STAGE_GIF.notallow;
  return null;
}

function popup(cfg){
  return new Promise(res=>{
    popupResolve=res;
    const steps=cfg.steps;
    $('mcard').innerHTML=
      `<div class="mtop${cfg.splash?' splash':''}"><span class="kicker">${cfg.kicker}</span>`+
        `<div class="popStage" id="popStage">`+
          `<img class="mmedia" id="mmedia" alt="">`+
          `<img class="mstill" src="${MASCOT_STILL}" alt="Joy">`+
        `</div>`+
      `</div>`+
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
    // Paint the state clip, if this card has one. It fades in only once it has actually
    // decoded, and onerror leaves it hidden, so a missing or slow asset always degrades to
    // the still portrait underneath rather than to an empty box or a broken-image icon.
    const src = popupGif(cfg);
    if(src){
      const media = $('mcard').querySelector('.mmedia');
      media.classList.remove('on');
      media.onload  = () => media.classList.add('on');
      media.onerror = () => media.classList.remove('on');
      media.src = src;
      media.alt = cfg.gifAlt || '';
      $('mcard').querySelector('.popStage').classList.add('on');
    }
    const close=v=>{ popupResolve=null; $('modal').classList.remove('show'); $('mcard').innerHTML=''; res(v); };
    $('mcard').querySelectorAll('.macts button').forEach(b=>b.onclick=()=>close(b.dataset.a));
    // An auto-closing popup must not take down whatever replaced it. The working card closes
    // itself on a timer, and by the time that timer fires a field review or a Face ID prompt
    // can easily be the card on screen. Without this check the late timer would blank the
    // card the customer is actually reading, and resolve the wrong promise.
    if(cfg.auto) setTimeout(()=>{ if(popupResolve===res) close(cfg.autoResult||'auto'); }, cfg.auto);
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
  // Inline "Ready to confirm" card first: the customer's choice to begin the approval
  // walkthrough. Clicking it proceeds to the field-by-field review popups below, where the
  // recognised details are read back. Nothing has been signed at this point either way.
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
  // Inline "Ready to confirm" Face ID card: the customer's choice to verify with FaceID before
  // the biometric prompt (the executeChain popup). Mirrors the live path's confirmation flow.
  const fgo = await faceIdCard(d, lang);
  if(fgo!=='confirm') return {cancelled:true};
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
