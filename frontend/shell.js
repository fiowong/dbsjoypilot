/* 6/6 shell.js — load LAST. Device shell only: the DBS digibank phone mockup that
   wraps the three app screens (Login → digibank home → AuntieJoy chat). It switches
   screens, renders the balance hero / "Enable AuntieJoy" hero / quick-action tiles,
   and drops the landing intro card into the chat. It does NOT implement any payment
   logic — that is owned by the five files above (translations/ui/personalization/
   scenarios/live) and by the backend. Everything it touches is a global already defined
   by those files: MASCOT, money(), chatEl, scroll(), resetRun(), popup(), liveFreeTurn(),
   mic. Loaded after live.js so all of those exist. */

/* ---------- element refs ---------- */
const studioEl  = $('studio');
const appBodyEl = $('appBody');
const avImgEl   = $('avImg');
if(avImgEl) avImgEl.src = MASCOT;   // point the chat header avatar at the mascot

/* ---------- one DBS app screen at a time, inside the device ---------- */
const SCREENS = ['scrLogin','appwin','scrJoy'];
function showScreen(id){
  SCREENS.forEach(s=>{ const el = $(s); if(el) el.classList.toggle('on', s===id); });
}

function showLogin(){
  showScreen('scrLogin');
  studioEl.classList.add('appmode');   // the engine room isn't relevant before log in
}

/* ---------- digibank home ---------- */
const HOME_HTML = `
  <div class="balcard"><div class="bb">
    <div class="bk">DBS SAVINGS · ENDING 1234</div>
    <div class="bv">SGD 12,480.55</div>
    <div class="bs">Available balance · updated just now</div>
  </div></div>

  <button class="enable" type="button" data-go="joy" data-blank="1">
    <span class="eav"><img src="${MASCOT}" alt=""></span>
    <span class="etxt">
      <span class="ebadge">VOICE-FIRST · NEW</span>
      <div class="et">Enable AuntieJoy</div>
      <div class="ed">Just say who you want to pay and how much. AuntieJoy gets it ready,
        DBS checks it, and you approve it.</div>
    </span>
    <span class="earrow">›</span>
  </button>

  <div class="appsec">QUICK ACTIONS</div>
  <div class="tiles">
    <button class="tile" type="button" data-go="pay"><div class="ti">💸</div>
      <div class="tn">Pay &amp; Transfer</div><div class="td">Send money to any payee</div></button>
    <button class="tile" type="button" data-go="pay"><div class="ti">📱</div>
      <div class="tn">PayNow</div><div class="td">Transfer by mobile or NRIC</div></button>
    <button class="tile" type="button" data-go="pay"><div class="ti">📷</div>
      <div class="tn">Scan &amp; Pay</div><div class="td">Pay with a QR code</div></button>
    <button class="tile" type="button" data-go="pay"><div class="ti">💳</div>
      <div class="tn">Cards</div><div class="td">Manage your cards</div></button>
  </div>`;

const PAY_HTML = `
  <div class="appsec" style="margin-top:0">PAY &amp; TRANSFER</div>
  <div class="applist">
    <button class="approw hero" type="button" data-go="joy">
      <span class="ri">🎤</span>
      <span class="rt"><div class="rn">Ask AuntieJoy instead</div>
        <div class="rd">Say it in English, 中文 or Bahasa Melayu</div></span>
      <span class="rc">›</span>
    </button>
    <button class="approw" type="button" data-go="saved">
      <span class="ri">👤</span>
      <span class="rt"><div class="rn">To a saved payee</div>
        <div class="rd">Mei Tan · John Doe · Alice</div></span>
      <span class="rc">›</span>
    </button>
    <button class="approw" type="button" data-go="new">
      <span class="ri">➕</span>
      <span class="rt"><div class="rn">To a new payee</div>
        <div class="rd">Add an account number or PayNow ID</div></span>
      <span class="rc">›</span>
    </button>
    <button class="approw" type="button" data-go="paynow">
      <span class="ri">SG</span>
      <span class="rt"><div class="rn">PayNow transfer</div>
        <div class="rd">Mobile number, NRIC or UEN</div></span>
      <span class="rc">›</span>
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
    body:'Every transfer in this build is prepared by <b>AuntieJoy</b> — that is the thing being demonstrated. '+
         'The other Pay &amp; Transfer routes are shown so you can see where AuntieJoy sits in the real app.',
    acts:[{id:'joy',label:'Open AuntieJoy',kind:'yes'},{id:'back',label:'Back to Pay &amp; Transfer',kind:'no'}],
    note:'Nothing was sent, and no payment was created.'
  });
  if(a==='joy') showJoy();
}

/* ---------- AuntieJoy chat surface ---------- */
function introCard(){
  const d=document.createElement('div');
  d.className='row them';
  d.innerHTML=
    `<div class="intro">
       <div class="ihead">
         <span class="iav"><img src="${MASCOT}" alt=""></span>
         <span>
           <div class="inm">AuntieJoy</div>
           <div class="isub">voice-first payments</div>
         </span>
         <img class="dbslogo sm" src="assets/dbs-logo-white.png" alt="DBS">
       </div>
       <div class="ibody">
         <div class="ihi">Hello! I'm AuntieJoy 👋</div>
         <div class="itext">I can get a payment ready for you. Just tell me <b>who</b> you'd like to
           pay and <b>how much</b> — in English, 中文 or Bahasa Melayu. I'll read it back to you,
           and nothing is sent until you say yes.</div>
         <div class="chips">
           <button class="chip" type="button" data-say="Pay Mei Tan 50 dollars">Pay Mei Tan $50</button>
           <button class="chip" type="button" data-say="Pay John Doe 100 dollars">Pay John Doe $100</button>
           <button class="chip" type="button" data-say="付给Mei Tan五十元">付给 Mei Tan $50</button>
           <button class="chip" type="button" data-say="Bayar Mei Tan lima puluh dolar">Bayar Mei Tan $50</button>
         </div>
         <button class="ibig" type="button">🎤 Tap to speak</button>
         <div class="inote">You can also type in the box below. DBS checks every payment before it
           goes out — and AuntieJoy can never send one on its own.</div>
       </div>
     </div>`;
  chatEl.appendChild(d); scroll();
  d.querySelectorAll('.chip').forEach(c=>c.addEventListener('click', ev=>{
    ev.preventDefault();
    liveFreeTurn(c.dataset.say, false);
  }));
  const big=d.querySelector('.ibig');
  if(big) big.addEventListener('click', ev=>{ ev.preventDefault(); mic.click(); });
  return d;
}

function showJoy(blank){
  showScreen('scrJoy');
  studioEl.classList.remove('appmode');
  if(blank){
    chatEl.innerHTML='';          // fresh start — drop any prior transcript
    resetRun();                   // resolve any popup the old run was blocked on
    introCard();                  // the warm one-tap way in
  }
  const f = $('fld');
  if(f) f.focus();
}

/* ---------- shell wiring ---------- */
const lgBtn = $('lgBtn');
if(lgBtn) lgBtn.addEventListener('click', ev=>{ ev.preventDefault(); showHome(); });

const appBack = $('appBack');
if(appBack) appBack.addEventListener('click', ev=>{ ev.preventDefault(); showJoy(); });

const restartBtn = $('restartBtn');
if(restartBtn) restartBtn.addEventListener('click', ev=>{ ev.preventDefault(); showHome(); });

/* The MECE A–H rail is a BACKUP, kept for the BRD walkthrough and judge Q&A. It is
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
