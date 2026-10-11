/* Headless check of the Joy DBS shell and the in-transcript cards.
   Run:  NODE_PATH=<workspace>/node_modules node shell.test.js            */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const dir = __dirname;
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
const css  = fs.readFileSync(path.join(dir, 'styles.css'), 'utf8');
// Loaded in the same order index.html uses, the files share globals.
// icons.js MUST be here: ui.js and shell.js call icon() at load time, so without it the
// whole bundle throws during boot.
const files = ['translations.js','icons.js','ui.js','personalization.js','scenarios.js','stt.js',
               'live.js','shell.js'];

// The OLD brand name is gone; the product is Joy / JoyPilot now.
// Two things make this check stricter than it first looks:
//   1. It scans the WHOLE frontend FOLDER, not a hand-kept list of app files. The folder is
//      what gets deployed, so a stray mention in serve.py or a test file is served bytes too
//      -- and a scanner greps the bytes, not the DOM. A hand-kept list would silently miss
//      any new file someone drops in.
//   2. The needle is ASSEMBLED AT RUNTIME, so this test file does not itself contain the
//      string it is looking for. A test that ships the forbidden word cannot assert the word
//      is absent -- the same trap as explaining a removal by quoting what was removed.
// Matched case-SENSITIVELY and on purpose: the technical global `window.AUNTIEJOY_API_BASE` is
// a configuration contract between the page and the backend (and `AUNTIEJOY_*` are environment
// variable names), not branding -- renaming those would break the deploy for no user-visible
// gain, so they are deliberately out of scope for this check.
const OLD_BRAND = new RegExp('Auntie' + 'Joy');
const frontendHasOldBrand = () =>
  fs.readdirSync(dir)
    .filter(f => /\.(js|mjs|css|html|py|json)$/.test(f))
    .some(f => OLD_BRAND.test(fs.readFileSync(path.join(dir, f), 'utf8')));

let fails = 0;
const ok = (name, cond, extra) => {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + name + (cond ? '' : '   → ' + (extra === undefined ? '' : extra)));
  if (!cond) fails++;
};

const problems = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => problems.push('jsdomError: ' + e.message));
vc.on('error', (...a) => problems.push('console.error: ' + a.join(' ')));

// Drop the <script src> tags: we inject the very same files ourselves, in the same order.
// They must go in as real <script> elements (not one big eval) because eval code traps its
// own top-level `const`/`let` in a throwaway lexical scope, whereas a real script puts them
// in the global lexical scope, which is how ui.js's `iface`, `auntie`, `fill` … are visible
// to the files loaded after it in a browser.
const srcFree = html.replace(/<script\b[^>]*\bsrc=[^>]*>\s*<\/script>/gi, '');
const dom = new JSDOM(srcFree, {
  runScripts: 'dangerously',
  url: 'http://127.0.0.1:8077/', virtualConsole: vc,
});
const { window } = dom;
const doc = window.document;

// No network in this test: any fetch is a bug in the paths we exercise.
let fetchCalls = 0;
window.fetch = () => { fetchCalls++; return Promise.reject(new Error('no network in test')); };

// Detect an actual navigation/reload attempt.
let unloadCount = 0;
window.addEventListener('beforeunload', () => { unloadCount++; });
window.addEventListener('unload', () => { unloadCount++; });

const errors = [];
window.addEventListener('error', e => errors.push(String(e.message)));

let bootOK = true;
for (const f of files) {
  const before = problems.length;
  const s = doc.createElement('script');
  s.textContent = fs.readFileSync(path.join(dir, f), 'utf8');
  doc.body.appendChild(s);            // classic scripts run synchronously on insert
  if (problems.length > before) {
    console.log('  FAIL  ' + f + ' threw during boot → ' + problems[problems.length - 1]);
    bootOK = false; break;
  }
}
if (!bootOK) process.exit(1);

const $ = id => doc.getElementById(id);
const scrOn = id => $(id).classList.contains('on');
const chat = $('chat');
// `iface`, `auntie`, `fill` … are top-level `const`, so they are global *lexical* bindings,
// not properties of `window`, same as in a browser. Reach them the way a later <script>
// would: by evaluating an expression in global scope.
const ev = code => window.eval(code);

console.log('\n=== 1. LANDING IS THE DBS LOGIN (nothing auto-runs) ===');
ok('login screen is active', scrOn('scrLogin'));
ok('chat transcript is empty at boot', chat.children.length === 0, chat.children.length + ' children');
// The A-H rail is GONE - removed, not relocated. It used to sit on the Execution-path page of
// the explainer overlay; the owner asked for it to be taken out, so it was. These two checks
// stop it creeping back, and they pin the CONSEQUENCE rather than hiding it: `.sbtn` is the
// class scenarios.js binds every scripted scenario with (`querySelectorAll('.sbtn')`), so an
// empty `.sbtn` set means the 22 rehearsal branches have no in-UI trigger any more and the app
// runs through the live /turn flow only. If someone re-adds the rail, this fails - which is the
// point, because the trade-off should be a decision, not an accident.
ok('the A-H rail is gone from the document entirely', !$('rail'));
ok('no scenario-rail buttons (.sbtn) exist anywhere',
   doc.querySelectorAll('.sbtn').length === 0,
   doc.querySelectorAll('.sbtn').length + ' .sbtn');
ok('no backend call was made at boot', fetchCalls === 0, fetchCalls + ' calls');

console.log('\n=== 2. DEVICE MOCKUP ===');
ok('device + screen present', !!$('device') && !!$('deviceScreen'));
ok('status bar + island present', !!doc.querySelector('.statusbar') && !!doc.querySelector('.island'));
ok('all three screens live inside the device screen',
   ['scrLogin','appwin','scrJoy'].every(s => $('deviceScreen').contains($(s))));
ok('inspector sits outside the device', !$('device').contains($('agents')));
ok('only one screen is active at a time', ['scrLogin','appwin','scrJoy'].filter(scrOn).length === 1);

console.log('\n=== 3. LOGIN FORM (boot) -> DBS HOME -> ASK JOY TO PAY ===');
// The User ID / PIN form IS the login screen. The launchpad (hero photo + six tiles) and the
// form's back arrow were deleted on 2 Oct, so boot lands on the form and LOGIN goes straight
// into digibank -- there is no tile grid round trip any more.
ok('login form is OPEN at boot (form-first)',
   $('lgForm').hidden === false && scrOn('scrLogin'));
ok('no launchpad remains behind the form (hero + tiles deleted 2 Oct)',
   !doc.querySelector('#scrLogin .lghero') && !doc.querySelector('#scrLogin .lgmenu'));
ok('no back arrow on the form (nothing to go back to)', !$('lgFormBack'));

console.log('\n=== 3b. THE LOGIN FORM IS INTACT (and the DEMO labelling stays) ===');
// THE FORM WAS DELETED ON 9 OCT AND RESTORED THE SAME DAY. Both halves of that are worth
// recording, because the reasons are in direct tension and the next person will ask.
//
// WHY IT WAS DELETED: on 9 Oct the .xyz registry put dbsjoy.xyz on `server hold` -- which
// deletes the name from the DNS zone, so no resolver anywhere can find it -- and Google Safe
// Browsing served a "Dangerous site" interstitial. Both were triggered by the same thing: a
// pixel-faithful DBS digibank User ID + PIN form on a domain that was ONE DAY OLD. Nothing was
// ever collected (the fields are prefilled samples, the form has no action and no method, and no
// value is read, stored or transmitted). It made no difference -- a page that ASKS for a bank
// PIN does not get the benefit of the doubt, and a classifier cannot tell a demo from a phish.
//
// WHY IT IS BACK: the owner asked for it back, explicitly, having been told the above. That is
// their call to make and it is now the intended state, so this section guards the FORM, not its
// absence. What must NOT come back is the part that was impersonation rather than simulation:
// the real DBS Bank Ltd company registration number. That stays out, and is asserted below.
ok('the User ID field is present', !!$('lgUser'));
ok('the PIN field is present and masked', !!$('lgPin') && $('lgPin').type === 'password');
ok('both fields are prefilled with obvious samples, never real credentials',
   $('lgUser').value === 'S1234567A' && $('lgPin').value === 'SAMPLEPASSWORD1234');
ok('the form has no action and no method -- it cannot post anywhere',
   !$('lgForm').getAttribute('action') && !$('lgForm').getAttribute('method'));
ok('the LOGIN button is the way in',
   !!$('lgSubmit') && /LOGIN/i.test($('lgSubmit').textContent));
ok('the PIN-recovery links are present', !!$('lgForgotUser') && !!$('lgForgotPin'));
ok('the Face ID and sign-up affordances are present', !!$('lgFace') && !!$('lgStarted'));
ok('the real DBS company number is NOT quoted anywhere -- the one line we do not restore',
   !/196800306E/.test(html));
ok('the page title names the product and says prototype',
   /joy/i.test(doc.title) && /prototype/i.test(doc.title), doc.title);
ok('no old-brand ("Auntie" + "Joy") mention survives anywhere in the deployed frontend folder',
   !OLD_BRAND.test(html) && !frontendHasOldBrand());
ok('a DEMO tag sits beside the logo on the entry screen',
   !!doc.querySelector('#scrLogin .lgdemotag'));
ok('a DEMO tag travels with the DBS chrome inside the phone',
   !!doc.querySelector('#appwin .appdemo'));
ok('the form carries a note saying these are sample credentials',
   /sample credentials/i.test($('scrLogin').textContent));

$('lgSubmit').dispatchEvent(new window.MouseEvent('click', { bubbles:true }));
ok('LOGIN dismissed, app window active', !scrOn('scrLogin') && scrOn('appwin'));
// Home is the real-digibank dashboard: red cashback hero, 5 quick-action tiles, the ONE
// Joy CTA, the deposits card, and a 5-tab bottom nav.
ok('red cashback hero present', !!$('appBody').querySelector('.hero'));
ok('five quick-action tiles present', $('appBody').querySelectorAll('.qbtn').length === 5,
   $('appBody').querySelectorAll('.qbtn').length + ' tiles');
ok('deposits card present', !!$('appBody').querySelector('.dep'));
ok('deposits quotes the account the backend actually resolves',
   /DBS Savings ending 1234/.test($('appBody').querySelector('.dep-acc').textContent),
   $('appBody').querySelector('.dep-acc').textContent);
ok('exactly ONE Joy entry point on the home screen',
   $('appBody').querySelectorAll('[data-go="joy"]').length === 1,
   $('appBody').querySelectorAll('[data-go="joy"]').length + ' entry points');
ok('bottom tab nav has all five tabs', doc.querySelectorAll('#tabnav .tab').length === 5);
ok('Home is the tab that is highlighted',
   doc.querySelector('#tabnav .tab.on').dataset.tab === 'home',
   doc.querySelector('#tabnav .tab.on').dataset.tab);
// The bottom nav actually switches screens, and moves its own highlight with it.
doc.querySelector('#tabnav .tab[data-tab="pay"]').dispatchEvent(new window.MouseEvent('click', { bubbles:true }));
ok('Pay & Transfer tab opens the Pay & Transfer list',
   !!$('appBody').querySelector('.payhero') && scrOn('appwin'));
ok('and the Pay tab is the one highlighted',
   doc.querySelector('#tabnav .tab.on').dataset.tab === 'pay',
   doc.querySelector('#tabnav .tab.on').dataset.tab);
ok('Joy hero card is present (Pay with Joy + Talk to Joy)',
   !!$('appBody').querySelector('.payhero-btn') &&
   $('appBody').querySelector('.payhero-btn').dataset.go === 'joy',
   $('appBody').querySelector('.payhero-btn') ? $('appBody').querySelector('.payhero-btn').dataset.go : 'missing');
ok('Talk to Joy is a FRESH entry to the chat (data-blank="1") - without it showJoy() renders an empty transcript',
   $('appBody').querySelector('.payhero-btn').dataset.blank === '1',
   $('appBody').querySelector('.payhero-btn').dataset.blank || 'no data-blank');
ok('the three manual options are elevated cards',
   $('appBody').querySelectorAll('.paycard').length === 3,
   $('appBody').querySelectorAll('.paycard').length + ' cards');
ok('the Recent Payees section lists the four saved payees',
   $('appBody').querySelectorAll('.recent-av:not(.recent-add)').length === 4,
   $('appBody').querySelectorAll('.recent-av').length + ' avatars');
ok('no OS emoji in the banking chrome', !/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u.test($('appBody').textContent));
doc.querySelector('#tabnav .tab[data-tab="home"]').dispatchEvent(new window.MouseEvent('click', { bubbles:true }));
ok('Home tab returns to the dashboard', !!$('appBody').querySelector('.joy-cta'));
ok('and the Home tab is highlighted again',
   doc.querySelector('#tabnav .tab.on').dataset.tab === 'home');

$('appBody').querySelector('.joy-cta').dispatchEvent(new window.MouseEvent('click', { bubbles:true }));
ok('Joy chat active', scrOn('scrJoy'));
ok('intro card rendered', !!chat.querySelector('.intro'));
ok('intro offers suggestion chips', chat.querySelectorAll('.chip').length >= 3,
   chat.querySelectorAll('.chip').length + ' chips');
ok('transcript contains ONLY the intro card', chat.children.length === 1, chat.children.length + ' children');
// The intro card is drawn BEFORE the first turn, so no trusted name exists yet. It must NOT
// invent one, addressing the customer as "Auntie" would read like the assistant talking to
// itself. The body states the safety contract plainly instead, and carries no name slot.
const greet = () => (chat.querySelector('.intro .itxt')?.textContent || '').replace(/\s+/g, ' ').trim();
// British spelling is the house style for user-visible prose (see MEGA-IMPLEMENTATION-SPEC §4.8):
// authorisation, authorise, personalisation, recognised. This assertion tracks the copy, so it
// moved from "authorize" to "authorise" with it.
ok('intro states the review-and-authorise safety contract',
   /review and authorise before any money moves/.test(greet()), greet());
ok('intro never invents the customer name', !/Auntie|Mei Ling/.test(greet()), greet());

console.log('\n=== 4. RESET: resetPanels() KEEPS THE TRANSCRIPT, resetRun() CLEARS IT ===');
const beforePanels = chat.children.length;
window.resetPanels();
ok('resetPanels() keeps the transcript', chat.children.length === beforePanels,
   chat.children.length + ' vs ' + beforePanels);
ok('resetPanels() did clear the agent panel', $('agents').querySelector('.placeholder') !== null);
window.resetRun();
ok('resetRun() clears the transcript', chat.children.length === 0, chat.children.length + ' children');

console.log('\n=== 5. DBS PAYMENT RECEIPT CARD ===');
window.receiptCard({ stamp:'23 Sep 2026, 09:41 pm', amount:'SGD 50.00',
  to:'Mei Tan · ••••7788', from:'DBS Savings · ending 1234', ref:'txn_002' });
const rc = doc.querySelector('.rcpt');
ok('receipt card rendered', !!rc);
ok('carries the real DBS logo', !!rc.querySelector('.rhead img.dbslogo'));
ok('states the payment completed', /Payment completed/.test(rc.querySelector('.rstatus').textContent));
ok('shows the amount prominently', rc.querySelector('.ramt').textContent === 'SGD 50.00',
   rc.querySelector('.ramt').textContent);
ok('itemises To / From / Reference / Status',
   [...rc.querySelectorAll('.rr .k')].map(e=>e.textContent).join('|') === 'To|From|Reference|Status');
ok('carries the transaction reference', /txn_002/.test(rc.textContent));

console.log('\n=== 6. CLARIFICATION CARD MUST NOT RELOAD, AND ADDS NO WORDING ===');
window.resetRun();
const card = window.clarifyCard('Sorry Auntie, I didn’t quite catch that.', null);
const btn = card.querySelector('.cbtn');
ok('retry button is type="button" (cannot implicitly submit)', btn.getAttribute('type') === 'button');
ok('retry button is not inside a form', card.closest('form') === null);
const lenBefore = chat.children.length;
btn.dispatchEvent(new window.MouseEvent('click', { bubbles:true, cancelable:true }));
ok('no navigation / reload triggered', unloadCount === 0, unloadCount + ' unload events');
ok('card still present after retry', doc.contains(card));
ok('retry focused the input', doc.activeElement === $('fld'),
   'activeElement=' + (doc.activeElement && doc.activeElement.id));
// Our invariant: the client composes NO customer-facing sentence of its own, so a retry
// must not append a chat line, the wording on the card is the backend's own line.
ok('retry appended no client-written sentence', chat.children.length === lenBefore,
   chat.children.length + ' vs ' + lenBefore);
ok('retry marked itself done', card.dataset.retried === '1');
btn.dispatchEvent(new window.MouseEvent('click', { bubbles:true, cancelable:true }));
ok('a second click does not queue another turn', chat.children.length === lenBefore);
ok('still no reload after double click', unloadCount === 0);

console.log('\n=== 7. removeLastIfaceBubble() REMOVES ONLY THE BUBBLE ===');
window.resetRun();
window.clarifyCard('x', null);              // a card (not a bubble), must survive
ev("iface('a backend line')");              // a bubble, must be removed
const before = chat.children.length;
const removed = window.removeLastIfaceBubble();
ok('removed exactly one row', chat.children.length === before - 1,
   chat.children.length + ' vs ' + before);
ok('reported success', removed === true);
ok('the clarification card survived', chat.querySelector('.ccard') !== null);

console.log('\n=== 8. AGENT VERDICT CARDS ===');
const GUARDRAIL = { engine:'live', decision:'ALLOW', risk_category:'SAFE_PAYMENT_REQUEST',
  risk_level:'LOW', detected_language:'EN', message_key:'CONTINUE',
  developer_reason:'Ordinary send-money request naming a recipient and amount.' };
const INTENT = { engine:'live', intent:'PAYMENT', confidence:0.95, detected_language:'EN',
  reason:'Customer initiates a new transfer.', threshold:0.75 };
const n0 = $('agents').children.length;
window.agent('GUARDRAILS AGENT', GUARDRAIL);
window.agent('INTENT CLASSIFIER', INTENT);
const gcard = $('agents').children[n0], icard = $('agents').children[n0+1];
ok('guardrails card is a verdict card', gcard.classList.contains('verdict'));
ok('guardrails leads with ALLOW', /ALLOW/.test(gcard.querySelector('.vbadge').textContent));
ok('guardrails badge coloured ok', gcard.querySelector('.vbadge').classList.contains('ok'));
ok('guardrails shows RISK LOW', /RISK LOW/.test(gcard.querySelector('.vtag').textContent));
ok('guardrails quotes the agent reason', /Ordinary send-money request/.test(gcard.textContent));
ok('guardrails tucks raw JSON behind a disclosure', !!gcard.querySelector('details.raw'));
ok('intent card is a verdict card', icard.classList.contains('verdict'));
ok('intent leads with PAYMENT', /PAYMENT/.test(icard.querySelector('.vbadge').textContent));
ok('intent shows 95% confidence', /95%/.test(icard.querySelector('.vbar-t').textContent));
ok('intent marks it ABOVE threshold', /ABOVE THRESHOLD/.test(icard.querySelector('.vtag').textContent));
ok('intent draws the bar at 95%',
   icard.querySelector('.vtrack i').getAttribute('style').includes('width:95%'));
ok('intent draws the 0.75 threshold marker at 75%',
   icard.querySelector('.vtrack u').getAttribute('style').includes('left:75%'));
window.agent('INTENT CLASSIFIER', Object.assign({}, INTENT, { confidence:0.61 }));
const low = $('agents').children[n0+2];
ok('low-confidence intent warns instead of passing',
   low.querySelector('.vbadge').classList.contains('warn') &&
   /BELOW THRESHOLD/.test(low.querySelector('.vtag').textContent));
window.agent('GUARDRAILS AGENT', Object.assign({}, GUARDRAIL,
  { decision:'BLOCK', risk_level:'HIGH', risk_category:'COERCION_SUSPECTED' }));
const blk = $('agents').children[n0+3];
ok('blocked guardrail goes red', blk.querySelector('.vbadge').classList.contains('stop'));
window.agent('MOCK LEDGER', { txn:'txn_002' });
const plain = $('agents').children[n0+4];
ok('other agents still render as plain payload cards',
   !plain.classList.contains('verdict') && !!plain.querySelector('pre'));

console.log('\n=== 9. REAL DBS LOGO ASSETS ===');
for (const f of ['dbs-logo.png','dbs-logo-white.png','dbs-mark.png']) {
  ok('asset present: assets/' + f, fs.existsSync(path.join(dir, 'assets', f)));
}

console.log('\n=== 11. THE EARS (stt.js) ===');
// `class SttClient` is a top-level class declaration, so (like `const`) it is a global
// LEXICAL binding, not a property of window, reach it the way live.js does.
ok('SttClient is defined', ev("typeof SttClient") === 'function');
// jsdom has no AudioContext, so the client must honestly report that it cannot run here.
// That same flag is what makes live.js fall back to the browser recogniser.
ok('reports itself unsupported with no audio device', ev("SttClient.supported()") === false);
ok('toggle maps to an ASR engine (EN default)', ev("sttLanguage()") === 'en');
ev("document.querySelector('#langtog .lt[data-l=\\'BM\\']').click()");
ok('BM switches to the multilingual engine', ev("sttLanguage()") === 'bm');
ev("document.querySelector('#langtog .lt[data-l=\\'EN\\']').click()");
const wsUrl = ev("sttWsUrl()");
// API_BASE defaults to the PAGE's host (the jsdom page is http://127.0.0.1:8077/) on the
// backend's port. If you change the default port in scenarios.js, update it here and in README.
ok('socket URL is derived from API_BASE', wsUrl === 'ws://127.0.0.1:8078/ws/stt', wsUrl);
ok('API_BASE follows the page host + backend port (no hardcoded 127.0.0.1)',
   ev("API_BASE") === 'http://127.0.0.1:8078', ev("API_BASE"));
// Convert inside the page realm and return a plain string, a jsdom ArrayBuffer is not
// safely viewable from this realm.
const pcm = ev("Array.from(new Int16Array(floatToPcm16(new Float32Array([0, 1, -1])))).join(',')");
ok('PCM is 16-bit signed, full-scale mapped correctly', pcm === '0,32767,-32768', pcm);

console.log('\n=== 12. STYLESHEET COVERS EVERY CLASS THE MARKUP USES ===');
// The composer once shipped with `.cinput` / `.cinrow` / `.langtog` present in the HTML and NO
// rules in styles.css at all. Nothing threw, so every other check here stayed green while the
// layout silently collapsed. Compare the two sets directly instead of trusting a screenshot.
const cssSelectors = css.replace(/\/\*[\s\S]*?\*\//g, '')   // comments
  .replace(/url\([^)]*\)/g, '')                             // asset paths (.png/.svg/...)
  .replace(/"[^"]*"|'[^']*'/g, '');                         // quoted property values
const cssClasses = new Set();
for (const m of cssSelectors.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)) cssClasses.add(m[1]);

const markupClasses = new Set();
const jsFiles = fs.readdirSync(dir).filter(f => f.endsWith('.js') && !f.endsWith('.test.js'));
for (const src of [html, ...jsFiles.map(f => fs.readFileSync(path.join(dir, f), 'utf8'))]) {
  for (const m of src.matchAll(/class\s*=\s*["']([^"']*)["']/g)) {
    for (const tok of m[1].split(/\s+/)) {
      // Only literal class names. `class="ic${cls ? ' ' + cls : ''}"` splits into fragments
      // that are not class names at all, so require the exact CSS identifier shape.
      if (/^-?[_a-zA-Z][\w-]*$/.test(tok)) markupClasses.add(tok);
    }
  }
  // State classes toggled from JS are styling hooks too. Take ONLY the first argument:
  // `classList.toggle('light-bar', id==='scrJoy')` has a boolean second argument that is not
  // a class, and a naive scan reports "scrJoy" as an unstyled class forever.
  for (const m of src.matchAll(/classList\s*\.\s*(?:add|remove|toggle|contains)\s*\(\s*(["'])([^"']*)\1/g)) {
    for (const tok of m[2].split(/\s+/)) {
      if (/^-?[_a-zA-Z][\w-]*$/.test(tok)) markupClasses.add(tok);
    }
  }
}
const unstyled = [...markupClasses].filter(c => !cssClasses.has(c)).sort();
ok('every class used in markup has a rule in styles.css', unstyled.length === 0,
   'unstyled: ' + unstyled.join(', '));
ok('scanned a meaningful number of classes', markupClasses.size > 60,
   markupClasses.size + ' classes found');

console.log('\n=== 9c. VOICE MODE IS DISCREET (nothing announces it) ===');
// Voice Mode is a backstage rehearsal switch for demoing the unverified-speaker escalation.
// If it announced itself, it would have to be hidden again before a demo, so it must be
// invisible. This test exists because discreetness is very easy to regress by accident.
(function(){
  const ui   = fs.readFileSync(path.join(dir,'ui.js'),'utf8');
  const chat = $('chat');
  const toggle = $('voiceToggle');
  ok('the voice mode switch exists', !!toggle);
  ok('it defaults to OFF', toggle.checked === false);

  // Toggling must not change a single thing a judge could see.
  const beforeBody = doc.body.className;
  const beforeState = $('stState') ? $('stState').textContent : '';
  toggle.checked = true;
  toggle.dispatchEvent(new window.MouseEvent('change', { bubbles:true }));
  ok('turning it on adds no body class', doc.body.className === beforeBody,
     'now: ' + doc.body.className);
  ok('turning it on writes nothing to the state readout',
     (!$('stState') || $('stState').textContent === beforeState),
     $('stState') ? $('stState').textContent : '');
  ok('turning it on emits no system/audit line',
     !/VOICE MODE ON/i.test(chat.textContent));
  ok('the switch reads as a plain setting, not a warning',
     !/alert|warning|danger/i.test(toggle.closest('label').className));

  // And the CSS must not re-introduce a tell.
  ok('no voicemode body-class rule exists', !/body\.voicemode/.test(css));
  ok('the toggle has no distinct checked colour of its own',
     !/\.voicetoggle\s+input:checked\s*\{[^}]*background:(?!#?var\(--brand\))/.test(css) ||
     /\.voicetoggle input:checked\{background:var\(--brand\)\}/.test(css));
  ok('toggling does not call state() or sysLine()', (function(){
    const i = ui.indexOf('voiceToggle.onchange');
    if (i < 0) return false;
    // Read to the end of the handler by brace balance, not by the first '}', a naive
    // slice lands inside the comment and would pass or fail for the wrong reason.
    const open = ui.indexOf('{', ui.indexOf('=>', i));
    let depth = 0, end = open;
    for (let k = open; k < ui.length; k++) {
      if (ui[k] === '{') depth++;
      else if (ui[k] === '}') { depth--; if (depth === 0) { end = k; break; } }
    }
    const handler = ui.slice(open, end + 1)
      .replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');   // strip comments first
    return !/\bstate\s*\(/.test(handler) && !/\bsysLine\s*\(/.test(handler);
  })());
  ok('the intent is documented, so nobody re-adds a banner',
     /DISCREET BY DESIGN|silent/i.test(ui));
  toggle.checked = false;
  toggle.dispatchEvent(new window.MouseEvent('change', { bubbles:true }));
})();

/* ==========================================================================
   9d. THE RE-LAYOUT: TOP BAR, TWO PANELS, HERO PAIR, EXPLAINER OVERLAY
   Every check here guards a DELIBERATE move rather than an accident, and the
   recurring theme is "taken off the screen" vs "deleted". The state readout, the
   audit timeline and the requirements list were RELOCATED; the A-H rail was later
   DELETED outright. A tidy-up that genuinely deletes one of the relocated blocks
   has to fail here, because on the screen the two look identical.
   ========================================================================== */
console.log('\n=== 9d. TOP BAR, TWO PANELS, HERO PAIR, EXPLAINER OVERLAY ===');
(function(){
  const uiSrc    = fs.readFileSync(path.join(dir,'ui.js'),'utf8');
  const shellSrc = fs.readFileSync(path.join(dir,'shell.js'),'utf8');

  // ---- the top bar ----
  ok('the dead "DBS · DCTA · SCENARIO 1" chip is gone', !/DBS · DCTA · SCENARIO 1/.test(html));
  ok('the Scenarios button is gone', !$('railToggle'));
  ok('the old "DBS app" button is gone', !$('restartBtn'));
  ok('the Personalisation button is gone', !$('personalizeBadge'));
  ok('a Reset button replaces it', !!$('resetBtn'));
  ok('Reset is wired to resetDemo(), not to a bare navigation',
     /resetBtn\.addEventListener\('click'[\s\S]{0,80}resetDemo\(\)/.test(shellSrc));
  ok('resetDemo() clears the run BEFORE it navigates',
     /function resetDemo\(\)\{[\s\S]{0,120}resetRun\(\)/.test(shellSrc));
  ok('resetDemo() restarts the chat in place when the chat is the current screen',
     /classList\.contains\('on'\)[\s\S]{0,40}showJoy\(true\)/.test(shellSrc));
  // HOME sits next to Reset, and the two are NOT the same control. Reset is context-aware
  // (inside the chat it restarts in place), so it cannot be used as "take me back to the start".
  // Home is the unconditional one: clear the run and land on the login screen, from anywhere.
  ok('a Home button sits in the top bar next to Reset',
     !!$('homeBtn') && $('resetBtn').previousElementSibling === $('homeBtn') &&
     $('homeBtn').classList.contains('ghostbtn') && $('resetBtn').classList.contains('ghostbtn'));
  ok('Home is wired to homeDemo()', /homeBtn\.addEventListener\('click'[\s\S]{0,80}homeDemo\(\)/.test(shellSrc));
  ok('homeDemo() clears the run and then goes to the login screen, in that order',
     /function homeDemo\(\)\{[\s\S]{0,160}resetRun\(\)[\s\S]{0,80}gotoLoginForm\(\)/.test(shellSrc));
  // The two long status chips are gone. They described the demo's LIMITS ("DEMO ENVIRONMENT ·
  // SIMULATED APPROVAL · SYNTHETIC LEDGER") rather than its own contents, and the green one
  // claimed five agents against an architecture that has four. THREE buttons replaced them, each
  // named for the page it opens, so the control names its destination instead of its caveat.
  // A fourth button ("What's real") was removed on request: it is not a standalone tab any more,
  // and its content is now the closing section of "How it works". Asserted as a SET: the order is
  // a presentation choice, the membership is the contract.
  const expBtns = [...doc.querySelectorAll('.badges [data-explain]')];
  ok('the two status chips are gone, replaced by three page buttons',
     !$('demoBadge') && !$('agentsBadge') && expBtns.length === 3,
     expBtns.length + ' buttons');
  ok('every page button is a real BUTTON, not a div',
     expBtns.every(b => b.tagName === 'BUTTON'));
  ok('the three buttons open arch / how / path, one each',
     expBtns.map(b => b.dataset.explain).sort().join(',') === 'arch,how,path',
     expBtns.map(b => b.dataset.explain).join(','));
  ok('there is no standalone "What\'s real" button any more',
     !expBtns.some(b => b.dataset.explain === 'real'),
     expBtns.map(b => b.dataset.explain).join(','));
  ok('no button claims a COUNT of agents or controls in the top bar',
     !expBtns.some(b => /\bagents?\b|\bcontrols?\b|\d/i.test(b.textContent)),
     expBtns.map(b => b.textContent.trim()).join(' | '));
  ok('the three buttons are visually distinct (three hues)',
     ['arch','how','path'].every(c => new RegExp('^\\.bdg\\.' + c + '\\{', 'm').test(css)));

  // ---- the inspector is two panels, not four ----
  const panels = doc.querySelectorAll('.ins > .panel');
  ok('the inspector is exactly two panels', panels.length === 2, panels.length + ' panels');
  ok('panel 1 is the control chain',
     /END-TO-END CONTROL CHAIN/.test(panels[0].querySelector('h3').textContent));
  ok('panel 2 is the typed agent contracts',
     /TYPED AGENT CONTRACTS/.test(panels[1].querySelector('h3').textContent));
  // Checked against the panel HEADINGS, not the raw HTML: the old name is still mentioned in
  // the comments that explain where the block went, and a substring match over the whole file
  // would fail on its own documentation.
  ok('no panel is headed STATE · OUTCOME · EVIDENCE any more',
     ![...doc.querySelectorAll('.panel h3')]
        .some(h => /STATE · OUTCOME · EVIDENCE/.test(h.textContent)));

  // ---- relocated, NOT deleted ----
  ok('the audit timeline is still on screen, now inside the control-chain panel',
     panels[0].contains($('audit')) && !!$('auditCnt') && !!$('auditMore'));
  ok('every live state readout survived the move',
     ['stState','stDraft','stOut','stLedger','stSig','stLang','stHeard','stPers',
      'vengRow','vengVal','ttsProvRow','ttsProvVal'].every(id => !!$(id)));
  ok('the SPEAKING row is still visible on the main screen', panels[1].contains($('ttsProvRow')));
  ok('the requirements-coverage list still exists, one click away',
     !!$('reqs') && !!$('explain') && $('explain').contains($('reqs')));
  ok('and it still uses the challenge wording verbatim, not a paraphrase',
     /Zero-UI Multi-Intent Transaction Parsing/.test(uiSrc) &&
     /Deterministic Human-in-the-Loop Biometric Authorization/.test(uiSrc));

  // ---- the hero pair ----
  const pair = doc.querySelector('.heropair');
  ok('the device sits inside a hero pair', !!pair && pair.contains($('device')));
  ok('the Joy mascot is in the pair', !!pair && !!pair.querySelector('.joymascot'));
  ok('the mascot ships WebP with a PNG fallback',
     !!doc.querySelector('.joymascot source[srcset$="joy-mascot.webp"]') &&
     !!doc.querySelector('.joymascot img[src$="joy-mascot-560.png"]'));
  ok('the mascot assets are actually on disk',
     fs.existsSync(path.join(dir,'assets','joy-mascot.webp')) &&
     fs.existsSync(path.join(dir,'assets','joy-mascot-560.png')) &&
     fs.existsSync(path.join(dir,'assets','joy-mascot.png')));
  ok('the mascot is shown only while the inspector is hidden (#studio.appmode)',
     /#studio\.appmode \.joymascot\{display:block\}/.test(css) &&
     /^\.joymascot\{display:none/m.test(css));
  ok('the device is no longer centred by its own auto margins (that would split it from Joy)',
     !/\.stage \.device\{margin-inline:auto\}/.test(css));
  // Below 900px the page stops being viewport-height-bound, so the hero pair's height becomes
  // indefinite - and a percentage inside min() is then invalid at used-value time, silently
  // falling back to the image's own 720px intrinsic height. Measured: Joy rendered TALLER than
  // the phone. The small-screen rule must therefore use a unit that always resolves.
  ok('the small-screen mascot height is definite (vw), not an unresolvable percentage',
     /@media\(max-width:900px\)\{[\s\S]*?\.joymascot\{height:min\(48vw,375px\)\}/.test(css));
  ok('the mascot also carries a hard max-height cap as a backstop',
     /\.joymascot\{[^}]*max-height:540px/.test(css));
  // The owner asked for the mascot at 1.5x on 9 Oct. The size lives in TWO places (the base
  // rule and the <=900px breakpoint) and they drift apart silently -- the phone would just look
  // fine on one viewport and wrong on the other. So assert the RATIO, not the numbers: whatever
  // the base cap is, the breakpoint cap must be the same 1.5x multiple of it that it is today.
  const capBase = (css.match(/\.joymascot\{[^}]*max-height:(\d+)px/) || [])[1];
  const capSmall = (css.match(/\.joymascot\{height:min\(\d+vw,(\d+)px\)\}/) || [])[1];
  ok('the mascot is 1.5x its previous size at BOTH breakpoints (base 540px / small 375px)',
     capBase === '540' && capSmall === '375', 'base=' + capBase + ' small=' + capSmall);
  // A state with no label falls through to the RAW enum, and at 14px monospace in a
  // third-of-a-panel box "CONFIRMATION_READY" breaks mid-word as "CONFIRMATION_RE / ADY". The
  // enum list is read out of the BACKEND rather than a hand-copied one, so adding a state to the
  // state machine fails here until someone gives it a human label.
  const appDir = path.join(dir, '..', 'backend', 'app');
  const stateNames = new Set();
  if (fs.existsSync(appDir)) {
    for (const f of fs.readdirSync(appDir).filter(n => n.endsWith('.py'))) {
      const src = fs.readFileSync(path.join(appDir, f), 'utf8');
      for (const m of src.matchAll(/"(IDLE|CAPTURED|CLARIFICATION_REQUIRED|CONFIRMATION_READY|CONFIRMED|EXECUTED|FROZEN|REJECTED|BLOCKED|SAFETY_INTERVENTION|STEP_UP_REQUIRED|EXPIRED|AWAITING_BIOMETRIC|SIGNED)"/g)) {
        stateNames.add(m[1]);
      }
    }
  }
  const labelBlock = (uiSrc.match(/STATE_LABEL = \{([\s\S]*?)\n\};/) || ['', ''])[1];
  const labelled = new Set([...labelBlock.matchAll(/([A-Z_]+):/g)].map(m => m[1]));
  const unlabelled = [...stateNames].filter(s => !labelled.has(s));
  ok('every backend session state has a human label in STATE_LABEL',
     stateNames.size >= 10 && unlabelled.length === 0,
     'found ' + stateNames.size + ' states; unlabelled: ' + unlabelled.join(', '));

  // ---- the phone got bigger ----
  ok('the phone renders its contents at full size (--phone-zoom 1, was .88)',
     /--phone-zoom:1;/.test(css));
  ok('the inspector is one full-height row, not a 2x2 grid',
     /\.ins\{display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr;/.test(css));
  // The second section is sized by its OWN content and must not be shrunk by the list above it.
  // The name of this check was always right; the value it pinned was not. `flex:0 1 auto` gave
  // the section flex-shrink:1, so it competed with the agent list for the deficit and lost:
  // measured in Chrome, the right panel's DETERMINISTIC OUTCOME block needed 197px and was
  // squeezed to 85px, cutting the whole verdict off the bottom of the panel. So this now READS
  // the shorthand and asserts the shrink factor, instead of matching a string that can drift.
  const psplitRule = (css.match(/\.psplit\{([^}]*)\}/) || ['', ''])[1];
  const psplitFlex = ((psplitRule.match(/flex:([^;}]*)/) || ['', ''])[1] || '').trim().split(/\s+/);
  ok('the second section is content-sized and NOT shrinkable by the list above',
     psplitFlex[0] === '0' && psplitFlex[1] === '0' && /max-height:/.test(psplitRule),
     'flex:' + psplitFlex.join(' '));
  // ...and the cap must carry a px FLOOR, not just a percentage. A bare `44%` is a share of the
  // panel, so on a SHORT panel it can come out SMALLER than the block it caps: measured at
  // 1280x640 the panel offered 160px for a 197px verdict, so the decision row itself was clipped
  // (fine at 1280x720). `max(44%,215px)` lets the percentage rule on tall panels and guarantees
  // the block its height on short ones. Asserted structurally so the number can be re-tuned
  // without editing this check, but the floor itself cannot be dropped.
  const psplitCap = ((psplitRule.match(/max-height:([^;}]*)/) || ['', ''])[1] || '').trim();
  const capFloor = (psplitCap.match(/max\(([^)]*)\)/) || ['', ''])[1] || '';
  ok('the max-height cap has a px floor, so a short panel cannot clip the verdict',
     /%/.test(capFloor) && /\d+px/.test(capFloor),
     'max-height:' + psplitCap);

  // ---- THE TOP BAR IS A HEIGHT BUDGET, and this is the only place that can catch it ----
  // `.top` is flex-wrap:wrap and the stage below it is `flex:1 1 auto`, so a SECOND ROW in the
  // bar costs the phone 34px of height (870 -> 837) while nothing on screen looks wrong. Adding
  // the Home button on 9 Oct pushed the bar 25px over its 1460px container and did exactly that.
  // jsdom has no layout engine, so this cannot be measured here; instead it pins the four
  // spacing values the live measurement depends on. Relax any one of them and the bar wraps
  // again at 1440px. The numbers come from /tmp/aj-topbar-sweep.cjs, not from guesswork.
  ok('the top bar keeps its compact spacing (only ~10px of slack at 1460px)',
     /^\.top\{[^}]*gap:9px/m.test(css) &&
     /^\.badges\{[^}]*gap:5px/m.test(css) &&
     /^\.bdg\{[^}]*padding:5px 8px/m.test(css) &&
     /^\.ghostbtn\{[^}]*padding:5px 8px/m.test(css));
  // 1440px is a common laptop width. Without this rule the bar is 66px short there and wraps.
  ok('a narrow-laptop compaction rule keeps the bar one row down to 1440px',
     /@media \(max-width:1500px\)\{[\s\S]*?\.bdg\{font-size:10px/.test(css));

  // ---- the explainer overlay ----
  ok('the overlay starts hidden', !!$('explain') && $('explain').hidden === true);
  const pages = doc.querySelectorAll('#explain .expage');
  ok('the overlay has THREE pages, not four', pages.length === 3, pages.length + ' pages');
  ok('every page has a tab that selects it',
     [...pages].every(p => !!doc.querySelector('#exTabs .extab[data-page="' + p.dataset.page + '"]')));
  ok('the tab order matches the button order (arch, how, path)',
     [...doc.querySelectorAll('#exTabs .extab')].map(b => b.dataset.page).join(',') ===
       'arch,how,path',
     [...doc.querySelectorAll('#exTabs .extab')].map(b => b.dataset.page).join(','));
  // The architecture diagram is the page a presenter opens FIRST, so it must be the default:
  // the one page with no `hidden` attribute, and the first tab. If this ever flips, "one click
  // to the diagram" quietly becomes "one click to the diagram, plus a second click".
  ok('the architecture page is the default (no hidden attribute)',
     doc.querySelector('.expage[data-page="arch"]').hidden === false);
  ok('the architecture page is drawn, not shipped as an image',
     !!doc.querySelector('.expage[data-page="arch"] .dgm') &&
     !!doc.querySelector('.expage[data-page="arch"] .dgband.ai') &&
     !!doc.querySelector('.expage[data-page="arch"] .dgband.det'));
  ok('the diagram carries both band names in the diagram\'s own words',
     /UNDERSTAND AND PROPOSE/.test($('explain').textContent) &&
     /DECIDE, AUTHORISE AND EXECUTE/.test($('explain').textContent));
  // The diagram numbers 0-16 and the chain numbers 0-15. That difference is deliberate and
  // explained on the page, so what must be guarded is that the explanation is PRESENT -- a bare
  // diagram would look like a bug the first time someone counts the boxes.
  ok('the page explains why the diagram counts 0-16 and the chain counts 0-15',
     /Banking Policy Check is one stage, not four/.test($('explain').textContent));
  // "What's real" is no longer a page of its own. Its two blocks - the live/simulated comparison
  // and the requirements-coverage list - are now the closing sections of the "How it works" page,
  // so #reqs must be found INSIDE data-page="how". ui.js looks #reqs up by id, so the move needed
  // no JavaScript change at all; this is the assertion that keeps it that way.
  ok('the requirements-coverage list now lives on the how-it-works page',
     doc.querySelector('.expage[data-page="how"]').contains($('reqs')));
  ok('the live/simulated comparison moved with it',
     /SIMULATED — deliberately, and labelled/.test(
       doc.querySelector('.expage[data-page="how"]').textContent));
  ok('there is no "real" page and no "real" tab any more',
     !doc.querySelector('.expage[data-page="real"]') &&
     !doc.querySelector('#exTabs .extab[data-page="real"]'));
  // The A-H rail was removed from the execution-path page on request. Asserted here as well as
  // at the top of the file: this is the block that would otherwise silently keep passing while
  // the page quietly grew a rail back.
  ok('the execution-path page no longer carries the A-H rail',
     !doc.querySelector('.expage[data-page="path"] #rail') && !$('rail'));
  ok('the failure-branches page is gone as a tab of its own',
     !doc.querySelector('.expage[data-page="branches"]') &&
     !doc.querySelector('#exTabs .extab[data-page="branches"]'));
  ok('the how-it-works page has a mount for the agents and one for the controls',
     !!$('exAgentList') && !!$('exCtlList'));
  ok('the execution-path page has a mount for the reference table', !!$('exRefList'));
  ok('the how-it-works page lists all FOUR agents, not five',
     doc.querySelectorAll('#exAgentList .exagent').length === 4,
     doc.querySelectorAll('#exAgentList .exagent').length + ' cards');
  ok('the Personalization Agent is not presented as a bounded agent',
     !/Personalization Agent/.test($('exAgentList').textContent));
  // The control list is rendered from the SAME STAGES array the live chain uses, so this is
  // really a check that the explainer cannot drift out of step with the main screen.
  ok('the controls list renders all sixteen stages',
     doc.querySelectorAll('#exCtlList .exct').length === 16,
     doc.querySelectorAll('#exCtlList .exct').length + ' controls');
  ok('the controls are grouped into the two bands the diagram draws',
     doc.querySelectorAll('#exCtlList .exband').length === 2 &&
     doc.querySelectorAll('#exCtlList .exband.ai .exct').length === 9 &&
     doc.querySelectorAll('#exCtlList .exband.det .exct').length === 7,
     [...doc.querySelectorAll('#exCtlList .exband')].map(b => b.className).join(' | '));
  ok('exactly four stages are marked as model stages',
     doc.querySelectorAll('#exCtlList .exct.llm').length === 4,
     doc.querySelectorAll('#exCtlList .exct.llm').length + ' marked');
  ok('and they are stages 03, 04, 05 and 08',
     [...doc.querySelectorAll('#exCtlList .exct.llm .exctn')].map(n => n.textContent).join(',') ===
       '03,04,05,08',
     [...doc.querySelectorAll('#exCtlList .exct.llm .exctn')].map(n => n.textContent).join(','));
  // The reference table on the path page is built from the same array, so it is a third view of
  // one source of truth rather than a third copy of the list.
  ok('the reference table renders all sixteen stages, tagged by band',
     doc.querySelectorAll('#exRefList .prefrow').length === 16 &&
     doc.querySelectorAll('#exRefList .prefrow.ai').length === 9 &&
     doc.querySelectorAll('#exRefList .prefrow.det').length === 7,
     doc.querySelectorAll('#exRefList .prefrow').length + ' rows');
  ok('Escape closes the overlay', /ev\.key === 'Escape'/.test(uiSrc));
  ok('the overlay has a close button', !!$('exClose'));

  // ---- and it actually opens, switches page and closes ----
  const wasHidden = $('explain').hidden;
  ev("openExplainer('how')");
  const openedOnHow = $('explain').hidden === false &&
    doc.querySelector('.expage[data-page="how"]').hidden === false &&
    doc.querySelector('.expage[data-page="arch"]').hidden === true;
  const tabOn = doc.querySelector('#exTabs .extab[data-page="how"]').classList.contains('on');
  ev("showExplainerPage('path')");
  const switched = doc.querySelector('.expage[data-page="path"]').hidden === false &&
                   doc.querySelector('.expage[data-page="how"]').hidden === true;
  // openExplainer() with no argument must land on the DIAGRAM, not on whichever page was last
  // read: the top-bar buttons always pass a page, so the no-argument path is the one a future
  // caller (a keyboard shortcut, a deep link) would use.
  ev("showExplainerPage('how')"); ev("openExplainer()");
  const defaultedToArch = doc.querySelector('.expage[data-page="arch"]').hidden === false;
  ev("closeExplainer()");
  ok('openExplainer() reveals the overlay on the requested page', wasHidden && openedOnHow);
  ok('the matching tab is marked as selected', tabOn);
  ok('showExplainerPage() switches to another page and hides the first', switched);
  ok('openExplainer() with no argument defaults to the architecture page', defaultedToArch);
  ok('closeExplainer() hides the overlay again', $('explain').hidden === true);
})();

console.log('\n=== 9f. THE EXECUTION-PATH PAGE QUOTES THE CODE, NOT THE SLIDE ===');
// The explainer pages name functions, a check count and the payment path. Those are checkable
// facts, so they are asserted against the BACKEND SOURCE rather than against a reviewer's memory.
// This build has no payment rail: stage 15 calls Ledger.execute() directly (orchestrator.py), and
// the DBS Fund Transfer API is an integration target only. The page must say exactly that, and
// must never name a rail class the code does not define.
(function(){
  const controls = fs.readFileSync(path.join(dir, '..', 'backend', 'app', 'controls.py'), 'utf8');
  const orch     = fs.readFileSync(path.join(dir, '..', 'backend', 'app', 'orchestrator.py'), 'utf8');
  const html     = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');

  ok('stage 15 on the page is the mock ledger the orchestrator actually calls',
     /Ledger\.execute\(/.test(orch) && /Ledger\.execute\(\)/.test(html));
  ok('the page names no rail class the backend does not define',
     !/MockLedgerRail|DbsFundTransferRail/.test(html) ||
     /class (MockLedgerRail|DbsFundTransferRail)/.test(controls + orch));
  ok('the DBS API is presented as an integration target, not as something the app calls',
     /integration target/.test(html) && /not built/.test(html));
  ok('the page does not invent a token endpoint the code never calls',
     !/\/auth\/token/.test(html));
  ok("the page does not use the slide's wrong path names",
     !/\/transfers\/ownAccount/.test(html) && !/\/transfers\/payeeAccount/.test(html));

  // The gateway function is `gateway_verify()` (controls.py); the slide wrote `gateway.verify()`.
  ok('the page names the gateway function the code actually defines',
     controls.includes('def gateway_verify(') &&
     /gateway_verify\(\)/.test(html) &&
     !/gateway\.verify\(\)/.test(html));

  // The check count is a NUMBER on the page, so pin it to the function body: a claim of "14
  // checks" that quietly becomes 15 checks in code is exactly the drift this catches.
  const from = controls.indexOf('def gateway_verify(');
  const rest = controls.slice(from + 1);
  const stop = rest.indexOf('\ndef ');
  const body = stop > 0 ? rest.slice(0, stop) : rest;
  const keys = [...new Set([...body.matchAll(/checks\["([a-z_]+)"\]/g)].map(m => m[1]))];
  ok('the checks the page claims are the checks the function runs',
     keys.length === 14 && /14 checks/.test(html), keys.length + ' in code');

  // The signature is an HMAC with a demo key; the page must not call it a bank-owned key.
  ok('the page describes the signature as the demo HMAC the code uses',
     /hmac\.new\(/.test(controls) && /HMAC-SHA256/.test(html) && !/bank-owned key/.test(html));
})();

console.log('\n=== 9e. HOME RETURNS TO THE LOGIN SCREEN FROM ANYWHERE ===');
// Reset and Home are deliberately different controls, and the difference is the whole point.
// Reset is CONTEXT-AWARE: inside the chat it restarts in place, so it can never serve as "take
// me back to the start". Home is the unconditional one. A presenter who has wandered into a
// failure branch, or into a half-answered confirmation popup, needs exactly one button that
// always lands in the same place -- so this drives it from the two places they get stuck.
(function(){
  // From the deepest point in the flow: the Joy chat.
  ev("showHome()"); ev("showJoy(true)");
  const inChat = scrOn('scrJoy');
  $('homeBtn').dispatchEvent(new window.MouseEvent('click', { bubbles:true }));
  ok('Home gets you back to the login screen from the chat',
     inChat && scrOn('scrLogin') && !scrOn('scrJoy'));
  ok('and the form is open and usable when you land',
     $('lgForm').hidden === false && !!$('lgUser') && !!$('lgPin'));
  ok('the run was CLEARED, not just navigated away from', chat.children.length === 0,
     chat.children.length + ' transcript children left behind');
  // And from the digibank home, the other place a presenter can get stuck.
  ev("showHome()");
  $('homeBtn').dispatchEvent(new window.MouseEvent('click', { bubbles:true }));
  ok('Home also works from the digibank home', scrOn('scrLogin') && !scrOn('appwin'));
})();

console.log('\n=== 10. NO STRAY ERRORS ===');
const realErrors = errors.filter(e => !/Not implemented|Could not parse CSS/.test(e));
ok('no uncaught JS errors', realErrors.length === 0, JSON.stringify(realErrors));
ok('no jsdom fatal errors', problems.filter(p => !/Not implemented/.test(p)).length === 0,
   JSON.stringify(problems.slice(0, 3)));

console.log('\n' + (fails === 0 ? 'ALL CHECKS PASSED' : fails + ' CHECK(S) FAILED'));
process.exit(fails === 0 ? 0 : 1);
