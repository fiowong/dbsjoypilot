/* Regression guard for the two frontend faults the customer reported:

     1. "faceid ... it said 抱歉，我刚刚联系不上支付系统，请再试一次。"
        A FaceID tap on a session the backend no longer holds (sessions are in memory, so a
        backend restart loses them) is answered with HTTP 409 - the backend REPLIED. That used
        to be reported with the same "unreachable" line as a dead socket, so the customer was
        told to retry a payment that could never authorise. The 409 now has its own line.

     2. "the first working on it card is still in english" (Bahasa turn)
        The processing card is drawn from the frontend's dominantLang(), which must mirror the
        backend's dominant_language() exactly. T1 is a NARROW win (BM 10 vs EN 9), so a single
        word counted differently flips the card to English while the reply stays Malay.

   Run:  NODE_PATH=<workspace>/node_modules node confirm.test.js                */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const dir = __dirname;
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
const liveSrc = fs.readFileSync(path.join(dir, 'live.js'), 'utf8');
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

// The exact sentence the customer quoted. It must still be the ZH "unreachable" wording, which
// is what proves the symptom they described came from the transport branch.
const REPORTED = '抱歉，我刚刚联系不上支付系统，请再试一次。';

console.log('=== 1. THE REPORTED SENTENCE IS THE OLD "UNREACHABLE" LINE ===');
ok('tt("ZH").chrome.unreachable is the sentence the customer quoted',
   window.tt('ZH').chrome.unreachable === REPORTED,
   JSON.stringify(window.tt('ZH').chrome.unreachable));

console.log('=== 2. A 409 NOW HAS ITS OWN, TRUTHFUL LINE IN EVERY LANGUAGE ===');
for (const lang of ['EN', 'ZH', 'BM']) {
  const c = window.tt(lang).chrome;
  ok(`${lang}: nothingToConfirm exists`, typeof c.nothingToConfirm === 'string' && c.nothingToConfirm.length > 10);
  ok(`${lang}: nothingToConfirm is NOT the unreachable line`, c.nothingToConfirm !== c.unreachable);
  // It must not promise a retry, which is the whole defect.
  ok(`${lang}: nothingToConfirm does not tell the customer to try again`,
     !/try again|再试一次|cuba lagi/i.test(c.nothingToConfirm), c.nothingToConfirm);
}

console.log('=== 3. THE TWO FAILURES ARE ROUTED TO DIFFERENT LINES ===');
ok('the 409 branch is keyed on err.status === 409', /err\.status\s*===\s*409/.test(liveSrc));
ok('the 409 branch says nothingToConfirm', /CONFIRM_REFUSED_409[\s\S]{0,160}?nothingToConfirm/.test(liveSrc));
ok('the transport branch still says unreachable', /CONFIRM_CALL_FAILED[\s\S]{0,120}?c\.unreachable/.test(liveSrc));
ok('the 409 keeps the backend reason in the audit panel', /CONFIRM_REFUSED_409/.test(liveSrc));

console.log('=== 4. THE WORKING CARD FOLLOWS THE BACKEND LANGUAGE (BM 10 vs EN 9) ===');
const T1 = 'Pindahkan seribu dollar dari akun simpanan saya ke akun bersama saya, kemudian hantarlah lima puluh dolar kepada ah girl.';
ok('dominantLang(T1) is BM', window.dominantLang(T1) === 'BM', window.dominantLang(T1));
ok('dominantLang("hantar lima puluh dolar kepada Mei Tan") is BM',
   window.dominantLang('hantar lima puluh dolar kepada Mei Tan') === 'BM');
// The word that broke it: the backend counts "dollar" as Malay, so the frontend must too.
ok('_BM_VOCAB contains dollar/dollars', liveSrc.includes("'dollars'") && liveSrc.includes("'dollar'"));
ok('no neutral-word list was reintroduced', !/_NEUTRAL_WORDS/.test(liveSrc));

console.log('=== 5. NO STRAY ERRORS ===');
ok('no jsdom fatal errors', problems.length === 0, problems.slice(0, 2).join(' | '));

console.log(fails ? `\n${fails} CHECK(S) FAILED` : '\nALL CHECKS PASSED');
process.exit(fails ? 1 : 0);
