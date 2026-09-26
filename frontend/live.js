/* 5/5 live.js — load 5th and LAST. Live free-form /turn + /confirm flow: postJSON, liveFieldReview/liveConfirmFlow/liveFreeTurn, mic/language wiring, and the boot call runScenario('HAPPY') at the very end — must be last since it depends on everything above. */
/* ---------- live free-form input: real /turn + /confirm calls, no more scripting ---------- */
const fld=$('fld'), mic=$('mic');
let FREE_SESSION_ID = null;
const TERMINAL_STATES = new Set(['EXECUTED','REJECTED','FROZEN','COOLING_OFF']);

// Right-column YOU bubble the mic handler draws as words arrive (interim + final).
// liveFreeTurn consumes this on a voice turn so it doesn't echo a second "YOU" copy.
// The mic handler clears it on the no-speech / error paths so it never lingers.
let _pendingYouBubble = null;
// Re-entrancy guard: while a listening session is in flight, ignore extra mic taps.
// Without this a double-tap would start a second SttClient on the same mic.
let _micListening = false;

function guessLang(text){
  if(/[一-鿿]/.test(text)) return 'ZH';
  if(/\b(hantar|kepada|dolar|akaun|puluh|duit|ribu|wang|betul|saja)\b/i.test(text)) return 'BM';
  return 'EN';
}

async function postJSON(path, body){
  const res = await fetch(`${API_BASE}${path}`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
  });
  if(!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
  return res.json();
}

// A live /turn or /confirm response is a flat event list rather than a `turns` wrapper, but it
// is translated by the SAME stepsForEvent()/finalStateSteps() the scenario path uses — defined
// in scenarios.js. Keeping one mapping is what stops the live and scripted demos drifting apart.
function eventsToSteps(events, seenStages){
  const steps=[];
  for(const ev of events) steps.push(...stepsForEvent(ev, seenStages));
  return steps;
}

// The most recent sentence the backend rendered for this turn — its own reviewed
// INTERFACE_LINE wording. Used when re-presenting a clarification as a card, so the
// client never has to write that sentence itself.
function lastInterfaceLine(events){
  for(let i=(events||[]).length-1;i>=0;i--){
    const ev=events[i], p=ev.payload||{};
    if(ev.event_type==='INTERFACE_LINE' && p.text) return p.text;
  }
  return null;
}

// Shows what the intent classifier actually understood, so "is the LLM getting it?" is
// answerable at a glance instead of guessed. Tagged `.ins` so it disappears along with
// the technical panels and never pollutes the clean customer-only view.
function showUnderstood(resp, asrConfidence, viaVoice){
  const ev=(resp.events||[]).slice().reverse().find(e=>e.event_type==='INTENT_RESULT');
  if(!ev) return;
  const p=ev.payload||{};
  const esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const conf=(p.confidence!=null)?Math.round(p.confidence*100)+'%':'—';
  // Voice provenance: the live "Heard…" line is overwritten as soon as a turn resolves,
  // so the clarity reading is repeated here where it survives.
  const asr=(viaVoice && typeof asrConfidence==='number')
    ? `<br><span class="why">heard via voice · clarity ${Math.round(asrConfidence*100)}%`+
      (asrConfidence < 0.85 ? ' — below the 0.85 threshold, so a repeat may be requested' : '')+`</span>`
    : '';
  const d=document.createElement('div');
  d.className='row them ins';
  d.innerHTML=`<span class="lab UNDERSTOOD">UNDERSTOOD</span><div class="bub sys">`+
    `intent <b>${esc(p.intent||'—')}</b> · confidence <b>${conf}</b>`+
    (p.detected_language?` · language <b>${esc(p.detected_language)}</b>`:'')+
    (p.reason?`<br><span class="why">${esc(p.reason)}</span>`:'')+asr+`</div>`;
  chatEl.appendChild(d); scroll();
}

// Local, presentation-only field-by-field walkthrough (BRD §8.11.2 / A.8) using
// the REAL draft the backend just returned. Nothing is sent to the server until
// all three fields get a "yes" — a "no" just stops here and asks Auntie to
// restate that field as a normal free-text/voice turn (same as scenario E2).
async function liveFieldReview(lang, d){
  const t=tt(lang);
  const F=[
    ['RECIPIENT','Recipient', t.recipient(`${d.payee_display} · ${d.payee_masked}`), 1, 'user'],
    ['AMOUNT','Amount',       t.amount(money(d.amount_minor, d.currency)),            2, 'receipt'],
    ['SOURCE_ACCOUNT','Source', t.source(d.account_masked),                           3, 'card']
  ];
  // No spoken intro here: the backend already said the REVIEW_READY line from its catalogue
  // when it opened confirmation, so this walkthrough is popups only.
  for(let i=0;i<F.length;i++){
    const f=F[i];
    stage(11,'run');
    const a=await popup({
      kicker:`FIELD ${f[3]} OF 3 · ${f[0]}`, stepIdx:i, steps:F,
      icon:f[4] || 'shieldCheck',
      title:f[1], body:f[2],
      fields: `<div class="fr"><span class="k">Draft version</span><span class="v">v${d.version} · ${d.draft_id}</span></div>`,
      acts:[{id:'yes',label:t.yes,kind:'yes'},{id:'no',label:t.no,kind:'no'}],
      note:'One “yes” confirms only the field on this screen. It does not sign, and it does not call the gateway.'
    });
    if(a==='cancelled') return {cancelled:true};
    if(a==='no'){
      stage(11,'fail'); audit(`FIELD_REJECTED_${f[0]}`,'warn'); outcome('EDIT_REQUESTED','warn');
      await reportFieldSignal(f[0], 'REJECTED');
      return {edit:true, field:f[0]};
    }
    // Extra confirmation layer: once personalization is active, a single "yes"
    // isn't taken as final — the detail is read back once more before it locks in.
    if(personalizeActive() && t.confirmAgain){
      const again = await popup({
        kicker:`DOUBLE-CHECK · ${f[0]}`, title:f[1],
        body: t.confirmAgain(f[2]),
        acts:[{id:'yes',label:t.yes,kind:'yes'},{id:'no',label:t.no,kind:'no'}],
        note:'Confirming twice, just to be sure. This still hasn’t signed or sent anything.'
      });
      if(again==='cancelled') return {cancelled:true};
      if(again==='no'){
        stage(11,'fail'); audit(`FIELD_REJECTED_${f[0]}_ON_RECHECK`,'warn'); outcome('EDIT_REQUESTED','warn');
        await reportFieldSignal(f[0], 'REJECTED');
        return {edit:true, field:f[0]};
      }
    }
    audit(`FIELD_CONFIRMED_${f[0]}`,'ok');
  }
  stage(11,'pass');
  return {edit:false};
}

// Field review → mock FaceID → real POST /confirm → render whatever the
// backend actually decided (sign, gateway, ledger — or a freeze/reject).
//
// IN-10: one turn can carry several independent instructions. The backend queues them, and
// after each one executes it advances session.draft to the NEXT queued instruction and reads
// that one back itself (event NEXT_INSTRUCTION_QUEUED). So this walkthrough has to run once
// per queued draft. Without the loop the customer is left with no card to act on for
// instruction 2, and the "yes" they type instead travels to /turn as a fresh turn — which the
// backend answers by restarting the review, so the same read-back is heard twice.
async function liveConfirmFlow(session, lang, my){
  const offered = new Set();
  let cur = session;
  while(my===RUN && cur && cur.draft){
    // The backend hands over the next queued instruction by leaving the session in
    // CONFIRMATION_READY with session.draft pointing at a DIFFERENT draft — it has already
    // re-read that draft back itself. Anything else means this flow is done: EXECUTED, a
    // freeze or a reject (the last instruction), or a review re-opened on the SAME draft.
    //
    // Deliberately keyed on the draft's identity and not on a counter: `pending_remaining`
    // counts the instructions queued BEHIND the current one, so it is already 0 while the
    // final draft still needs confirming. Identity is also what makes a stalled queue
    // unable to spin.
    const id = cur.draft.draft_id;
    if(id && offered.has(id)) return;
    if(id) offered.add(id);
    const next = await liveConfirmOnce(cur, lang, my);
    if(my!==RUN || !next) return;
    if(!(next.state==='CONFIRMATION_READY' && next.draft)) return;
    cur = next;
  }
}

// ONE draft's approval walkthrough. Returns the session snapshot to carry on with when the
// backend queued another instruction behind this one, or null when the flow should stop here
// (cancelled, a rejected field, an unreachable backend, or a newer run superseding this one).
async function liveConfirmOnce(session, lang, my){
  const d = session.draft;
  // Present the recognised draft in the transcript first. The approval walkthrough begins
  // only when the customer acts on that card, so the card is a real control rather than
  // decoration. A new run resolves it as 'cancelled' and this flow unwinds.
  const go = await draftCard(d);
  if(my!==RUN || go!=='confirm') return null;
  const r = await liveFieldReview(lang, d);
  if(my!==RUN || r.cancelled) return null;
  // A rejected field already produced the backend's FIELD_EDIT_REQUESTED line via
  // /field_signal, so there is nothing to say here — just wait for the next turn.
  if(r.edit) return null;
  // This lead-in belongs to the simulated FaceID popup below, which is frontend chrome (like
  // the field-review popups), not a conversation line the backend decides.
  iface(tt(lang).ready); await sleep(500);
  if(my!==RUN) return null;
  stage(12,'run');
  const a = await popup({
    kicker:'MOCK FACEID · SIMULATED', title:'Face ID',
    body:'AuntieJoy is asking for a <b>transaction-specific</b> biometric confirmation on the registered device.',
    fields:`<div class="fr"><span class="k">Draft</span><span class="v">${d.draft_id} · v${d.version}</span></div>
            <div class="fr big"><span class="k">Amount</span><span class="v">${money(d.amount_minor, d.currency)}</span></div>`,
    acts:[{id:'ok',label:icon('faceid')+' Confirm with Face ID',kind:'yes'}],
    icon:'faceid',
    note:'Simulated. The prototype does not claim production FaceID integration (BRD §7.4).'
  });
  if(my!==RUN) return null;
  if(a==='cancelled'){ stage(12,''); return null; }
  audit('MOCK_FACEID_SUCCESS (local) · calling /confirm…','ok');
  let resp;
  try{
    resp = await postJSON('/confirm', {
      session_id: FREE_SESSION_ID, fields:['RECIPIENT','AMOUNT','SOURCE_ACCOUNT'],
      language: lang, biometric: true, faceid_result: 'SUCCESS'
    });
  }catch(err){
    audit('CONFIRM_CALL_FAILED · '+err.message,'stop');
    iface('Sorry, I could not reach the payment system just now. Please try again.');
    return null;
  }
  if(my!==RUN) return null;
  syncPersonalization(resp.session);
  const seenStages=new Set();
  const steps = eventsToSteps(resp.events, seenStages).concat(finalStateSteps(resp.session));
  await play(steps, my);
  if(my!==RUN) return null;
  // A real executed payment gets the full DBS receipt treatment, so the confirmation reads
  // as a digibank record rather than as a chat message claiming success. The SPOKEN line is
  // still the backend's own reviewed INTERFACE_LINE (played above) — this card only SHOWS it,
  // and every value on it comes from the canonical draft and the execution record.
  //
  // Keyed off the ledger record THIS confirmation produced rather than off the session's final
  // state: in a multi-instruction turn the backend has already advanced to the next draft by
  // the time this response lands, so the state reads CONFIRMATION_READY even though a payment
  // was just recorded. The event is what actually tells the two apart, and for a single
  // payment it coincides with state==='EXECUTED' exactly as before.
  if(resp.events.some(e => e.event_type === 'LEDGER_RECORDED')){
    const txn = (resp.session.execution && resp.session.execution.transaction_id) || d.draft_id;
    receiptCard({
      stamp: new Date().toLocaleString('en-SG',{day:'2-digit',month:'short',year:'numeric',
              hour:'2-digit',minute:'2-digit'}),
      amount: money(d.amount_minor, d.currency),
      to:     d.payee_display + (d.payee_masked ? ' · ' + d.payee_masked : ''),
      from:   'DBS Savings · ' + (d.account_masked || 'ending 1234'),
      ref:    txn
    });
  }
  return resp.session;
}

// asrConfidence: the REAL confidence the speech engine reported for this utterance, or null
// when it reported none. Passing it through is what makes the backend's ASR ambiguity
// guardrail live — previously every voice turn was sent as a hardcoded 0.93, so a badly
// misheard amount could never trigger a "please repeat that" challenge.
// asrSource travels with it so the backend can always tell a measurement from a stand-in.
async function liveFreeTurn(text, viaVoice, asrConfidence, asrSource){
  stopSpeaking();   // barge-in: a fresh customer turn cuts Auntie off mid-sentence
  const my=++RUN;
  BUSY=true;
  if(!FREE_SESSION_ID){
    FREE_SESSION_ID = 'sess_free_' + Math.random().toString(36).slice(2,10);
    // Panels only. The transcript is deliberately NOT cleared: AuntieJoy's intro card
    // (and anything already said) must stay on screen, or starting a conversation would
    // look identical to the page reloading.
    resetPanels();
    document.querySelectorAll('.sbtn').forEach(b=>b.classList.remove('on'));
    AUTO_PERSONALIZE = false;   // a new conversation starts with a clean read — no carry-over profiling
    updatePersonalizeBadge();
    await sleep(120);
    if(my!==RUN) return;
    audit('LIVE BACKEND · '+API_BASE,'ok');
  }
  const lang = guessLang(text);
  // Clear only the STALE, transient mic status markers ("Listening…" / "Hearing…" /
  // "Stopped listening…") left over from a PREVIOUS mic press. The "Heard you — clarity N%"
  // marker is KEPT: it is the only place the engine's clarity reading survives, and it proves
  // the ears worked. Clarification cards (.ccard) and every other bubble stay untouched.
  chatEl.querySelectorAll('.row.them .bub.iface').forEach(b => {
    if(/^(Listening|Hearing|Stopped listening)/.test(b.textContent || '')) b.closest('.row').remove();
  });
  // Echo the customer's own words into the transcript. BOTH typed and voice turns get a
  // right-column "YOU" echo for symmetry — but a VOICE turn has usually already drawn that
  // bubble word-by-word while the mic streamed interim results, so reuse it instead of
  // creating a second copy. A TYPED turn never reuses one: any bubble still hanging around
  // belongs to an abandoned mic session and must be dropped, or the typed words would go
  // un-echoed and a stale line would be left on screen.
  if(viaVoice && _pendingYouBubble){
    _pendingYouBubble = null;
  } else {
    if(_pendingYouBubble){ try{ _pendingYouBubble.remove(); }catch(_){} _pendingYouBubble = null; }
    customer(text);
  }
  let resp;
  try{
    // A number alone is not evidence. Where it came from travels with it, so the audit trail
    // never mistakes a stand-in for a measurement the engine actually reported.
    const hasConf = (typeof asrConfidence==='number' && asrConfidence>=0 && asrConfidence<=1);
    resp = await postJSON('/turn', {
      session_id: FREE_SESSION_ID, text, via_voice: !!viaVoice,
      asr_confidence: hasConf ? asrConfidence
                              : (viaVoice ? 0.93 : 0.99),   // stand-in, labelled as such below
      asr_confidence_source: hasConf ? (asrSource || 'engine_reported')
                                     : (viaVoice ? 'unreported' : 'typed')
    });
  }catch(err){
    audit('TURN_CALL_FAILED · '+err.message,'stop');
    iface('Sorry, I could not reach the payment system just now. Please try again.');
    BUSY=false; return;
  }
  if(my!==RUN) return;
  const seenStages=new Set();
  const steps = eventsToSteps(resp.events, seenStages).concat(finalStateSteps(resp.session));
  await play(steps, my);
  if(my!==RUN) return;

  const st = resp.session.state;
  syncPersonalization(resp.session);   // the backend's Personalization Agent already decided this turn
  showUnderstood(resp, asrConfidence, viaVoice);   // diagnostic: intent + clarity, hidden in the customer view
  if(st==='CONFIRMATION_READY' && resp.session.draft){
    await liveConfirmFlow(resp.session, lang, my);
  } else {
    if(TERMINAL_STATES.has(st)) FREE_SESSION_ID = null;   // conversation ended — next turn starts fresh
    if(st==='CLARIFICATION_REQUIRED'){
      // The backend already spoke this turn's clarification line, and play() rendered it as
      // a plain bubble. Re-present that SAME backend wording as an in-transcript card — the
      // client writes no sentence of its own — and drop the plain bubble so Auntie never
      // sees the same line twice.
      const said = lastInterfaceLine(resp.events);
      if(said){
        removeLastIfaceBubble();
        clarifyCard(said, ()=>{ const f=$('fld'); if(f) f.focus(); });
      }
    }
  }
  // Every sentence this turn came back as an INTERFACE_LINE event and was played above.
  // The client composes nothing: no clarification wording, no freeze explanation, no receipt.
  if(my!==RUN) return;
  BUSY=false;
}

fld.addEventListener('keydown',e=>{ if(e.key==='Enter'&&fld.value.trim()){const t=fld.value.trim();fld.value='';liveFreeTurn(t,false);} });

// Web Speech API only listens in ONE language at a time — it can't auto-detect,
// so Auntie (or whoever's speaking) picks which one with this toggle first.
let MIC_LANG = 'en-SG';
document.querySelectorAll('#langtog .lt').forEach(b=>{
  b.onclick = () => {
    document.querySelectorAll('#langtog .lt').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
    MIC_LANG = b.dataset.lang;
  };
});

/* Tencent has no single engine that covers English + Chinese + Malay, so the language
   toggle picks the ENGINE, not just a hint: EN and ZH share 16k_zh_en (which auto-detects
   between them), while BM needs 16k_multi_lang. */
function sttLanguage(){
  const l = document.querySelector('#langtog .lt.on')?.dataset.l || 'EN';
  return l === 'BM' ? 'bm' : (l === 'ZH' ? 'zh' : 'en');
}

/* The real ears: stream the mic to the backend, which asks Tencent Cloud ASR.
   Resolves with {text, confidence, confidenceSource}; throws if the engine is unusable. */
async function sttTurn(setYou){
  // Tap-to-talk that waits for the engine to finish. We deliberately do NOT call start()
  // and stop() back-to-back — that gave no listening window, so the engine returned an
  // empty final and the turn was silently dropped. Instead we let the engine's VAD
  // (1 s of silence after speech) finalise on its own, and only cancel on timeout/error.
  // Interim results stream into the customer's own bubble via setYou, word by word.
  let failure = null;
  let lastInterim = '';   // keep the best partial, in case the engine's final comes back empty
  const result = await new Promise((resolve, reject) => {
    let client, settled = false;
    const done = (v) => {
      if(settled) return;
      settled = true;
      if(client){ try { client.cancel(); } catch(_){} }
      resolve(v);
    };
    client = new SttClient({
      onInterim: t => { lastInterim = t; try { setYou(t); } catch(_){} },
      onError:   m => { failure = m; },
      onFinal:   r => done(r)
    });
    client.start(sttLanguage()).then(() => {
      if(failure){ client.cancel(); reject(new Error(failure)); return; }
      // Safety net so the mic can never hang forever if the engine never finalises.
      setTimeout(() => done(null), 30000);
    }, reject);
  });
  if(failure && !result) throw new Error(failure);
  // Never drop a turn the engine clearly heard: if the final is empty (or never came) but
  // interim words did arrive, use those instead of returning nothing. This is exactly the
  // "it caught my words but didn't send" case.
  if((!result || !result.text) && lastInterim){
    return { text: lastInterim,
             confidence: result ? result.confidence : null,
             confidenceSource: result ? result.confidenceSource : 'interim_fallback',
             engine: result ? result.engine : undefined };
  }
  return result;
}

/* The browser's built-in recogniser. Kept as the fallback: it needs no server and no key,
   so the demo still works when the backend or the ASR provider is unavailable. */
function webSpeechTurn(setYou){
  return new Promise(resolve=>{
    const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
    if(!SR){ resolve(null); return; }
    const r = new SR();
    r.lang = MIC_LANG;
    // Partial results let the customer see words streaming into their own bubble (setYou).
    r.interimResults = true;
    r.continuous = false;
    r.maxAlternatives = 1;
    r.onresult = e=>{
      let interim='', final='', conf=null;
      for(let i=e.resultIndex;i<e.results.length;i++){
        const res=e.results[i], alt=res[0];
        if(res.isFinal){ final += alt.transcript; conf = alt.confidence; }
        else interim += alt.transcript;
      }
      if(interim.trim()) setYou(interim.trim());
      if(final.trim()) resolve({ text: final.trim(), confidence: typeof conf==='number' ? conf : null });
    };
    // Errors are SYSTEM messages, so they belong on the left — never in the customer's bubble.
    r.onerror = ev=>{
      iface(icon('alert') + ' ' + ({
        'not-allowed':'Microphone permission was blocked. Please allow it in your browser, or type instead.',
        'service-not-allowed':'The speech service is not allowed here. Please type instead.',
        'audio-capture':'No microphone was found. Please check your device, or type instead.',
        'network':'Speech recognition needs a network connection. Please type instead.',
        'no-speech':'I didn’t hear anything. Tap the microphone once and try again.',
        'aborted':'Listening stopped.'
      }[ev.error] || 'Listening stopped unexpectedly. Please try again, or type instead.'));
      resolve(null);
    };
    r.onend = ()=>{
      resolve(null);
    };
    try{ r.start(); }
    catch(err){ resolve(null); }
  });
}

mic.onclick=async()=>{
  // Re-entrancy guard: a second tap while we are already listening would start a second
  // SttClient on the same microphone and corrupt both streams. Ignore it.
  if(_micListening) return;
  _micListening = true;
  try{ await micCapture(); }
  finally{ _micListening = false; }
};

async function micCapture(){
  stopSpeaking();   // barge-in: pressing the mic immediately silences any playback
  // Clear any stale mic placeholders from a PREVIOUS press (Listening…/Hearing…/Stopped…)
  // so we never end up with a stack of them. The live FreeTurn path also clears these,
  // but that only fires when the engine produced a result — if it didn't (no speech,
  // aborted, blocked by a guardrail), the bubbles would otherwise pile up on every
  // extra press of the mic.
  chatEl.querySelectorAll('.row.them .bub.iface').forEach(b => {
    if(/^(Listening|Hearing|Stopped listening)/.test(b.textContent || '')) b.closest('.row').remove();
  });
  // A previous press may also have left an unsent YOU bubble behind (interim words that
  // never became a turn). Remove it so the customer never sees a ghost line of their own.
  if(_pendingYouBubble){ try{ _pendingYouBubble.remove(); }catch(_){} _pendingYouBubble = null; }
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  const haveStt = (typeof SttClient !== 'undefined') && SttClient.supported();
  if(!haveStt && !SR){
    iface('Voice input is not available in this browser — please type, or run a scripted scenario.');
    return;
  }

  // Pre-flight the microphone explicitly. SpeechRecognition does not always raise the
  // browser's permission prompt, and when it doesn't, clicking the mic silently does
  // nothing at all. Requesting a throwaway audio stream first guarantees the prompt
  // appears and turns any refusal into a specific, readable message.
  if(navigator.mediaDevices && navigator.mediaDevices.getUserMedia){
    try{
      const probe = await navigator.mediaDevices.getUserMedia({ audio:true });
      probe.getTracks().forEach(t=>t.stop());   // we only wanted the permission
    }catch(err){
      iface(icon('alert') + ' ' + ({
        NotAllowedError:'Microphone permission is blocked. Click the padlock/site-settings icon in the address bar, set Microphone to Allow, then try again.',
        PermissionDeniedError:'Microphone permission is blocked. Click the padlock/site-settings icon in the address bar, set Microphone to Allow, then try again.',
        NotFoundError:'No microphone was found on this device. Please type instead.',
        NotReadableError:'The microphone is already in use by another app. Please close it and try again.',
        SecurityError:'The browser blocked microphone access on this page. Please type instead.'
      }[err && err.name] || 'Microphone access failed. Please type instead.'));
      return;
    }
  }

  // The INTERFACE bubble is only a status marker ("we are listening"). The customer's OWN
  // words stream into their own right-column YOU bubble, word by word, via setYou — so the
  // transcript reads like a real voice-first exchange instead of the system quoting them back.
  const live = iface(icon('mic') + 'Listening…');
  // Every status line carries the mic icon; the icon is re-emitted on each update because
  // setting innerHTML replaces the whole bubble content.
  const setLive = t => { const b = live && live.querySelector('.bub'); if(b) b.innerHTML = icon('mic') + fill(t); };
  // Interim → the customer's own bubble. The first partial creates it; every later partial
  // updates it in place, so the words appear one by one exactly as they are spoken.
  const setYou = t => {
    const s = String(t==null?'':t).trim();
    if(!s) return;
    if(!_pendingYouBubble) _pendingYouBubble = customer(s);
    else { const b = _pendingYouBubble.querySelector('.bub.you'); if(b) b.textContent = s; }
  };
  mic.classList.add('on');

  try{
    // Prefer the real engine. Any failure — no config, no server, no key — falls through to
    // the browser's recogniser rather than leaving Auntie with a dead button.
    if(haveStt){
      let r = null;
      try{ r = await sttTurn(setYou); }
      catch(err){ console.warn('[auntiejoy] falling back to the browser recogniser:', err.message); }
      if(r && r.text){
        setYou(r.text);   // settle the customer's bubble on the FINAL text, not the last partial
        const c = (typeof r.confidence==='number') ? r.confidence : null;
        const clarity = c!=null ? ` — clarity ${Math.round(c*100)}%` : ' — clarity not reported';
        // The words now live in the customer's own bubble, so this left-hand marker carries
        // only the reading that has no home there: how clearly the engine heard them.
        setLive(`Heard you${clarity}`);
        // Feed the REAL engine confidence through, so a garbled utterance can be challenged
        // instead of sailing past the ASR ambiguity guardrail.
        liveFreeTurn(r.text, true, c, r.confidenceSource);
        return;
      }
    }

    const spoken = await webSpeechTurn(setYou);
    if(spoken && spoken.text){
      setYou(spoken.text);
      const c = (typeof spoken.confidence==='number') ? spoken.confidence : null;
      setLive(`Heard you${c!=null ? ` — clarity ${Math.round(c*100)}%` : ''}`);
      liveFreeTurn(spoken.text, true, c);
      return;
    }

    // Nothing usable came back: never leave a ghost YOU bubble or a stuck "Listening…" marker.
    if(_pendingYouBubble){ try{ _pendingYouBubble.remove(); }catch(_){} _pendingYouBubble = null; }
    const b = live && live.querySelector('.bub');
    if(b && /Listening…/.test(b.textContent)) b.innerHTML = icon('mic') + 'Stopped listening — nothing was heard.';
  } finally {
    mic.classList.remove('on');
  }
}

/* ---------- boot ---------- */
/* Boot: play the happy path so the first thing anyone sees is the real control chain,
   then it pauses at the field-by-field confirmation popup waiting for Auntie. */
// runScenario('HAPPY');
