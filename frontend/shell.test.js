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
// The A-H rail is a BACKUP and must never be on the main screen. It no longer has a top-bar
// toggle or a full-width strip of its own: it now lives on page 3 of the explainer overlay,
// which is hidden at boot. The buttons themselves are unchanged and scenarios.js still wires
// them by class, so what this guards is that the rail is (a) still present and (b) not visible
// until someone asks for it.
ok('the A-H rail is off the main screen (page 3 of the hidden explainer overlay)',
   !!$('rail') && !!$('explain') && $('explain').hidden === true &&
   $('explain').contains($('rail')));
ok('no backend call was made at boot', fetchCalls === 0, fetchCalls + ' calls');

console.log('\n=== 2. DEVICE MOCKUP ===');
ok('device + screen present', !!$('device') && !!$('deviceScreen'));
ok('status bar + island present', !!doc.querySelector('.statusbar') && !!doc.querySelector('.island'));
ok('all three screens live inside the device screen',
   ['scrLogin','appwin','scrJoy'].every(s => $('deviceScreen').contains($(s))));
ok('inspector sits outside the device', !$('device').contains($('agents')));
ok('only one screen is active at a time', ['scrLogin','appwin','scrJoy'].filter(scrOn).length === 1);

console.log('\n=== 3. LOGIN FORM (boot) -> DBS HOME -> ASK JOY TO PAY ===');
// The User ID / PIN form IS the login screen. The launchpad (hero photo + six tiles) and
// the form's back arrow were deleted on 2 Oct, so boot lands on the form and LOGIN goes
// straight into digibank - there is no tile grid round trip any more.
ok('login form is OPEN at boot (matching iggy, form-first)',
   $('lgForm').hidden === false && scrOn('scrLogin'));
ok('no launchpad remains behind the form (hero + tiles deleted 2 Oct)',
   !doc.querySelector('#scrLogin .lghero') && !doc.querySelector('#scrLogin .lgmenu'));
ok('no back arrow on the form (nothing to go back to)', !$('lgFormBack'));
$('lgSubmit').dispatchEvent(new window.MouseEvent('click', { bubbles:true }));
ok('login dismissed, app window active', !scrOn('scrLogin') && scrOn('appwin'));
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
   audit timeline, the requirements list and the A-H rail were all RELOCATED. A
   later tidy-up that genuinely deletes one of them has to fail here, because on
   the screen the two look identical.
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
  ok('the demo-status chip is a BUTTON that opens its own explainer',
     !!$('demoBadge') && $('demoBadge').tagName === 'BUTTON' &&
     $('demoBadge').dataset.explain === 'demo');
  ok('the agents chip is a BUTTON that opens its own explainer',
     !!$('agentsBadge') && $('agentsBadge').tagName === 'BUTTON' &&
     $('agentsBadge').dataset.explain === 'agents');

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
     /@media\(max-width:900px\)\{[\s\S]*?\.joymascot\{height:min\(32vw,250px\)\}/.test(css));
  ok('the mascot also carries a hard max-height cap as a backstop',
     /\.joymascot\{[^}]*max-height:360px/.test(css));
  // A state with no label falls through to the RAW enum, and at 14px monospace in a
  // third-of-a-panel box "CONFIRMATION_READY" breaks mid-word as "CONFIRMATION_RE / ADY".
  //
  // The enum is read from the DECLARATION in models.py, NOT from a list of names copied into
  // this file. The earlier version scanned every backend file for a hardcoded set of fourteen
  // names, so it could only ever find states already on that list -- seven real ones
  // (EVIDENCE_COMMITTED, DRAFTING, DRAFT_READY, COOLING_OFF, CONFIRMATION_COMPLETE, SCHEDULED,
  // CANCELLED) were invisible to it, rendered as raw enums, and this check stayed green.
  // Reading the declaration is what makes "adding a state fails here" true rather than claimed.
  const modelsPath = path.join(dir, '..', 'backend', 'app', 'models.py');
  const stateNames = new Set();
  if (fs.existsSync(modelsPath)) {
    const modelsSrc = fs.readFileSync(modelsPath, 'utf8');
    const enumSrc = modelsSrc.match(/class SessionState\([\s\S]*?(?=\nclass )/);
    for (const m of (enumSrc ? enumSrc[0] : '').matchAll(/^\s{4}([A-Z][A-Z0-9_]*)\s*=/gm)) {
      stateNames.add(m[1]);
    }
  }
  ok('the SessionState enum was read out of models.py', stateNames.size >= 10,
     'found ' + stateNames.size + ' states');
  const labelBlock = (uiSrc.match(/STATE_LABEL = \{([\s\S]*?)\n\};/) || ['', ''])[1];
  const labelled = new Set([...labelBlock.matchAll(/([A-Z_]+):/g)].map(m => m[1]));
  const unlabelled = [...stateNames].filter(s => !labelled.has(s));
  ok('every backend session state has a human label in STATE_LABEL',
     unlabelled.length === 0,
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

  // ---- the explainer overlay ----
  ok('the overlay starts hidden', !!$('explain') && $('explain').hidden === true);
  const pages = doc.querySelectorAll('#explain .expage');
  ok('the overlay has three pages', pages.length === 3, pages.length + ' pages');
  ok('every page has a tab that selects it',
     [...pages].every(p => !!doc.querySelector('#exTabs .extab[data-page="' + p.dataset.page + '"]')));
  ok('the demo page carries the requirements-coverage list',
     doc.querySelector('.expage[data-page="demo"]').contains($('reqs')));
  ok('the branches page carries the A-H rail',
     doc.querySelector('.expage[data-page="branches"]').contains($('rail')));
  ok('the agents page has a mount for the agents and one for the controls',
     !!$('exAgentList') && !!$('exCtlList'));
  ok('the agents page lists all five agents',
     doc.querySelectorAll('#exAgentList .exagent').length === 5,
     doc.querySelectorAll('#exAgentList .exagent').length + ' cards');
  // The control list is rendered from the SAME STAGES array the live chain uses, so this is
  // really a check that the explainer cannot drift out of step with the main screen.
  ok('the controls list renders all sixteen stages',
     doc.querySelectorAll('#exCtlList .exct').length === 16,
     doc.querySelectorAll('#exCtlList .exct').length + ' controls');
  ok('exactly four stages are marked as model stages',
     doc.querySelectorAll('#exCtlList .exct.llm').length === 4,
     doc.querySelectorAll('#exCtlList .exct.llm').length + ' marked');
  ok('and they are stages 03, 04, 05 and 08',
     [...doc.querySelectorAll('#exCtlList .exct.llm .exctn')].map(n => n.textContent).join(',') ===
       '03,04,05,08',
     [...doc.querySelectorAll('#exCtlList .exct.llm .exctn')].map(n => n.textContent).join(','));
  ok('Escape closes the overlay', /ev\.key === 'Escape'/.test(uiSrc));
  ok('the overlay has a close button', !!$('exClose'));

  // ---- and it actually opens, switches page and closes ----
  const wasHidden = $('explain').hidden;
  ev("openExplainer('agents')");
  const openedOnAgents = $('explain').hidden === false &&
    doc.querySelector('.expage[data-page="agents"]').hidden === false &&
    doc.querySelector('.expage[data-page="demo"]').hidden === true;
  const tabOn = doc.querySelector('#exTabs .extab[data-page="agents"]').classList.contains('on');
  ev("showExplainerPage('branches')");
  const switched = doc.querySelector('.expage[data-page="branches"]').hidden === false &&
                   doc.querySelector('.expage[data-page="agents"]').hidden === true;
  ev("closeExplainer()");
  ok('openExplainer() reveals the overlay on the requested page', wasHidden && openedOnAgents);
  ok('the matching tab is marked as selected', tabOn);
  ok('showExplainerPage() switches to another page and hides the first', switched);
  ok('closeExplainer() hides the overlay again', $('explain').hidden === true);
})();

console.log('\n=== 10. NO STRAY ERRORS ===');
const realErrors = errors.filter(e => !/Not implemented|Could not parse CSS/.test(e));
ok('no uncaught JS errors', realErrors.length === 0, JSON.stringify(realErrors));
ok('no jsdom fatal errors', problems.filter(p => !/Not implemented/.test(p)).length === 0,
   JSON.stringify(problems.slice(0, 3)));

console.log('\n' + (fails === 0 ? 'ALL CHECKS PASSED' : fails + ' CHECK(S) FAILED'));
process.exit(fails === 0 ? 0 : 1);
