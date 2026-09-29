/* 6/6 shell.js, load LAST. Device shell only: the DBS digibank phone mockup that
   wraps the three app screens (Login → digibank home → AuntieJoy chat). It switches
   screens, renders the balance hero / "Enable AuntieJoy" hero / quick-action tiles,
   and drops the landing intro card into the chat. It does NOT implement any payment
   logic, that is owned by the five files above (translations/ui/personalization/
   scenarios/live) and by the backend. Everything it touches is a global already defined
   by those files: MASCOT, money(), chatEl, scroll(), resetRun(), popup(), liveFreeTurn(),
   mic. Loaded after live.js so all of those exist. */

/* ---------- element refs ---------- */
const studioEl  = $('studio');
const appBodyEl = $('appBody');
const deviceScreenEl = $('deviceScreen');

/* ---------- one DBS app screen at a time, inside the device ---------- */
const SCREENS = ['scrLogin','appwin','scrJoy'];
function showScreen(id){
  SCREENS.forEach(s=>{ const el = $(s); if(el) el.classList.toggle('on', s===id); });
  // AuntieJoy is the only screen with a LIGHT header, so the mock status bar flips to dark
  // ink there. Without this the white status text vanishes against the pale page.
  if(deviceScreenEl) deviceScreenEl.classList.toggle('light-bar', id==='scrJoy');
}

function showLogin(){
  showScreen('scrLogin');
  studioEl.classList.add('appmode');   // the engine room isn't relevant before log in
}

/* ---------- digibank home ---------- */
/* Every icon here comes from icons.js, no OS emoji in banking chrome. The balance card is a
   dark gradient surface with a masked account number and a currency pill; the AuntieJoy entry
   is a white card with a red accent badge and a pulsing mic dot rather than a full-red block. */
const HOME_HTML = `
  <div class="balcard"><div class="bb">
    <div class="brow">
      <div>
        <div class="bk">DBS Multi-Currency Autosave</div>
        <div class="bnum">•••• 1234</div>
      </div>
      <span class="bpill">PRIMARY</span>
    </div>
    <div class="bamt"><span class="bcur">SGD</span><span class="bv">12,480.55</span></div>
    <div class="bdiv"></div>
    <div class="bs"><span>Available balance</span><span>Updated just now</span></div>
  </div></div>

  <button class="enable" type="button" data-go="joy" data-blank="1">
    <span class="eav">${icon('mic')}<span class="epulse"></span></span>
    <div class="etxt">
      <span class="ebadge">${icon('sparkle','ic-sm')}VOICE-FIRST</span>
      <div class="et">Enable AuntieJoy</div>
      <div class="ed">Just say who you want to pay and how much. AuntieJoy gets it ready,
        DBS checks it, and you approve it.</div>
    </div>
    <span class="earrow">${icon('chevronRight')}</span>
  </button>

  <div class="appsec">Quick actions</div>
  <div class="tiles">
    <button class="tile" type="button" data-go="pay">
      <span class="ti">${icon('transfer')}</span>
      <div><div class="tn">Pay &amp; Transfer</div><div class="td">Local &amp; international</div></div>
    </button>
    <button class="tile" type="button" data-go="pay">
      <span class="ti">${icon('paynow')}</span>
      <div><div class="tn">PayNow</div><div class="td">Mobile, NRIC or UEN</div></div>
    </button>
    <button class="tile" type="button" data-go="pay">
      <span class="ti">${icon('scan')}</span>
      <div><div class="tn">Scan &amp; Pay</div><div class="td">SGQR / NETS QR</div></div>
    </button>
    <button class="tile" type="button" data-go="pay">
      <span class="ti">${icon('card')}</span>
      <div><div class="tn">Cards</div><div class="td">Manage &amp; controls</div></div>
    </button>
  </div>`;

const PAY_HTML = `
  <div class="appsec" style="margin-top:0">Pay &amp; Transfer</div>
  <div class="applist">
    <button class="approw hero" type="button" data-go="joy">
      <span class="ri">${icon('mic')}</span>
      <span class="rt"><div class="rn">Ask AuntieJoy instead</div>
        <div class="rd">Say it in English, 中文 or Bahasa Melayu</div></span>
      <span class="rc">${icon('chevronRight')}</span>
    </button>
    <button class="approw" type="button" data-go="saved">
      <span class="ri">${icon('user')}</span>
      <span class="rt"><div class="rn">To a saved payee</div>
        <div class="rd">Mei Tan · John Doe · Alice</div></span>
      <span class="rc">${icon('chevronRight')}</span>
    </button>
    <button class="approw" type="button" data-go="new">
      <span class="ri">${icon('plus')}</span>
      <span class="rt"><div class="rn">To a new payee</div>
        <div class="rd">Add an account number or PayNow ID</div></span>
      <span class="rc">${icon('chevronRight')}</span>
    </button>
    <button class="approw" type="button" data-go="paynow">
      <span class="ri">${icon('paynow')}</span>
      <span class="rt"><div class="rn">PayNow transfer</div>
        <div class="rd">Mobile number, NRIC or UEN</div></span>
      <span class="rc">${icon('chevronRight')}</span>
    </button>
  </div>`;

function showApp(html){
  appBodyEl.innerHTML = html;
  showScreen('appwin');
  studioEl.classList.add('appmode');
  appBodyEl.querySelectorAll('[data-go]').forEach(b=>b.addEventListener('click', ev=>{
    ev.preventDefault();
    const go = b.dataset.go;
    if(go==='joy')          showJoy(b.dataset.blank==='1');
    else if(go==='pay')     showPay();
    else if(go==='saved' || go==='new' || go==='paynow') showPayScopeNotice();
    else if(go==='back')    showPay();
    else                     showHome();
  }));
}
function showHome(){ showApp(HOME_HTML); }
function showPay(){  showApp(PAY_HTML);  }

/* The other Pay & Transfer routes exist in the real DBS app but aren't part of this
   prototype. Rather than fake them, say so plainly and hand the customer the path
   that *is* built. */
async function showPayScopeNotice(){
  const a = await popup({
    kicker:'PROTOTYPE SCOPE',
    title:'That route isn’t built in this demo',
    body:'Every transfer in this build is prepared by <b>AuntieJoy</b>, that is the thing being demonstrated. '+
         'The other Pay &amp; Transfer routes are shown so you can see where AuntieJoy sits in the real app.',
    acts:[{id:'joy',label:'Open AuntieJoy',kind:'yes'},{id:'back',label:'Back to Pay &amp; Transfer',kind:'no'}],
    note:'Nothing was sent, and no payment was created.'
  });
  if(a==='joy') showJoy();
}

/* Same honesty rule for the two other real-digibank affordances we now surface on the login
   screen: say they aren't built rather than leaving a control that does nothing. */
async function showAltLoginNotice(){
  await popup({
    kicker:'PROTOTYPE SCOPE',
    title:'Digital Token / Face ID sign-in isn’t wired up',
    body:'The real digibank app signs you in with a <b>Digital Token</b> or <b>Face ID</b>. '+
         'This build demonstrates <b>AuntieJoy</b>, so sign-in uses the sample credentials shown above.',
    acts:[{id:'ok',label:'Got it',kind:'yes'}],
    icon:'faceid',
    note:'Nothing was sent, and no credential was collected.'
  });
}
async function showForgotNotice(){
  await popup({
    kicker:'PROTOTYPE SCOPE',
    title:'PIN recovery isn’t part of this demo',
    body:'In the real app you would reset your PIN through DBS support. This build only '+
         'demonstrates the <b>AuntieJoy</b> payment flow.',
    acts:[{id:'ok',label:'Got it',kind:'yes'}],
    icon:'lock',
    note:'Nothing was sent.'
  });
}

/* ---------- AuntieJoy chat surface ----------
   The landing block is one transcript child: the assistant's message card, the
   "SAMPLE VOICE COMMANDS" label, and the example chips. Kept together so the
   transcript still holds exactly one thing before the customer says anything.
   The card is a plain WHITE surface with a monogram avatar, the assistant is
   introduced by the card, so the app bar above it can stay quiet navigation. */
function introCard(){
  const d=document.createElement('div');
  d.className='row them full';
  d.innerHTML=
    `<div class="intro">
       <div class="ihead">
         <span class="ajav">AJ</span>
         <span class="inm">AuntieJoy</span>
         <span class="itag">Voice Assistant</span>
       </div>
       <div class="itext">Hello<span data-nslot></span>! Tell me who to pay and how much.
         I support English, 华语, or Bahasa Melayu.</div>
       <div class="isec">${icon('alert')}
         <div><b>Security Check:</b> AuntieJoy prepares the transaction. You must review &amp;
           authorize with your Digital Token before any money moves.</div>
       </div>
     </div>
     <div class="cmdlabel">SAMPLE VOICE COMMANDS</div>
     <div class="chips">
       <button class="chip" type="button" data-say="Pay Mei Tan 50 dollars">&ldquo;Pay Mei Tan $50&rdquo;</button>
       <button class="chip" type="button" data-say="付给Mei Tan五十元">&ldquo;付给 Mei Tan $50&rdquo;</button>
       <button class="chip" type="button" data-say="Bayar Mei Tan lima puluh dolar">&ldquo;Bayar Mei Tan $50&rdquo;</button>
     </div>`;
  chatEl.appendChild(d); scroll();
  if(typeof refreshNameSlots === 'function') refreshNameSlots(d);
  d.querySelectorAll('.chip').forEach(c=>c.addEventListener('click', ev=>{
    ev.preventDefault();
    liveFreeTurn(c.dataset.say, false);
  }));
  return d;
}

function showJoy(blank){
  showScreen('scrJoy');
  studioEl.classList.remove('appmode');
  if(blank){
    chatEl.innerHTML='';          // fresh start, drop any prior transcript
    resetRun();                   // resolve any popup the old run was blocked on
    introCard();                  // the warm one-tap way in
  }
  const f = $('fld');
  if(f) f.focus();
}

/* ---------- shell wiring ---------- */
const lgBtn = $('lgBtn');
if(lgBtn) lgBtn.addEventListener('click', ev=>{ ev.preventDefault(); showHome(); });

/* Trailing validity tick. It only appears once the field actually matches the real Singapore
   NRIC shape (letter + 7 digits + letter), so the cue carries information instead of being
   decoration that is always on. */
const lgUserEl = $('lgUser'), lgUserOk = $('lgUserOk');
if(lgUserEl && lgUserOk){
  const markValid = () => lgUserOk.classList.toggle('on', /^[STFGM]\d{7}[A-Z]$/i.test(lgUserEl.value.trim()));
  lgUserEl.addEventListener('input', markValid);
  markValid();
}

const lgAlt = $('lgAlt');
if(lgAlt) lgAlt.addEventListener('click', ev=>{ ev.preventDefault(); showAltLoginNotice(); });

const lgForgot = $('lgForgot');
if(lgForgot) lgForgot.addEventListener('click', ev=>{ ev.preventDefault(); showForgotNotice(); });

const appBack = $('appBack');
if(appBack) appBack.addEventListener('click', ev=>{ ev.preventDefault(); showJoy(); });

/* The chat header's Back control returns to the digibank home, the same single-header
   navigation the redesign calls for. */
const joyBack = $('joyBack');
if(joyBack) joyBack.addEventListener('click', ev=>{ ev.preventDefault(); showHome(); });

const restartBtn = $('restartBtn');
if(restartBtn) restartBtn.addEventListener('click', ev=>{ ev.preventDefault(); showHome(); });

/* The MECE A-H rail is a BACKUP, kept for the BRD walkthrough and judge Q&A. It is
   hidden by default so the landing experience is the DBS app, not a scenario picker. */
const railEl = $('rail'), railToggle = $('railToggle');
if(railToggle && railEl) railToggle.addEventListener('click', ev=>{
  ev.preventDefault();
  railEl.hidden = !railEl.hidden;
  railToggle.textContent = railEl.hidden ? 'Scenarios' : 'Hide scenarios';
});

/* Boot: first thing anyone sees is the DBS digibank login, exactly as a real
   customer would. AuntieJoy is reached by logging in and choosing to enable it. */
showLogin();
