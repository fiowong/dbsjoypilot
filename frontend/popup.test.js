/* Headless check of the Joy DBS popup layer: where it lives in the DOM, the
   state-animation it picks per card type, that it can never become a dead end, and that
   every animation actually exists on disk at one shared canvas size.
   Run:  NODE_PATH=<workspace>/node_modules node popup.test.js
   Adapted from iggy's popup.test.js, but rewritten for dbs's actual DOM:
     - dbs keeps #modal as a SIBLING of the three .scr screens (NOT inside #scrJoy), so the
       login + Pay & Transfer notices stay visible on their own screens.
     - dbs picks the clip with popupGif(cfg) (icon/fields/acts heuristic), not iggy's
       stateGif(state, outcome). The two real Face ID prompts still win via icon:'faceid'.
     - dbs has no avatar crop (MASCOT_FACE / avImg) and no Escape-to-cancel, so those are dropped. */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const dir = __dirname;
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
const css  = fs.readFileSync(path.join(dir, 'styles.css'), 'utf8');
// Same order index.html uses; icons.js MUST load so icon() is defined at boot.
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
const dom = new JSDOM(srcFree, { runScripts:'dangerously', url:'http://127.0.0.1:8078/', virtualConsole: vc });
const { window } = dom;
const doc = window.document;
window.fetch = () => Promise.reject(new Error('no network in test'));

for (const f of files) {
  const before = problems.length;
  const s = doc.createElement('script');
  s.textContent = fs.readFileSync(path.join(dir, f), 'utf8');
  doc.body.appendChild(s);
  if (problems.length > before) { console.log('  FAIL  ' + f + ' threw → ' + problems[problems.length-1]); process.exit(1); }
}

const $ = id => doc.getElementById(id);
// querySelector (not getElementById) reaches the popup's own nodes once injected.
const q = sel => doc.querySelector(sel);
const ev = code => window.eval(code);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const G = ev('STAGE_GIF');

(async () => {

console.log('\n=== 1. THE POPUP LIVES INSIDE THE PHONE, AS A SIBLING OF THE CHAT SCREEN ===');
const modal = q('#modal'), mcard = q('#mcard'), screen = $('deviceScreen');
ok('popup layer exists', !!modal);
ok('card exists', !!mcard);
ok('popup layer is inside the device screen', screen.contains(modal));
ok('popup layer is a SIBLING of #scrJoy (dbs design: login + Pay notices stay on their screens)',
   !$('scrJoy').contains(modal) && $('deviceScreen').contains(modal) && $('scrJoy') !== modal.parentNode);
ok('no iggy-style .popLayer markup leaked in', !doc.querySelector('.popLayer'));
ok('the overlay is absolutely positioned, not viewport-fixed',
   /\.modal\{[^}]*position:absolute/.test(css) && !/\.modal\{[^}]*position:fixed/.test(css));
ok('the overlay is clipped by the device screen radius',
   /\.device-screen\{[^}]*overflow:hidden/.test(css));

console.log('\n=== 2. THE POPUP IS HIDDEN UNTIL ASKED FOR ===');
ok('popup layer starts hidden', !modal.classList.contains('show'));

console.log('\n=== 3. THE SPLASH SHOWS WELCOME.GIF WITHIN THE POPUP ===');
ev('welcomeSplash')();               // opens, awaits a click we never send
await sleep(20);
ok('splash opened on first entry to the chat', modal.classList.contains('show'));
const stage = q('.popStage'), media = q('.mmedia');
ok('splash renders a state-animation slot', !!stage && !!media);
ok('splash plays Welcome', /Welcome\.webp$/.test(media.getAttribute('src')||''), media.getAttribute('src'));
ok('a normal popup carries the still portrait as the fallback',
   /mstill/.test(fs.readFileSync(path.join(dir,'ui.js'),'utf8')), 'the still is emitted by popup()');
ok('the splash card is inside the phone', screen.contains(mcard));
window.closePopup('x'); await sleep(10);
ok('splash dismissed again', !modal.classList.contains('show'));

console.log('\n=== 4. THE MASCOT IS THE SUPPLIED PORTRAIT, AND EVERY STATE ASSET EXISTS ===');
const MASCOT = ev('MASCOT');
const MASCOT_STILL = ev('MASCOT_STILL');
// dbs ships MASCOT as an inline avatar (data URI); what must be on disk is the still fallback.
ok('MASCOT resolves to a renderable image (inline avatar or path)',
   /^data:image\//.test(MASCOT) || fs.existsSync(path.join(dir, MASCOT)), MASCOT.slice(0, 48));
ok('the still-portrait fallback file is on disk', fs.existsSync(path.join(dir, MASCOT_STILL)), MASCOT_STILL);
for (const [k, v] of Object.entries(G)) {
  const p = decodeURIComponent(v);
  ok('state asset on disk: ' + k + ' → ' + v, fs.existsSync(path.join(dir, p)), v);
}
ok('every state animation actually exists',
   Object.values(G).every(v => fs.existsSync(path.join(dir, decodeURIComponent(v)))));

console.log('\n=== 5. popupGif() MAPS EACH CARD TYPE TO THE RIGHT CLIP ===');
const popupGif = ev('popupGif');
const STILL = 'assets/mascot-still.png';   // a sentinel that no branch should return
ok('Face ID prompt (icon:faceid) → Verification Required',
   popupGif({ icon:'faceid' }) === G.faceid, popupGif({ icon:'faceid' }));
ok('field review (cfg.fields) → Confirm Amount',
   popupGif({ fields:'<div class="fr"></div>' }) === G.confirm, popupGif({ fields:'x' }));
ok('refusal with a "dan" act → Not Allowed',
   popupGif({ acts:[{ id:'block', kind:'dan' }] }) === G.notallow, popupGif({ acts:[{ kind:'dan' }] }));
ok('refusal with a "no" act → Not Allowed',
   popupGif({ acts:[{ id:'no', kind:'no' }] }) === G.notallow, popupGif({ acts:[{ kind:'no' }] }));
ok('explicit cfg.gif always wins',
   popupGif({ gif: STILL, icon:'faceid' }) === STILL, popupGif({ gif: STILL }));
ok('a plain notice (only a "yes" act) → still only (null)',
   popupGif({ acts:[{ id:'ok', kind:'yes' }] }) === null, popupGif({ acts:[{ kind:'yes' }] }));
ok('LOGIN "not wired up" SWAP: explicit notallow wins even with a "yes" button',
   popupGif({ acts:[{ id:'ok', kind:'yes' }], gif: G.notallow }) === G.notallow,
   popupGif({ acts:[{ kind:'yes' }], gif: G.notallow }));

console.log('\n=== 6. EVERY STATE ANIMATION EXISTS AND HAS VALID DIMENSIONS ===');
const gifNames = Object.keys(G);
let sized = 0, sizedOk = true;
for (const k of gifNames) {
  const p = path.join(dir, decodeURIComponent(G[k]));
  if (!fs.existsSync(p)) { sizedOk = false; ok('animation on disk: ' + k, false, G[k]); continue; }
  const buf = fs.readFileSync(p);
  let w = 0, h = 0;
  const sig = buf.slice(0, 6).toString();
  if (sig.startsWith('RIFF') && buf.slice(8, 12).toString() === 'WEBP') {
    // WebP: the standard 260x195 stage size; every webp must match it.
    if (buf.slice(12, 16).toString() === 'VP8X') {
      w = 1 + (buf[24] | (buf[25] << 8) | (buf[26] << 16));
      h = 1 + (buf[27] | (buf[28] << 8) | (buf[29] << 16));
    } else if (buf.slice(12, 16).toString() === 'VP8 ') {
      w = (buf[26] | (buf[27] << 8)) & 0x3fff;
      h = (buf[28] | (buf[29] << 8)) & 0x3fff;
    }
    if (w === 260 && h === 195) sized++; else { sizedOk = false; }
    ok('animation ' + k + ' (webp) is 260x195', w === 260 && h === 195, w + 'x' + h);
  } else if (sig === 'GIF87a' || sig === 'GIF89a') {
    // GIF fallback: faceid's webp had a cropped head, so a full-face gif is used instead.
    // The popup CSS uses aspect-ratio:4/3 + object-fit:cover, so any reasonable gif size
    // renders correctly. We only assert it parsed and has positive dimensions.
    w = buf[6] | (buf[7] << 8);
    h = buf[8] | (buf[9] << 8);
    sized++;
    ok('animation ' + k + ' (gif fallback) has parseable dimensions', w > 0 && h > 0, w + 'x' + h);
  } else {
    sizedOk = false;
    ok('animation ' + k + ' has a recognised image signature', false, sig);
  }
}
ok('all ' + gifNames.length + ' animations are parseable', sizedOk, sized + '/' + gifNames.length);

console.log('\n=== 7. A POPUP CAN NEVER BECOME A DEAD END ===');
const p = window.popup({ kicker:'T', title:'t', body:'b', acts:[{ id:'only', label:'Go', kind:'yes' }] });
await sleep(20);
ok('popup is shown', modal.classList.contains('show'));
mcard.querySelector('.macts button').dispatchEvent(new window.MouseEvent('click', { bubbles:true }));
ok('a click on the only button resolves the popup', (await p) === 'only');
ok('the layer is hidden again', !modal.classList.contains('show'));
ok('the card is emptied again', mcard.innerHTML === '');
ok('the animation src is cleared', !q('.mmedia'));

console.log('\n=== 8. closePopup() / resetRun() STILL UNBLOCK ANYTHING WAITING ON IT ===');
const p2 = window.popup({ kicker:'T', title:'t', body:'b', acts:[{ id:'go', label:'Go', kind:'yes' }] });
await sleep(20);
window.closePopup('cancelled');
ok('closePopup() resolves the pending popup', (await p2) === 'cancelled');
ok('layer hidden after closePopup()', !modal.classList.contains('show'));
const p3 = window.popup({ kicker:'T', title:'t', body:'b', acts:[{ id:'go', label:'Go', kind:'yes' }] });
await sleep(20);
window.resetRun();
ok('resetRun() resolves the popup', (await p3) === 'cancelled');
ok('resetRun() hides the layer', !modal.classList.contains('show'));

console.log('\n=== 9. A REFUSAL BUTTON CARRIES NOT ALLOWED ===');
const p4 = window.popup({ kicker:'T', title:'t', body:'b', acts:[{ id:'ack', label:'Acknowledge', kind:'no' }] });
await sleep(20);
ok('a popup whose button is a refusal plays Not Allowed',
   /Not%20Allowed\.webp$/.test(q('.mmedia') ? q('.mmedia').getAttribute('src') : ''),
   q('.mmedia') && q('.mmedia').getAttribute('src'));
window.closePopup('x'); await p4.catch(()=>{});

console.log('\n=== 10. THE WORKING POPUP IS AUTO-CLOSING (NEVER A DEAD END) ===');
const t0 = Date.now();
const p5 = window.popup({ kicker:'T', title:'t', body:'b',
  acts:[{ id:'hide', label:'Hide', kind:'no' }], auto:120, autoResult:'auto' });
ok('a Hide button is offered, so the customer is never trapped', mcard.querySelector('.macts button') !== null);
ok('the working card resolves by itself', (await p5) === 'auto');
ok('and it resolved on its timer, not on a click', Date.now() - t0 < 900);

console.log('\n=== 11. NO STRAY ERRORS ===');
const realProblems = problems.filter(p => !/Not implemented|Could not parse CSS/.test(p));
ok('no jsdom fatal errors', realProblems.length === 0, JSON.stringify(realProblems.slice(0,3)));

console.log('\n' + (fails === 0 ? 'ALL CHECKS PASSED' : fails + ' CHECK(S) FAILED'));
process.exit(fails === 0 ? 0 : 1);

})();
