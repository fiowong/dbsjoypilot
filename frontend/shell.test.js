/* Headless check of the AuntieJoy DBS shell and the in-transcript cards.
   Run:  NODE_PATH=<workspace>/node_modules node shell.test.js            */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const dir = __dirname;
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
const css  = fs.readFileSync(path.join(dir, 'styles.css'), 'utf8');
// Loaded in the same order index.html uses — the files share globals.
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
// in the global lexical scope — which is how ui.js's `iface`, `auntie`, `fill` … are visible
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
// not properties of `window` — same as in a browser. Reach them the way a later <script>
// would: by evaluating an expression in global scope.
const ev = code => window.eval(code);

console.log('\n=== 1. LANDING IS THE DBS LOGIN (nothing auto-runs) ===');
ok('login screen is active', scrOn('scrLogin'));
ok('chat transcript is empty at boot', chat.children.length === 0, chat.children.length + ' children');
ok('scenario rail is hidden (backup only)', $('rail').hidden === true);
ok('no backend call was made at boot', fetchCalls === 0, fetchCalls + ' calls');

console.log('\n=== 2. DEVICE MOCKUP ===');
ok('device + screen present', !!$('device') && !!$('deviceScreen'));
ok('status bar + island present', !!doc.querySelector('.statusbar') && !!doc.querySelector('.island'));
ok('all three screens live inside the device screen',
   ['scrLogin','appwin','scrJoy'].every(s => $('deviceScreen').contains($(s))));
ok('inspector sits outside the device', !$('device').contains($('agents')));
ok('only one screen is active at a time', ['scrLogin','appwin','scrJoy'].filter(scrOn).length === 1);

console.log('\n=== 3. LOG IN -> DBS HOME -> ENABLE AUNTIEJOY ===');
$('lgBtn').dispatchEvent(new window.MouseEvent('click', { bubbles:true }));
ok('login dismissed, app window active', !scrOn('scrLogin') && scrOn('appwin'));
ok('balance card present', !!$('appBody').querySelector('.balcard'));
ok('big enable button present', !!$('appBody').querySelector('.enable'));
$('appBody').querySelector('.enable').dispatchEvent(new window.MouseEvent('click', { bubbles:true }));
ok('AuntieJoy chat active', scrOn('scrJoy'));
ok('intro card rendered', !!chat.querySelector('.intro'));
ok('intro offers suggestion chips', chat.querySelectorAll('.chip').length >= 3,
   chat.querySelectorAll('.chip').length + ' chips');
ok('transcript contains ONLY the intro card', chat.children.length === 1, chat.children.length + ' children');
// The greeting addresses the customer by name, but the intro card is drawn BEFORE the first
// turn — so at this point no trusted name exists yet. It must NOT invent one (addressing the
// customer as "Auntie" reads like the assistant talking to itself).
const greet = () => chat.querySelector('.intro .itext').textContent.replace(/\s+/g, ' ').trim();
ok('greeting starts nameless rather than inventing a name', /^Hello! Tell me who to pay/.test(greet()), greet());
ev("CUSTOMER_NAME='Mei Ling'; CUSTOMER_NAME_KNOWN=true; refreshNameSlots();");
ok('greeting fills the trusted name once the session supplies it',
   /^Hello Mei Ling! Tell me who to pay/.test(greet()), greet());
ev("CUSTOMER_NAME='Auntie'; CUSTOMER_NAME_KNOWN=false; refreshNameSlots();");
ok('and drops it again if the session is reset', /^Hello! Tell me/.test(greet()), greet());

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
// must not append a chat line — the wording on the card is the backend's own line.
ok('retry appended no client-written sentence', chat.children.length === lenBefore,
   chat.children.length + ' vs ' + lenBefore);
ok('retry marked itself done', card.dataset.retried === '1');
btn.dispatchEvent(new window.MouseEvent('click', { bubbles:true, cancelable:true }));
ok('a second click does not queue another turn', chat.children.length === lenBefore);
ok('still no reload after double click', unloadCount === 0);

console.log('\n=== 7. removeLastIfaceBubble() REMOVES ONLY THE BUBBLE ===');
window.resetRun();
window.clarifyCard('x', null);              // a card (not a bubble) — must survive
ev("iface('a backend line')");              // a bubble — must be removed
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
// LEXICAL binding, not a property of window — reach it the way live.js does.
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
// Convert inside the page realm and return a plain string — a jsdom ArrayBuffer is not
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

console.log('\n=== 10. NO STRAY ERRORS ===');
const realErrors = errors.filter(e => !/Not implemented|Could not parse CSS/.test(e));
ok('no uncaught JS errors', realErrors.length === 0, JSON.stringify(realErrors));
ok('no jsdom fatal errors', problems.filter(p => !/Not implemented/.test(p)).length === 0,
   JSON.stringify(problems.slice(0, 3)));

console.log('\n' + (fails === 0 ? 'ALL CHECKS PASSED' : fails + ' CHECK(S) FAILED'));
process.exit(fails === 0 ? 0 : 1);
