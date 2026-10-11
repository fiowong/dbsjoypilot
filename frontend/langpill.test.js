/* Guards the language pills (EN, 中文, BM).

   WHY THIS EXISTS
   --------------
   Tencent has no single ASR engine that covers English + Chinese + Malay, so the pill does
   not just hint a language -- it selects an ENGINE:

       16k_zh_en      Chinese + English (+31 dialects), auto-detects between them
       16k_multi_lang English, Malay and 13 others; NO Chinese

   That makes the pill a one-character change away from a silent, invisible fault: rename a
   `data-l` value, or add a pill whose value the backend does not know, and the mic simply
   listens with the wrong engine. Nothing throws. The transcript is just quietly wrong.

   Two things are therefore pinned here:
     1. The pill set and its wiring (exactly one selected, clicking moves the selection).
     2. That every language value the frontend can send is a key the BACKEND actually
        understands -- read out of stt.py, not duplicated as a literal here, so this test
        fails if either side is edited without the other.

   Every pill must also carry a real BCP-47 `data-lang`: that is what the no-server Web Speech
   fallback is handed. There is no Auto pill; it was removed from the UI on 11 Oct.

   Run:  NODE_PATH=<workspace>/node_modules node langpill.test.js                */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const dir = __dirname;
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
const files = ['translations.js', 'icons.js', 'ui.js', 'personalization.js', 'scenarios.js',
               'stt.js', 'live.js', 'shell.js'];

let fails = 0;
const ok = (name, cond, extra) => {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + name +
              (cond ? '' : '   → ' + (extra === undefined ? '' : extra)));
  if (!cond) fails++;
};

const problems = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => problems.push('jsdomError: ' + e.message));
vc.on('error', (...a) => problems.push('console.error: ' + a.join(' ')));

const srcFree = html.replace(/<script\b[^>]*\bsrc=[^>]*>\s*<\/script>/gi, '');
const dom = new JSDOM(srcFree, { runScripts: 'dangerously', virtualConsole: vc });
const { window } = dom;
const doc = window.document;
window.fetch = () => Promise.reject(new Error('no network in test'));

for (const f of files) {
  const s = doc.createElement('script');
  s.textContent = fs.readFileSync(path.join(dir, f), 'utf8');
  doc.body.appendChild(s);
}

const pills = () => [...doc.querySelectorAll('#langtog .lt')];
const selected = () => doc.querySelector('#langtog .lt.on');
const byL = l => pills().find(b => b.dataset.l === l);

console.log('=== 1. THE PILL SET ===');
ok('there are three language pills (EN, ZH, BM)', pills().length === 3, pills().length);
ok('exactly one pill is selected', doc.querySelectorAll('#langtog .lt.on').length === 1);
ok('the default selection is EN', selected() && selected().dataset.l === 'EN',
   selected() && selected().dataset.l);
for (const l of ['EN', 'ZH', 'BM']) {
  ok(`the ${l} pill exists`, !!byL(l));
}

console.log('=== 2. NO AUTO PILL, AND EVERY PILL IS SAFE FOR THE WEB SPEECH FALLBACK ===');
ok('there is no Auto pill', !byL('AUTO'));
for (const b of pills()) {
  ok(`the ${b.dataset.l} pill carries a real BCP-47 data-lang`,
     /^[a-z]{2}(-[A-Za-z0-9]{2,8})*$/.test(b.dataset.lang || ''), b.dataset.lang);
}

console.log('=== 3. EVERY PILL RESOLVES TO A LANGUAGE THE BACKEND KNOWS ===');
// Read the backend's own mapping rather than restating it, so the two cannot drift apart.
const sttSrc = fs.readFileSync(path.join(dir, '..', 'backend', 'app', 'stt.py'), 'utf8');
const block = sttSrc.match(/ENGINE_BY_LANGUAGE\s*=\s*\{([\s\S]*?)\}/);
ok('stt.py declares ENGINE_BY_LANGUAGE', !!block);
const backendKeys = block ? [...block[1].matchAll(/"([^"]+)"\s*:/g)].map(m => m[1]) : [];
ok('stt.py lists at least four languages', backendKeys.length >= 4, backendKeys.join(','));

const expected = { EN: 'en', ZH: 'zh', BM: 'bm' };
for (const [pill, want] of Object.entries(expected)) {
  const b = byL(pill);
  if (!b) { ok(`${pill} -> ${want}`, false, 'pill missing'); continue; }
  // Select just this pill, the way a click does, then ask the real resolver.
  pills().forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  const got = window.sttLanguage();
  ok(`${pill} -> "${got}"`, got === want, `expected ${want}`);
  ok(`the backend understands "${got}"`, backendKeys.includes(got),
     `backend keys: ${backendKeys.join(',')}`);
}

console.log('=== 4. CLICKING A PILL MOVES THE SELECTION ===');
const bm = byL('BM');
bm.click();
ok('clicking BM selects only BM',
   doc.querySelectorAll('#langtog .lt.on').length === 1 && selected().dataset.l === 'BM',
   selected() && selected().dataset.l);
ok('clicking BM leaves the backend a key it knows', backendKeys.includes(window.sttLanguage()),
   window.sttLanguage());

console.log('=== 5. NO STRAY ERRORS ===');
ok('no jsdom fatal errors', problems.length === 0, problems.slice(0, 2).join(' | '));

console.log(fails ? `\n${fails} CHECK(S) FAILED` : '\nALL CHECKS PASSED');
process.exit(fails ? 1 : 0);
