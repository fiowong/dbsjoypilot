/* 6/6 shell.js, load LAST. Device shell only: the DBS digibank phone mockup that
   wraps the three app screens (Login → digibank home → Joy chat). It switches
   screens, renders the balance hero / "Enable Joy" hero / quick-action tiles,
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
  // Every screen header is now dark (login black, digibank red, Joy near-black), so the
  // mock status bar stays its default WHITE ink on every screen — no light-bar flip needed.
  if(deviceScreenEl) deviceScreenEl.classList.remove('light-bar');
}

function showLogin(){
  showScreen('scrLogin');
  studioEl.classList.add('appmode');   // the engine room isn't relevant before log in
  // Back at the launchpad always means the six tiles: a restart should never drop the
  // customer straight into a half-filled credential form.
  const f = $('lgForm');
  if(f) f.hidden = true;
}

/* ---------- digibank home ---------- */
/* Modelled on the real digibank dashboard (and on iggy, which got there first):
     - a flat red cashback hero,
     - a row of five quick-action icon tiles,
     - the one Joy entry point, a big red "Ask Joy to pay" CTA,
     - "Your accounts" with the deposits card,
     - a deposit promo and a "Discover more" pair,
     - the 5-tab bottom nav (declared in index.html, wired below).
   Joy gets exactly ONE entry point on this screen. Two ways into the same conversation
   on one screen is confusing, so the CTA is it. */
/* The two numbers the home dashboard quotes for the customer's deposits. Kept as named
   constants so the figure is a one-line edit and cannot drift between the card and
   anything else that quotes it. The account label follows backend/app/data.py
   (SourceAccount "DBS Savings", masked "ending 1234") - it is the same account the
   payment chain actually resolves against. */
const DEPOSITS_SGD     = '12,480.55';
const DEPOSITS_ACCOUNT = 'DBS Savings ending 1234';

const HOME_HTML = `
  <!-- Cashback hero: flat DBS red with the wording set as real text on the left and the
       supplied cut-out on the right. Built from type + image rather than a baked-in hero
       JPG, so the copy can be edited and nothing can drift out of alignment. -->
  <div class="hero">
    <div class="hero-copy">
      <div class="hero-t">Get up to S$60 Cashback</div>
      <div class="hero-s">Get rewards on your first transfer via DBS Remit</div>
    </div>
    <img class="hero-cutout" src="assets/lady-corner.png" alt="">
  </div>

  <div class="qrow">
    <button class="qbtn" type="button" data-tab="more"><img class="qimg" src="assets/tile-shield.png" alt=""><span>Security<br>Hub</span></button>
    <button class="qbtn" type="button" data-tab="pay"><img class="qimg" src="assets/tile-paynow.png" alt=""><span>PayNow</span></button>
    <button class="qbtn" type="button" data-tab="more"><img class="qimg" src="assets/tile-bills.png" alt=""><span>Bills,<br>Cards &amp;<br>GIRO</span></button>
    <button class="qbtn" type="button" data-tab="more"><img class="qimg" src="assets/tile-overseas.png" alt=""><span>Overseas<br>transfer</span></button>
    <button class="qbtn" type="button" data-tab="more"><img class="qimg" src="assets/tile-scan.png" alt=""><span>Insights</span></button>
  </div>

  <!-- Joy's single entry point on this screen. Above "Your accounts", directly under the
       quick-action row, so the order reads: shortcuts -> Joy -> accounts. Joy is the thing
       being demonstrated, so it takes the prominent slot rather than sitting below the fold. -->
  <button class="joy-cta" type="button" data-go="joy" data-blank="1">
    <img src="${MASCOT_FACE}" alt="" class="joy-av">
    <div class="joy-cta-t">
      <span class="joy-cta-b">VOICE-FIRST · NEW</span>
      <div class="joy-cta-h">Ask Joy to pay</div>
      <div class="joy-cta-d">Just say who you want to pay and how much. Joy gets it ready,
        DBS checks it, and you approve it.</div>
    </div>
    <span class="joy-cta-arrow">›</span>
  </button>

  <div class="ahdr">Your accounts</div>

  <div class="card dep">
    <div class="dep-l">
      <div class="dep-t">Deposits</div>
      <div class="dep-acc">${DEPOSITS_ACCOUNT}</div>
    </div>
    <div class="dep-r">
      <div class="dep-k">Available balance</div>
      <div class="dep-v">SGD ${DEPOSITS_SGD}</div>
    </div>
  </div>

  <!-- Static deposit-promo card, same real DBS cashback reminder as the hero. -->
  <button class="card promo" type="button" data-tab="apply">
    <span class="promo-l">
      <span class="promo-t">Deposit</span>
      <span class="promo-d">Enjoy up to S$60 cashback on your first transfer with DBS Remit!</span>
      <span class="promo-link">Find out more ›</span>
    </span>
    <span class="promo-r" aria-hidden="true">
      <span class="promo-bubble">$</span>
    </span>
  </button>

  <div class="ahdr">Discover more</div>
  <div class="row2">
    <div class="card hero2"><div class="hero2-t">eDocument reminder</div>
      <div class="hero2-d">Most of your documents are going digital from Oct 2026.</div>
      <div class="hero2-link">Learn more</div></div>
    <div class="card hero2"><div class="hero2-t">Insights Coming Soon</div>
      <div class="hero2-d">Deeper views into how you save, spend and grow.</div>
      <div class="hero2-link">Notify me</div></div>
  </div>`;

/* Pay & Transfer is a deliberate echo of the real digibank list: icon, name, supporting
   line, chevron. Icons come from icons.js SVG, not emoji - banking chrome must not depend
   on the OS emoji font rendering identically on a judge's laptop.
   The payee names are dbs's own registry (data.py): never copy iggy's chip/row strings,
   "Mei Ling" and "Daniel Tan" are not people in this demo. */
const PAY_HTML = `
  <div class="appsec" style="margin-top:0">Pay &amp; Transfer</div>
  <div class="applist">
    <button class="approw hero" type="button" data-go="joy">
      <span class="ri">${icon('mic')}</span>
      <span class="rt"><div class="rn">Ask Joy instead</div>
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
  </div>
  <div class="pnote">Nothing is sent from this screen. Joy always shows you the details first,
    and DBS checks every payment before it goes out.</div>`;

/* Which bottom tab reads as active. One helper so the nav and any deep link
   (a quick-action tile, the promo card) all move the highlight the same way. */
function setTab(t){
  document.querySelectorAll('#tabnav .tab').forEach(x=>x.classList.toggle('on', x.dataset.tab===t));
}

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
  // Quick-action tiles and the promo card. PayNow is the only one that leads somewhere in
  // this demo (the Pay & Transfer list); the rest switch the tab highlight and say plainly
  // that the route isn't built, rather than faking a screen the prototype doesn't have.
  appBodyEl.querySelectorAll('[data-tab]').forEach(b=>{
    b.addEventListener('click', ev=>{
      ev.preventDefault();
      const t = b.dataset.tab;
      if(t==='pay')  showPay();
      else if(t==='home') showHome();
      else { setTab(t); showComingSoon(t); }
    });
  });
}
function showHome(){ showApp(HOME_HTML); setTab('home'); }
function showPay(){  showApp(PAY_HTML);  setTab('pay');  }

/* digiWealth / Apply / More exist in the real digibank but aren't part of this prototype.
   Same honesty rule as everywhere else: say so, and point back at what IS built. */
async function showComingSoon(tab){
  const label = ({wealth:'digiWealth',apply:'Apply',more:'More'})[tab] || tab;
  await popup({
    kicker:'PROTOTYPE SCOPE',
    title:`${label} isn’t built in this demo`,
    body:'This route exists in the real DBS app but isn’t part of the prototype. '+
         'The piece that <i>is</i> built is the voice-first payment flow under <b>Joy</b>.',
    acts:[{id:'ok',label:'Back to Home',kind:'no'}],
    note:'Nothing was sent, and no payment was created.'
  });
}

/* The other Pay & Transfer routes exist in the real DBS app but aren't part of this
   prototype. Rather than fake them, say so plainly and hand the customer the path
   that *is* built. */
async function showPayScopeNotice(){
  const a = await popup({
    kicker:'PROTOTYPE SCOPE',
    title:'That route isn’t built in this demo',
    body:'Every transfer in this build is prepared by <b>Joy</b>, that is the thing being demonstrated. '+
         'The other Pay &amp; Transfer routes are shown so you can see where Joy sits in the real app.',
    acts:[{id:'joy',label:'Open Joy',kind:'yes'},{id:'back',label:'Back to Pay &amp; Transfer',kind:'no'}],
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
         'This build demonstrates <b>Joy</b>, so sign-in uses the sample credentials shown above.',
    acts:[{id:'ok',label:'Got it',kind:'yes'}],
    gif: STAGE_GIF.notallow,
    note:'Nothing was sent, and no credential was collected.'
  });
}
async function showForgotNotice(){
  await popup({
    kicker:'PROTOTYPE SCOPE',
    title:'PIN recovery isn’t part of this demo',
    body:'In the real app you would reset your PIN through DBS support. This build only '+
         'demonstrates the <b>Joy</b> payment flow.',
    acts:[{id:'ok',label:'Got it',kind:'yes'}],
    icon:'lock',
    note:'Nothing was sent.'
  });
}

/* "GET STARTED" — opening a new account is not something this prototype can do. */
async function showSignupNotice(){
  await popup({
    kicker:'PROTOTYPE SCOPE',
    title:'Opening an account isn’t part of this demo',
    body:'This build is already signed in as a sample customer so it can demonstrate '+
         '<b>Joy</b>. Use the sample credentials above to log in.',
    acts:[{id:'ok',label:'Got it',kind:'yes'}],
    gif: STAGE_GIF.notallow,
    note:'Nothing was collected, and no account was created.'
  });
}

/* ---------- Joy chat surface ----------
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
         <span class="iav"><img src="assets/auntie_mascot_half.png" alt="Joy"></span>
         <div class="imeta">
           <div class="inm">Joy</div>
           <div class="isub">voice-first payments</div>
         </div>
         <img class="dbslogo" src="assets/dbs-logo-white.png" alt="DBS">
       </div>
       <div class="ibody">
         <div class="ihi">Good to see you! How can I make your day easier?</div>
         <div class="itxt">Tell me who you'd like to pay and how much.
           You'll review and authorize before any money moves.</div>
         <div class="cmdlabel">SAMPLE VOICE COMMANDS</div>
         <div class="chips">
           <button class="chip" type="button" data-say="Pay Mei Tan 50 dollars">&ldquo;Pay Mei Tan $50 tomorrow&rdquo;</button>
           <button class="chip" type="button" data-say="Send Ah Boy one fifty">&ldquo;Send Ah Boy one fifty&rdquo;</button>
           <button class="chip" type="button" data-say="付三十块给Uncle Lim">&ldquo;付30块给Uncle Lim&rdquo;</button>
           <button class="chip" type="button" data-say="Bayar Ibu 20 dollar">&ldquo;Bayar Ibu 20 dollar&rdquo;</button>
         </div>
       </div>
     </div>`;
  chatEl.appendChild(d); scroll();
  if(typeof refreshNameSlots === 'function') refreshNameSlots(d);
  d.querySelectorAll('.chip').forEach(c=>c.addEventListener('click', ev=>{
    ev.preventDefault();
    liveFreeTurn(c.dataset.say, false);
  }));
  return d;
}

/* ---------- the Start gate ----------
   Joy is the thing being demonstrated, so she is the first thing on screen: a card with
   her waving hello, before the customer is asked for anything.
   Two deliberate choices:
   - It goes through popup() like every other card instead of writing into the popup layer
     directly. One popup code path, and it inherits the guard that stops a late timer from
     closing a card that has since been replaced.
   - The language comes from the mic's language pill. Nothing has been said yet, so there is no
     detected language to use; the pill is the only honest signal available this early. It stays
     put between conversations, so a second visit opens in whatever the customer last chose. */
async function welcomeSplash(){
  const l = document.querySelector('#langtog .lt.on')?.dataset.l || 'EN';
  const w = tt(l).chrome.welcome;
  await popup({
    gif: STAGE_GIF.welcome, gifAlt:'Joy says hello', splash:true, kicker:w.kicker,
    title:w.title, body:w.body,
    acts:[{id:'start',label:w.start,kind:'yes'}],
    note:w.note
  });
  // Hand the keyboard back only once she has stepped aside, or the caret lands behind the card.
  const f = $('fld');
  if(f) f.focus();
}

function showJoy(blank){
  showScreen('scrJoy');
  studioEl.classList.remove('appmode');
  if(blank){
    chatEl.innerHTML='';          // fresh start, drop any prior transcript
    resetRun();                   // resolve any popup the old run was blocked on
    introCard();                  // the warm one-tap way in
    welcomeSplash();              // …and Joy herself, before anything is asked for
  }
  const f = $('fld');
  if(f) f.focus();
}

/* ---------- shell wiring ---------- */
/* The launchpad is the real digibank first page: six tiles, and the credential form is one
   tap deeper behind the "Log in" tile. So #lgBtn opens the form rather than logging in;
   #lgSubmit is what actually enters digibank. openLoginForm/closeLoginForm hide the hero
   photo + tile grid too, so the form is the only thing on screen while it is open; the
   launchpad returns when the customer taps the form's back arrow. */
const lgFormEl   = $('lgForm');
const lgWrapEl   = document.querySelector('#scrLogin .lgwrap');
const lgHeroEl   = lgWrapEl && lgWrapEl.querySelector('.lghero');
const lgMenuEl   = lgWrapEl && lgWrapEl.querySelector('.lgmenu');
function openLoginForm(){
  if(!lgFormEl || !lgWrapEl) { showHome(); return; }
  if(lgHeroEl) lgHeroEl.style.display = 'none';
  if(lgMenuEl) lgMenuEl.style.display = 'none';
  lgFormEl.hidden = false;
  const u = $('lgUser'); if(u) u.focus();
}
function closeLoginForm(){
  if(!lgFormEl || !lgWrapEl) return;
  lgFormEl.hidden = true;
  if(lgHeroEl) lgHeroEl.style.display = '';
  if(lgMenuEl) lgMenuEl.style.display = '';
}
/* Go straight to the User ID / PIN form: used at boot and after a logout, so a signed-out
   customer always lands on the form (matching iggy), not on the launchpad tile grid. */
function gotoLoginForm(){ showLogin(); openLoginForm(); }

const lgBtn = $('lgBtn');
if(lgBtn) lgBtn.addEventListener('click', ev=>{ ev.preventDefault(); openLoginForm(); });

const lgSubmit = $('lgSubmit');
if(lgSubmit) lgSubmit.addEventListener('click', ev=>{ ev.preventDefault(); showHome(); });

const lgFormBack = $('lgFormBack');
if(lgFormBack) lgFormBack.addEventListener('click', ev=>{ ev.preventDefault(); closeLoginForm(); });

/* Every other launchpad tile is a real digibank entry point that this prototype does not
   implement. Same honesty rule as the rest of the build: say so, don't leave a dead control. */
document.querySelectorAll('.lgmenu-tile[data-go="alt"]').forEach(b=>{
  b.addEventListener('click', ev=>{ ev.preventDefault(); showAltLoginNotice(); });
});

const lgFace = $('lgFace');
if(lgFace) lgFace.addEventListener('click', ev=>{ ev.preventDefault(); showAltLoginNotice(); });

const lgStarted = $('lgStarted');
if(lgStarted) lgStarted.addEventListener('click', ev=>{ ev.preventDefault(); showSignupNotice(); });

/* Trailing validity tick. It only appears once the field actually matches the real Singapore
   NRIC shape (letter + 7 digits + letter), so the cue carries information instead of being
   decoration that is always on. */
const lgUserEl = $('lgUser'), lgUserOk = $('lgUserOk');
if(lgUserEl && lgUserOk){
  const markValid = () => lgUserOk.classList.toggle('on', /^[STFGM]\d{7}[A-Z]$/i.test(lgUserEl.value.trim()));
  lgUserEl.addEventListener('input', markValid);
  markValid();
}

/* "Forgot User ID or PIN?" — the two links now read like the real digibank form. */
['lgForgotUser','lgForgotPin'].forEach(id=>{
  const el = $(id);
  if(el) el.addEventListener('click', ev=>{ ev.preventDefault(); showForgotNotice(); });
});

/* The digibank appbar's single top-right control is LOG OUT (matching iggy). It used to open
   Joy, but Joy is reachable from the home CTA and the Pay & Transfer row, which is the real
   customer journey anyway - and a button labelled "Log out" that opens a chat is a lie.
   Signed-out always lands back on the User ID / PIN form. */
const appBack = $('appBack');
if(appBack) appBack.addEventListener('click', ev=>{ ev.preventDefault(); gotoLoginForm(); });

/* Bottom-tab nav (Home / Pay & Transfer / digiWealth / Apply / More), the real digibank
   navigation. Home and Pay & Transfer render real content; the other three say plainly
   that they aren't built rather than faking a screen the prototype doesn't have. */
document.querySelectorAll('#tabnav .tab').forEach(b=>{
  b.addEventListener('click', ev=>{
    ev.preventDefault();
    const t = b.dataset.tab;
    if(t==='home')      showHome();
    else if(t==='pay')  showPay();
    else { setTab(t); showComingSoon(t); }
  });
});

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

/* Boot: open straight on the User ID / PIN form, matching iggy. The launchpad (Jewel/Changi
   hero + 6 tiles) is still in the DOM and reachable via the form's back arrow, but the form
   is the default — same one-tap path a real digibank customer takes. */
gotoLoginForm();
