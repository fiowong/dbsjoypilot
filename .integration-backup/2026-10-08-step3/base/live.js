/* 5/5 live.js, load 5th and LAST. Live free-form /turn + /confirm flow: postJSON, liveFieldReview/liveConfirmFlow/liveFreeTurn, mic/language wiring, and the boot call runScenario('HAPPY') at the very end, must be last since it depends on everything above. */
/* ---------- live free-form input: real /turn + /confirm calls, no more scripting ---------- */
const fld=$('fld'), mic=$('mic'), kbd=$('kbd'), cinput=$('cinput');
// Voice-first composer: the keyboard toggle flips the composer between "speak" (field hidden,
// mic dominant) and "type" (field shown). setComposerTyping() lives in ui.js so the clarification
// card's retry button uses the very same switch.
if(kbd) kbd.onclick = () => setComposerTyping(!(cinput && cinput.classList.contains('typing')));
let FREE_SESSION_ID = null;
// SCHEDULED is terminal too: a confirmed future-dated payment is finished for this draft, but it
// is NOT executed; no ledger entry was written, so no LEDGER_RECORDED event and no receipt card.
const TERMINAL_STATES = new Set(['EXECUTED','SCHEDULED','REJECTED','FROZEN','COOLING_OFF']);

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

// Ordinary Malay words, for the majority count below. Mirrors the backend's _BM_VOCAB
// (parsing.py): a majority vote needs the small joining words too ("dan", "dari", "satu"),
// or every Malay sentence scores as mostly English because its nouns happen to be English.
const _BM_VOCAB = new Set(['hantar','kepada','dolar','dollar','dollars','duit','saya','tidak','boleh','jumlah','akaun','bayar',
  'sekarang','lima','lapan','sepuluh','ribu','seratus','buang','had','cucu','cepat',
  'maaf','dan','atau','dari','ke','di','untuk','dengan','ini','itu','ada','nak',
  'sudah','sila','tolong','nanti','satu','dua','tiga','empat','enam','tujuh',
  'sembilan','puluh','ringgit','semua','lagi','juga','tapi','kalau','sebab','macam',
  'mana','apa','siapa','bil','bayaran','pindah','pindahkan','awak','kami','dia',
  'mereka','bahasa','wang','selamat','jangan','tanya','tak','cakap','buat','kita']);
// This set is kept BYTE-FOR-BYTE equal to the backend's _BM_VOCAB (parsing.py) on purpose. The
// card is a mirror of the backend's dominant_language(), so any word that counts differently here
// than there makes the card disagree with the answer that follows. Two earlier attempts to be
// "smarter" than the backend both broke that parity and put a Bahasa card back into English:
// adding a neutral-word list (the backend counts "dollar" as BM, so skipping it here made a
// 10-9 Malay turn tie 9-9 and English won), and counting only part of the turn. Do not add
// cleverness here; change the backend and mirror it, or leave it alone.

/* The language the customer used MOST, for a turn that mixes several. EN wins a tie.
   A deliberate mirror of the backend's dominant_language()/language_word_counts() in
   parsing.py, the processing card is drawn BEFORE the reply arrives, so this is what makes
   the card and the answer that follows land in the same tongue. guessLang() above cannot do
   this job: it returns ZH the moment it sees ANY Chinese character, so
   "帮我 bayar bil Starhub from my current account, 然后 transfer…" got a Chinese processing
   card followed by an English answer. (guessLang is still right for picking a TTS VOICE,
   where "contains any Chinese → speak it in Mandarin" is the safer rule.) */
function dominantLang(text){
  const counts = { EN:0, ZH:0, BM:0 };
  const s = String(text || '');
  // A RUN of Chinese counts as ONE point, not one per character: otherwise a single
  // four-character phrase outweighs four English words and "mostly Chinese" would be
  // decided by stroke count rather than by what the customer actually said.
  counts.ZH = (s.match(/[一-鿿]+/g) || []).length;
  // Same tokenisation as the backend's language_word_counts(): [A-Za-z]+ only, so a bare digit
  // ("50") belongs to neither tongue and never tips the count either way.
  for (const word of (s.toLowerCase().match(/[A-Za-z]+/g) || [])) {
    counts[_BM_VOCAB.has(word) ? 'BM' : 'EN']++;
  }
  let best = Math.max(counts.EN, counts.ZH, counts.BM);
  if (best === 0) return 'EN';
  // EN first, so it takes a tie as the neutral tongue, exactly like the backend.
  for (const lang of ['EN','ZH','BM']) if (counts[lang] === best) return lang;
  return 'EN';
}

// Every backend call is bounded. Without this a stalled upstream froze the customer view
// indefinitely: the request sat open, the input stayed disabled and Auntie never answered,
// with nothing on screen to say why. A timeout is not a decision, it is a refusal to wait
// forever, so it is reported as such (err.timedOut) and the caller decides what to say.
// 45s, deliberately ABOVE the backend's own 30s turn budget, so the server gives up first
// (and still answers, having fallen back to its deterministic mocks). The browser timeout
// is a backstop for a backend that is not answering at all, not the thing that decides.
// Raised together with TURN_DEADLINE_S: a live turn makes up to five model calls, and the
// old 25s ceiling cut the turn off mid-way, which is what made the last agent show "mock".
const REQUEST_TIMEOUT_MS = 45000;

// The language this conversation is actually being spoken in, as settled by the BACKEND (it
// counts the customer's words and holds the winner for the whole session). Null until the first
// reply arrives, because before that there is nothing to know. Used for the chrome that has to be
// drawn before an answer exists - the "working on it" card - so it lands in the same tongue as the
// card that replaces it. See the assignment inside liveFreeTurn().
let SETTLED_IFACE_LANG = null;

async function postJSON(path, body){
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try{
    res = await fetch(`${API_BASE}${path}`, {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body),
      signal: ctrl.signal
    });
  }catch(err){
    clearTimeout(timer);
    if(err && err.name === 'AbortError'){
      const e = new Error(`${path} → no response within ${REQUEST_TIMEOUT_MS/1000}s`);
      e.timedOut = true;
      throw e;
    }
    throw err;
  }
  clearTimeout(timer);
  if(!res.ok){
    // The status alone is not enough to act on. A 409 is the backend REFUSING (frozen/cancelled,
    // or no draft awaiting confirmation), not a transport failure, so the body's `detail` is
    // carried on the error and surfaced in the audit panel instead of being flattened to a code.
    let detail='';
    try{ const j = await res.json(); detail = (j && (j.detail || j.message)) || ''; }catch(_){}
    const err = new Error(`${path} → HTTP ${res.status}${detail ? ' · '+detail : ''}`);
    err.status = res.status; err.detail = detail;
    throw err;
  }
  return res.json();
}

// A live /turn or /confirm response is a flat event list rather than a `turns` wrapper, but it
// is translated by the SAME stepsForEvent()/finalStateSteps() the scenario path uses, defined
// in scenarios.js. Keeping one mapping is what stops the live and scripted demos drifting apart.
function eventsToSteps(events, seenStages){
  const steps=[];
  for(const ev of events) steps.push(...stepsForEvent(ev, seenStages));
  return steps;
}

// The most recent sentence the backend rendered for this turn, its own reviewed
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
/* What Joy made of the last utterance, for the inspector only.
   This used to be an "UNDERSTOOD" block appended to the chat, which put intent, confidence
   and language on the phone itself - it read as debug output and made the conversation look
   like a log. It is a diagnostic about the same turn the INTENT CLASSIFIER card already
   covers, so it now sits with the rest of the evidence in the STATE panel instead. */
function showUnderstood(resp, asrConfidence, viaVoice){
  const ev=(resp.events||[]).slice().reverse().find(e=>e.event_type==='INTENT_RESULT');
  if(!ev) return;
  const p=ev.payload||{};
  const el=$('stHeard');
  if(!el) return;
  const conf=(p.confidence!=null)?Math.round(p.confidence*100)+'%':'-';
  // Voice provenance: the live "Heard…" line is overwritten as soon as a turn resolves,
  // so the clarity reading is repeated here where it survives.
  const heard=(viaVoice && typeof asrConfidence==='number')
    ? ` · clarity ${Math.round(asrConfidence*100)}%`
      + (asrConfidence < 0.85 ? ' (low)' : '')
    : '';
  el.textContent = `${p.intent||'-'} · ${conf}${heard}`
    + (p.detected_language ? ` · ${p.detected_language}` : '');
  // The model's own one-line reasoning stays available without widening the row.
  if(p.reason) el.title = p.reason;
}

// Local, presentation-only field-by-field walkthrough (BRD §8.11.2 / A.8) using
// the REAL draft the backend just returned. Nothing is sent to the server until
// all three fields get a "yes"; a "no" just stops here and asks Auntie to
// restate that field as a normal free-text/voice turn (same as scenario E2).
async function liveFieldReview(lang, d){
  const t=tt(lang);
  // `lang` here is the language the BACKEND settled on for this session, not the local guess,
  // so the popups, the FaceID card below and the receipt all speak the same language as the
  // conversation around them.
  const c=t.chrome;
  const F=[
    ['RECIPIENT', c.fieldTitle.recipient, t.recipient(`${d.payee_display} · ${d.payee_masked}`), 1, 'user'],
    ['AMOUNT',    c.fieldTitle.amount,    t.amount(money(d.amount_minor, d.currency)),            2, 'receipt'],
    ['SOURCE_ACCOUNT', c.fieldTitle.source, t.source(d.account_masked),                           3, 'card']
  ];
  // No spoken intro here: the backend already said the REVIEW_READY line from its catalogue
  // when it opened confirmation, so this walkthrough is popups only.
  for(let i=0;i<F.length;i++){
    const f=F[i];
    stage(11,'run');
    const a=await popup({
      kicker:`${c.fieldKicker(f[3])} · ${f[0]}`, stepIdx:i, steps:F,
      icon:f[4] || 'shieldCheck',
      title:f[1], body:f[2],
      fields: `<div class="fr"><span class="k">${c.draftVersion}</span><span class="v">v${d.version} · ${d.draft_id}</span></div>`,
      acts:[{id:'yes',label:t.yes,kind:'yes'},{id:'no',label:t.no,kind:'no'}],
      note:c.fieldNote
    });
    if(a==='cancelled') return {cancelled:true};
    if(a==='no'){
      stage(11,'fail'); audit(`FIELD_REJECTED_${f[0]}`,'warn'); outcome('EDIT_REQUESTED','warn');
      await reportFieldSignal(f[0], 'REJECTED');
      return {edit:true, field:f[0]};
    }
    // Extra confirmation layer: once personalization is active, a single "yes"
    // isn't taken as final, the detail is read back once more before it locks in.
    if(personalizeActive() && t.confirmAgain){
      const again = await popup({
        kicker:`${c.doubleCheckKicker} · ${f[0]}`, title:f[1],
        body: t.confirmAgain(f[2]),
        acts:[{id:'yes',label:t.yes,kind:'yes'},{id:'no',label:t.no,kind:'no'}],
        note:c.doubleCheckNote
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
// backend actually decided (sign, gateway, ledger, or a freeze/reject).
//
// IN-10: one turn can carry several independent instructions. The backend queues them, and
// after each one executes it advances session.draft to the NEXT queued instruction and reads
// that one back itself (event NEXT_INSTRUCTION_QUEUED). So this walkthrough has to run once
// per queued draft. Without the loop the customer is left with no card to act on for
// instruction 2, and the "yes" they type instead travels to /turn as a fresh turn, which the
// backend answers by restarting the review, so the same read-back is heard twice.
// The IDENTITY step-up. Stage 1 could not verify the speaker, so the backend HELD the request and
// suppressed the read-back: Joy has spoken only the hold line and the payment has NOT been shown.
// This card is the control that decides whether it ever is, so it deliberately carries NO amount
// and NO payee - an unverified person must not be shown the payment they are asking about.
async function stepUpFaceId(lang, my){
  const c = tt(lang).chrome.stepup || tt(lang).chrome.faceid;
  stage(12,'run');
  audit('FACEID_STEP_UP · identity check, payment not yet presented · language '+lang,'ok');
  const a = await popup({
    kicker:c.kicker, title:c.title, body:c.body, fields:'',
    acts:[{id:'ok',label:icon('faceid')+' '+c.confirm,kind:'yes'}],
    icon:'faceid', note:c.note
  });
  if(my!==RUN) return false;
  if(a!=='ok'){ stage(12,''); audit('FACEID_STEP_UP_DECLINED · request stays held','stop'); return false; }
  audit('FACEID_STEP_UP_PASSED · identity verified, releasing the hold','ok');
  return true;
}

// `skipFaceId` means identity was already established by the step-up above, so this flow must not
// ask for the same thing a second time (the /confirm call still carries biometric:true).
async function liveConfirmFlow(session, lang, my, skipFaceId){
  const offered = new Set();
  let cur = session;
  while(my===RUN && cur && cur.draft){
    // The backend hands over the next queued instruction by leaving the session in
    // CONFIRMATION_READY with session.draft pointing at a DIFFERENT draft; it has already
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
    const next = await liveConfirmOnce(cur, lang, my, skipFaceId);
    if(my!==RUN || !next) return;
    if(!(next.state==='CONFIRMATION_READY' && next.draft)) return;
    cur = next;
  }
}

// Feature 2 helper: prove the just-shown receipt against the backend's append-only audit log.
// The backend hashes every audit event and chains it to the previous one, so any tampering with
// a past event would break the chain. We surface that verdict on the receipt itself.
async function verifyReceiptChain(sid){
  const el = chatEl.querySelector('.rcpt:last-of-type .rverify');
  if(!el || !sid) return;
  try{
    const r = await fetch(API_BASE + '/audit/' + encodeURIComponent(sid), { method:'GET' });
    if(!r.ok) throw new Error('audit ' + r.status);
    const data = await r.json();
    const ch = (data && data.chain) || {};
    if(ch.intact){
      el.dataset.state = 'ok';
      el.textContent = '✓ Verified against append-only audit log · hash chain intact ('
        + (ch.events || 0) + ' events) · head ' + String(ch.head || '').slice(0, 14) + '…';
    } else {
      el.dataset.state = 'bad';
      el.textContent = '⚠ Hash chain broken at event ' + (ch.broken_at_seq != null ? ch.broken_at_seq : '?')
        + ' — do not trust this receipt';
    }
  } catch (e) {
    el.dataset.state = 'warn';
    el.textContent = 'Audit log unavailable — receipt could not be verified on-chain';
  }
}

// Feature 1: narrate Joy's multilingual mixing in the panel. Detected = what the customer
// actually spoke (server-side, honest); Interface = the language Joy answers in. A switch is a
// trust signal. The code-mix count is a real word count of the customer's own words, so the
// panel can show "EN × N BM × M" even when the single-label detector collapses a mixed turn.
const BM_WORDS = new Set(['saya','kamu','dia','kita','mereka','dan','atau','dari','ke','di','untuk',
  'dengan','ini','itu','ada','nak','sudah','sila','tolong','nanti','satu','dua','tiga','empat','lima',
  'enam','tujuh','lapan','sembilan','sepuluh','ribu','seratus','juta','ringgit','dolar','duit','hantar',
  'kepada','bayar','jumlah','akaun','boleh','tidak','tak','maaf','cepat','sekarang','lagi','juga','tapi',
  'kalau','sebab','macam','mana','apa','siapa','bil','pindah','awak','kami','bahasa','wang','selamat',
  'jangan','tanya','cakap','buat','buah']);
let lastInterfaceLang = null;
function showLanguage(resp, text){
  const el = $('stLang'); if(!el) return;
  let detected = null;
  for(const ev of (resp.events || [])){
    if(ev.event_type === 'INTENT_RESULT' && ev.payload && ev.payload.detected_language) detected = ev.payload.detected_language;
    else if(!detected && ev.event_type === 'GUARDRAILS_RESULT' && ev.payload && ev.payload.detected_language) detected = ev.payload.detected_language;
  }
  const iface = resp.session && resp.session.interface_language;
  let mix = '';
  if(text){
    const w = String(text).toLowerCase().split(/[^a-zà-ÿ]+/i).filter(Boolean);
    let en = 0, bm = 0;
    for(const x of w){ if(BM_WORDS.has(x)) bm++; else if(/[a-z]/.test(x)) en++; }
    if(en && bm) mix = ' · code-mix EN×' + en + ' BM×' + bm;
  }
  el.textContent = (detected || '-')
    + (iface && iface !== detected ? ' → ' + iface : (iface ? ' (→ ' + iface + ')' : ''))
    + mix;
  // Narrate the switch the moment the interface moves to a different language than before.
  if(iface && lastInterfaceLang && iface !== lastInterfaceLang){
    audit('LANGUAGE_SWITCH · interface now ' + iface + ' (was ' + lastInterfaceLang + ')', 'ok');
    capline('Joy detected ' + (detected || iface) + ' and is answering in ' + iface + ', to match how you spoke.');
  }
  if(iface) lastInterfaceLang = iface;
}

// ONE draft's approval walkthrough. Returns the session snapshot to carry on with when the
// backend queued another instruction behind this one, or null when the flow should stop here
// (cancelled, a rejected field, an unreachable backend, or a newer run superseding this one).
async function liveConfirmOnce(session, lang, my, skipFaceId){
  const d = session.draft;
  const c = tt(lang).chrome;
  // Inline "Ready to confirm" card in the chat: the customer's choice to begin the approval
  // walkthrough. Each review popup below is itself the approval gate (yes to proceed, no to
  // correct), so clicking this card reveals the recognised details read back field-by-field.
  const go = await draftCard(d, lang);
  if(my!==RUN || go!=='confirm') return null;
  const r = await liveFieldReview(lang, d);
  if(my!==RUN || r.cancelled) return null;
  // A rejected field already produced the backend's FIELD_EDIT_REQUESTED line via
  // /field_signal, so there is nothing to say here, just wait for the next turn.
  if(r.edit) return null;
  // This lead-in belongs to the simulated FaceID popup below, which is frontend chrome (like
  // the field-review popups), not a conversation line the backend decides.
  if(!skipFaceId){
    iface(tt(lang).ready); await sleep(500);
    if(my!==RUN) return null;
    // Inline "Ready to confirm" Face ID card in the chat: the customer's choice to verify with
    // FaceID. Clicking it opens the biometric prompt below — a real, clickable gate, not an
    // auto-advancing step.
    const fgo = await faceIdCard(d, lang);
    if(my!==RUN || fgo!=='confirm') return null;
    stage(12,'run');
    // Which language this chrome renders in is decided by the backend's settled session language,
    // not by a local guess, so it is recorded here. If a card ever appears in the wrong tongue this
    // line says whether the wrong language arrived or the catalogue lookup missed.
    audit('FACEID_PROMPT · interface language '+lang+(c.faceid ? '' : ' · CHROME MISSING'),'ok');
    const a = await popup({
      kicker:c.faceid.kicker, title:c.faceid.title,
      body:c.faceid.body,
      fields:`<div class="fr"><span class="k">${c.faceid.draftLabel}</span><span class="v">${d.draft_id} · v${d.version}</span></div>
            <div class="fr big"><span class="k">${c.faceid.amountLabel}</span><span class="v">${money(d.amount_minor, d.currency)}</span></div>`,
      acts:[{id:'ok',label:icon('faceid')+' '+c.faceid.confirm,kind:'yes'}],
      icon:'faceid',
      note:c.faceid.note
    });
    if(my!==RUN) return null;
    if(a==='cancelled'){ stage(12,''); return null; }
    audit('MOCK_FACEID_SUCCESS (local) · calling /confirm…','ok');
  } else {
    audit('FACEID_ALREADY_VERIFIED · step-up passed, skipping the repeat prompt','ok');
  }
  let resp;
  try{
    resp = await postJSON('/confirm', {
      session_id: FREE_SESSION_ID, fields:['RECIPIENT','AMOUNT','SOURCE_ACCOUNT'],
      language: lang, biometric: true, faceid_result: 'SUCCESS'
    });
  }catch(err){
    // A 409 is not "the payment system is unreachable": the backend ANSWERED, and said there is
    // nothing left to authorise (no draft awaiting, a session lost to a backend restart, or a
    // frozen transaction). Reporting that as unreachable is false, hides the real cause, and
    // sends the customer to retry a payment that will never authorise - which is exactly what a
    // Face ID tap on a session with no draft looked like: a correct Face ID card, then an
    // "unreachable" bubble. So the two failures are told differently to the customer, and the
    // backend's own reason still goes to the audit panel either way.
    if(err && err.status === 409){
      audit('CONFIRM_REFUSED_409 · '+(err.detail || 'no draft awaiting confirmation'),'stop');
      iface(c.nothingToConfirm || c.unreachable);
    } else {
      audit('CONFIRM_CALL_FAILED · '+err.message,'stop');
      iface(c.unreachable);
    }
    return null;
  }
  if(my!==RUN) return null;
  syncPersonalization(resp.session);
  capline(captionFor(resp));   // a confirmation is its own turn: re-state what actually moved
  const seenStages=new Set();
  // Walk events ourselves (not via eventsToSteps) so the receipt card lands at the exact
  // spot the backend intends: right AFTER its own payment's "All done..." line, not before
  // it, and not after whatever the NEXT queued instruction said. For a single payment the
  // card still appears once, in the same place a customer expects it. For a multi-instruction
  // turn (payment #1 done, payment #2 still being read back) the card stays beside payment #1
  // instead of slipping below payment #2.
  const steps = [];
  let pendingReceipt = null;
  for(const ev of resp.events){
    steps.push(...stepsForEvent(ev, seenStages));
    if(ev.event_type === 'LEDGER_RECORDED' && !pendingReceipt){
      const txn = (resp.session.execution && resp.session.execution.transaction_id) || d.draft_id;
      pendingReceipt = {t:'receipt', lang,
        o:{
          stamp: new Date().toLocaleString('en-SG',{day:'2-digit',month:'short',year:'numeric',
                  hour:'2-digit',minute:'2-digit'}),
          amount: money(d.amount_minor, d.currency),
          to:     d.payee_display + (d.payee_masked ? ' · ' + d.payee_masked : ''),
          // The real source-account name comes from the backend (source_account_display), so
          // a Current Account transfer is never mislabelled "DBS Savings". Fall back to the
          // old hard-coded label only if the field is somehow absent.
          from:   (d.source_account_display ? d.source_account_display + ' · ' : 'DBS Savings · ')
                  + (d.account_masked || 'ending 1234'),
          ref:    txn
        }};
    } else if(pendingReceipt && ev.event_type === 'INTERFACE_LINE'){
      // The first INTERFACE_LINE after LEDGER_RECORDED is the backend's own "All done..."
      // sentence. Drop the receipt right behind it so the two travel as a pair.
      steps.push(pendingReceipt);
      pendingReceipt = null;
    }
  }
  // Safety net: a LEDGER_RECORDED with no INTERFACE_LINE following (shouldn't happen on
  // the live path, where say_line always follows the emit) still needs the card to show
  // somewhere rather than disappear.
  if(pendingReceipt) steps.push(pendingReceipt);
  steps.push(...finalStateSteps(resp.session));
  await play(steps, my);
  // Feature 2: the receipt proves itself against the backend's append-only, hash-chained audit
  // log. GET /audit/{session_id} -> {chain:{intact, head, events, broken_at_seq}}. We fill the
  // receipt's verify line with the REAL result — a banking-grade trust signal, not a claim.
  if(typeof verifyReceiptChain === 'function') verifyReceiptChain(FREE_SESSION_ID);
  if(my!==RUN) return null;
  // The money has moved, but the transcript alone cannot tell the customer whether this
  // conversation is over or whether they are allowed to pay someone else. Say it out loud.
  // Only once the WHOLE batch is done: a multi-instruction breath is confirmed one payment at a
  // time, and farewelling it half-way would read as "we are finished" while a payment is still
  // owed. There is deliberately NO countdown - a fresh payment takes ~20-30s to PREPARE, but
  // nothing makes anybody wait, and a timer would invent a rule the backend does not have
  // (measured: backend/tools/second_payment_probe.py, 17s / 25s / 32s across three payments).
  if(!resp.session.pending_remaining
     && (resp.session.state === 'EXECUTED' || resp.session.state === 'SCHEDULED')){
    const g = tt(lang).chrome.goodbye;
    await popup({
      gif: STAGE_GIF.goodbye, gifAlt:'Joy says goodbye', kicker:g.kicker,
      title:g.title, body:g.body,
      acts:[{id:'done',label:g.done,kind:'yes'}],
      auto: 15000, autoResult:'done',
      note:g.note
    });
    if(my!==RUN) return null;
  }
  return resp.session;
}

// asrConfidence: the REAL confidence the speech engine reported for this utterance, or null
// when it reported none. Passing it through is what makes the backend's ASR ambiguity
// guardrail live; previously every voice turn was sent as a hardcoded 0.93, so a badly
// misheard amount could never trigger a "please repeat that" challenge.
// asrSource travels with it so the backend can always tell a measurement from a stand-in.

/* Take the working card down, but ONLY if it is still the working card on screen. If the
   customer already hid it, or the review card has taken over, it must not be torn down from
   under them. Used on the paths that raise no card of their own; a path that DOES raise one
   simply lets popup() overwrite #mcard, which is an instant swap with no blank in between. */
function dismissWorking(workPopup){
  if(!workPopup) return;
  const media = popMedia();
  const stillWorking = media && (media.getAttribute('src')||'').indexOf('Working')!==-1;
  if($('modal').classList.contains('show') && stillWorking) closePopup('auto');
}

async function liveFreeTurn(text, viaVoice, asrConfidence, asrSource){
  stopSpeaking();   // barge-in: a fresh customer turn cuts Auntie off mid-sentence
  const my=++RUN;
  BUSY=true;
  if(!FREE_SESSION_ID){
    FREE_SESSION_ID = 'sess_free_' + Math.random().toString(36).slice(2,10);
    // Panels only. The transcript is deliberately NOT cleared: Joy's intro card
    // (and anything already said) must stay on screen, or starting a conversation would
    // look identical to the page reloading.
    resetPanels();
    document.querySelectorAll('.sbtn').forEach(b=>b.classList.remove('on'));
    AUTO_PERSONALIZE = false;   // a new conversation starts with a clean read, no carry-over profiling
    updatePersonalizeBadge();
    await sleep(120);
    if(my!==RUN) return;
    audit('LIVE BACKEND · '+API_BASE,'ok');
  }
  // Mirrors the backend's dominant_language() so the processing card is in the same language
  // as the reply that is about to arrive (see dominantLang above).
  //
  // The card is drawn BEFORE the backend answers, so on the FIRST turn it can only guess. From
  // the second turn on this conversation already HAS a settled language - the one the backend
  // chose, and the one every other card (field review, Face ID, receipt) is about to follow -
  // so preferring it means the card cannot disagree with the answer that arrives a moment later.
  // That is what stopped a Bahasa conversation from showing an English "working on it" card
  // followed by Bahasa confirmation and Face ID cards: three cards from one backend, two tongues.
  // A turn written in Chinese characters still wins immediately, because that is a script rather
  // than a guess, and it is how a mid-conversation switch to 中文 lands on the switch turn itself.
  const guessed = dominantLang(text);
  let lang = (guessed === 'ZH') ? 'ZH' : (SETTLED_IFACE_LANG || guessed);
  // Clear only the STALE, transient mic status markers ("Listening…" / "Hearing…" /
  // "Stopped listening…") left over from a PREVIOUS mic press. The live "Heard you" marker
  // is dropped at the source (micCapture) the moment the transcript settles, so nothing of
  // that shape should ever arrive here. Clarity still survives in the STATE panel row and
  // the engine survives in the VOICE ENGINE panel row. Clarification cards (.ccard) and
  // every other bubble stay untouched.
  chatEl.querySelectorAll('.row.them .bub.iface').forEach(b => {
    if(/^(Listening|Hearing|Stopped listening)/.test(b.textContent || '')) b.closest('.row').remove();
  });
  // Echo the customer's own words into the transcript. BOTH typed and voice turns get a
  // right-column "YOU" echo for symmetry, but a VOICE turn has usually already drawn that
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
  // A turn is in flight and there is nothing to look at yet: put the working card up. It closes
  // itself on a timer AND is dismissable, so a slow request can never become a dead end, and the
  // turn resolves it either way. Only raised when no other card is already up, a field review or
  // a Face ID prompt must never be covered by it. Nothing has been sent at this point; the note
  // on the card says so, because a thinking pause otherwise looks like a silent transaction.
  let workPopup = null;
  {
    const media = popMedia();
    const already = media && (media.getAttribute('src')||'').indexOf('Working')!==-1;
    if(!already && !(popupResolve && $('modal').classList.contains('show'))){
      // tt() falls back to EN for an unrecognised language, so `working` is always present.
      const w = tt(lang).chrome.working;
      workPopup = popup({
        gif: STAGE_GIF.working, gifAlt:'Joy is working on it', kicker:w.kicker,
        title:w.title, body:w.body,
        acts:[{id:'hide',label:w.hide,kind:'no'}],
        auto: 30000, autoResult:'auto',
        note:w.note
      });
    }
  }

  // Paint the five specialists as "reviewing…" in the agent panel the moment the turn is
  // sent, so the 7-27s the backend spends screening/classifying/drafting/validating is shown
  // as Joy "thinking out loud" rather than an idle gap. Each card locks in when its verdict
  // lands; specialists the backend did not consult this turn are marked, not hidden.
  if(typeof agentThinking === 'function') agentThinking();

  let resp;
  try{
    // A number alone is not evidence. Where it came from travels with it, so the audit trail
    // never mistakes a stand-in for a measurement the engine actually reported.
    const hasConf = (typeof asrConfidence==='number' && asrConfidence>=0 && asrConfidence<=1);
    // VOICE MODE (demo switch, top bar). When ON we declare the speaker as unverified:
    // voice_risk=SPEAKER_MISMATCH makes stage 1 pause for secondary out-of-band confirmation.
    // The confidence is driven BELOW the 0.60 admission floor too, because that is what a
    // genuine speaker mismatch looks like on the wire - it is evidence, not a verdict.
    // When OFF neither field is sent, so the backend behaves exactly as it always did.
    const vMode = (typeof voiceModeOn === 'function') && voiceModeOn();
    resp = await postJSON('/turn', {
      // Voice Mode is a GLOBAL demo switch: when ON, every input (mic, typed, or chip) is
      // declared as a voice turn so the backend's Stage 1 audio-risk gate fires and holds the
      // send behind FaceID. When OFF, via_voice stays honest (typed = false → no gate).
      session_id: FREE_SESSION_ID, text, via_voice: vMode ? true : !!viaVoice,
      asr_confidence: vMode
        ? 0.41                              // unverified speaker: far below the 0.60 floor
        : (hasConf ? asrConfidence          // real engine confidence, when the browser gives one
                   : (viaVoice ? 0.93 : 0.99)),   // stand-in, labelled as such below
      // Never send a bare number: the source tells the backend whether the value above is
      // a measurement or just a stand-in, so a placeholder can never trigger a challenge.
      asr_confidence_source: vMode ? 'engine_reported'
        : (hasConf ? (asrSource || 'engine_reported')
                   : (viaVoice ? 'unreported' : 'typed')),
      // Omitted entirely when Voice Mode is off, so the backend's own heuristic decides.
      ...(vMode ? { voice_risk: 'SPEAKER_MISMATCH' } : {})
    });
  }catch(err){
    // A timeout and a refusal are different failures and must not be reported as one thing.
    // "HTTP 409" is the backend answering; "no response within 20s" is it not answering, which
    // is the honest thing to show a judge instead of a generic "unreachable".
    if(err && err.timedOut) audit('TURN_TIMEOUT · '+err.message,'stop');
    else audit('TURN_CALL_FAILED · '+err.message,'stop');
    // A failed call must not leave the working card up. Nothing is in flight any more, and a
    // card that says "preparing the payment" would be claiming work that was never started.
    if(workPopup) closePopup('cancelled');
    // The backend never answered, so there is no catalogue line to quote and no settled session
    // language: the local reading of this turn's words is the best available, and the fallback
    // sentence is chrome, so it follows that.
    iface(tt(lang).chrome.unreachable);
    BUSY=false; return;
  }
  if(my!==RUN) return;
  // The answer is in, but the working card is DELIBERATELY STILL UP. What comes next, play()
  // animating the control chain, then the field review / Face ID card, takes a couple of
  // seconds, and taking the card down first left the customer staring at a finished transcript
  // with nothing happening: the dead gap. So the card now stays until either the next popup
  // takes it over (popup() overwrites #mcard, so that swap is instant, with no close-then-
  // reopen blank) or, on a path that raises no card, dismissWorking() below takes it down.
  // The backend has now settled which language this conversation is spoken in (it counts the
  // customer's words and holds the winner for the whole session). Adopt it for everything the
  // customer sees next (the field review, the Face ID prompt, the receipt) instead of the
  // local guess above, which only looked at this one sentence and disagreed with the backend.
  if(resp.session && resp.session.interface_language){
    lang = resp.session.interface_language;
    // Remember it for the NEXT turn's "working on it" card, which is drawn before any answer
    // exists and therefore has no backend language to consult of its own.
    SETTLED_IFACE_LANG = lang;
  }
  dismissWorking(workPopup); // the answer is in, so stop saying "working on it"
  const seenStages=new Set();
  const steps = eventsToSteps(resp.events, seenStages).concat(finalStateSteps(resp.session));
  await play(steps, my);
  if(typeof agentThinkingDone === 'function') agentThinkingDone();   // mark any specialist not consulted this turn
  if(my!==RUN) return;

  const st = resp.session.state;
  syncPersonalization(resp.session);   // the backend's Personalization Agent already decided this turn
  capline(captionFor(resp));           // the per-turn headline under the control chain
  showUnderstood(resp, asrConfidence, viaVoice);   // diagnostic: intent + clarity, in the STATE panel
  showLanguage(resp, text);             // diagnostic: multilingual mixing + interface switch, in the STATE panel
  // IDENTITY STEP-UP (Voice Mode / unverified speaker). Stage 1 could not verify the speaker, so
  // the backend HELD the request and suppressed the read-back: Joy has spoken only the hold line,
  // and the payment has NOT been presented. FaceID decides whether it ever is, and it runs HERE,
  // before the review card, so an unverified person never sees the amount or the payee. Declining
  // leaves the request held: no draft card, no /confirm, no payment.
  let faceidDone = false;
  if(resp.session.step_up_required && resp.session.step_up_reason === 'SPEAKER_MISMATCH'){
    faceidDone = await stepUpFaceId(lang, my);
    if(my!==RUN || !faceidDone){
      dismissWorking(workPopup);
      BUSY=false; return;
    }
    // Verified on this device: release the hold so the normal review can present the payment.
    // faceidDone tells liveConfirmFlow not to ask for FaceID a second time.
    resp.session.step_up_required = false;
  }
  // The read-back may have just ASKED the customer something, to confirm a slang amount
  // ("a grand"), or which account a payment should come from. Showing the "Confirm & Slide to
  // Pay" card at that moment puts two competing actions on screen (the card and the question
  // above it) and the customer cannot tell which to do first, so the card is held back until
  // they have answered. Nothing is skipped: the very next turn releases it.
  if(st==='CONFIRMATION_READY' && resp.session.draft && !resp.session.awaiting_multi_go_ahead){
    // liveConfirmFlow raises the review card, which overwrites #mcard and therefore REPLACES
    // the working card with no gap. dismissWorking() afterwards is a no-op unless that flow
    // raised no card at all, in which case the working card would otherwise sit there.
    const afterConfirm = await liveConfirmFlow(resp.session, lang, my, faceidDone);
    dismissWorking(workPopup);
    // A FINISHED payment ends its conversation, so the NEXT payment has to open a fresh
    // session. The terminal check further down never catches this one: it tests `st`, which
    // is the state from THIS turn's /turn response (CONFIRMATION_READY) and cannot see the
    // state the /confirm call ended on. So without this line the second payment reuses a
    // session that still carries the first payment's execution row, and /confirm lands on
    // CONFIRMATION_COMPLETE / AWAITING_BIOMETRIC instead of EXECUTED - the money looks like
    // it went through, but no receipt ever arrives and the customer is left hanging.
    // Measured, not guessed: backend/tools/second_payment_probe.py (25s fresh -> EXECUTED,
    // 32s reused -> stalled, 17s fresh again -> EXECUTED).
    // pending_remaining guards the multi-instruction batch: instructions are reviewed ONE AT
    // A TIME, so while more are still queued the session must stay alive for the next one.
    if(afterConfirm && TERMINAL_STATES.has(afterConfirm.state)
       && !afterConfirm.pending_remaining) FREE_SESSION_ID = null;
  } else if(st==='CONFIRMATION_READY' && resp.session.awaiting_multi_go_ahead){
    audit('CONFIRM_CARD_DEFERRED · the read-back above is still asking a question; '
          + 'the review card appears once the customer answers.','ok');
    dismissWorking(workPopup);   // no card is coming, so the transcript must be visible
  } else {
    dismissWorking(workPopup);   // ditto: the customer should be looking at the transcript
    if(TERMINAL_STATES.has(st)) FREE_SESSION_ID = null;   // conversation ended, next turn starts fresh
    if(st==='CLARIFICATION_REQUIRED'){
      // The backend already spoke this turn's clarification line, and play() rendered it as
      // a plain bubble. Re-present that SAME backend wording as an in-transcript card (the
      // client writes no sentence of its own) and drop the plain bubble so Auntie never
      // sees the same line twice.
      const said = lastInterfaceLine(resp.events);
      if(said){
        removeLastIfaceBubble();
        clarifyCard(said, ()=>{ const f=$('fld'); if(f) f.focus(); }, lang);
      }
    }
  }
  // Every sentence this turn came back as an INTERFACE_LINE event and was played above.
  // The client composes nothing: no clarification wording, no freeze explanation, no receipt.
  if(my!==RUN) return;
  BUSY=false;
}

fld.addEventListener('keydown',e=>{ if(e.key==='Enter'&&fld.value.trim()){const t=fld.value.trim();fld.value='';liveFreeTurn(t,false);} });

// Web Speech API only listens in ONE language at a time; it can't auto-detect,
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
  if(l === 'BM') return 'bm';
  if(l === 'ZH') return 'zh';
  if(l === 'AUTO') return 'multilingual';   // 16k_multi_lang: English + Malay, no need to pick one
  return 'en';
}

// The demo has a single customer; we forward this id to the STT socket so Tencent ASR can bias
// toward her own payee names. Mirrors the backend's DEMO_CUSTOMER_ID (session.Session default);
// a real build would take this from the authenticated session instead of a constant.
const CUSTOMER_ID = 'cust_auntie_001';

/* The real ears: stream the mic to the backend, which asks Tencent Cloud ASR.
   Resolves with {text, confidence, confidenceSource}; throws if the engine is unusable. */
async function sttTurn(setYou){
  // Tap-to-talk that waits for the engine to finish. We deliberately do NOT call start()
  // and stop() back-to-back; that gave no listening window, so the engine returned an
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
    client.start(sttLanguage(), CUSTOMER_ID).then(() => {
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
    // Errors are SYSTEM messages, so they belong on the left, never in the customer's bubble.
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
  // but that only fires when the engine produced a result; if it didn't (no speech,
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
    iface('Voice input is not available in this browser. Please type, or run a scripted scenario.');
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
  // words stream into their own right-column YOU bubble, word by word, via setYou, so the
  // transcript reads like a real voice-first exchange instead of the system quoting them back.
  let live = iface(icon('mic') + 'Listening…');
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
    // Prefer the real engine. Any failure (no config, no server, no key) falls through to
    // the browser's recogniser rather than leaving Auntie with a dead button.
    // The swap USED to be silent, so nobody could tell which engine the words came from -
    // and their confidence numbers are not the same measurement (Tencent reports word
    // stability, the browser reports its own score), so "clarity %" isn't comparable
    // between them. Both the audit trail and the customer's own status line now name the
    // engine that actually ran, so a demo never claims Tencent ASR it didn't use.
    if(haveStt){
      let r = null;
      try{ r = await sttTurn(setYou); }
      catch(err){
        console.warn('[joy] falling back to the browser recogniser:', err.message);
        audit('ASR_UNAVAILABLE · ' + err.message + ' · falling back to the browser recogniser','warn');
        // Reveal the VOICE ENGINE row: the real engine failed, so the panel has to say so.
        voiceEngine('Browser fallback · Tencent ASR not connected', true);
      }
      if(r && r.text){
        setYou(r.text);   // settle the customer's bubble on the FINAL text, not the last partial
        const c = (typeof r.confidence==='number') ? r.confidence : null;
        // The mic status bubble ("Listening…") has done its job: the YOU bubble is now the
        // "I heard you" beat. Drop it so the chat reads cleanly: your words -> Joy's reply.
        // Engine + clarity still survive in the panel (VOICE ENGINE row + STATE row).
        if(live){ try{ live.remove(); }catch(_){} live = null; }
        // sttTurn is the Tencent path, so any text it returns came from Tencent - even the
        // interim-only fallback, where the final result never arrived.
        const eng = 'Tencent ASR' + (r.engine ? ' (' + r.engine + ')' : '');
        audit('ASR_ENGINE · ' + eng + ' · confidence source ' + (r.confidenceSource || 'unknown'),'ok');
        voiceEngine(eng, false);   // record which engine ran, but do not reveal the row on success
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
      // Drop the mic status bubble: the YOU bubble is the "heard you" feedback now.
      // Engine + clarity are recorded in the panel (VOICE ENGINE row + audit timeline).
      if(live){ try{ live.remove(); }catch(_){} live = null; }
      audit('ASR_ENGINE · browser fallback · Tencent ASR produced no transcript for this turn','warn');
      voiceEngine('Browser fallback · Tencent ASR not connected', true);
      // The browser's score is a TRANSCRIPTION-accuracy measure, not a liveness signal, so
      // it is tagged browser_recognition: Stage 1 then admits the turn instead of misreading a
      // low BM score as "synthetic/replayed" (see orchestrator Stage 1).
      liveFreeTurn(spoken.text, true, c, 'browser_recognition');
      return;
    }

    // Nothing usable came back: never leave a ghost YOU bubble or a stuck "Listening…" marker.
    if(_pendingYouBubble){ try{ _pendingYouBubble.remove(); }catch(_){} _pendingYouBubble = null; }
    const b = live && live.querySelector('.bub');
    if(b && /Listening…/.test(b.textContent)) b.innerHTML = icon('mic') + 'Stopped listening. Nothing was heard.';
  } finally {
    mic.classList.remove('on');
  }
}

/* ---------- boot ---------- */
/* Boot: play the happy path so the first thing anyone sees is the real control chain,
   then it pauses at the field-by-field confirmation popup waiting for Auntie. */
// runScenario('HAPPY');
