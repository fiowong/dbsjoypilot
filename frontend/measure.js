/* ============================================================================
   NOT A BROWSER SCRIPT. DO NOT ADD A <script src="measure.js"> TO index.html.
   This file uses require() and child_process, which do not exist in a browser,
   so loading it there would throw and take the whole page down with it.
   ----------------------------------------------------------------------------
   Real-browser layout probe (QA TOOL, run by hand, never served).
   jsdom has no layout engine, so it cannot tell whether the logo is stretched or
   whether the phone keeps phone proportions. This launches headless Chrome,
   attaches over the DevTools protocol, and reads back actual rendered geometry
   for all three screens (login -> home -> Joy chat).

   Run:  node measure.js
   ============================================================================ */
const { spawn } = require('child_process');
const path = require('path');
const os = require('os');
const fs = require('fs');

/* Find a Chrome/Chromium to drive. Unlike the original probe this is not pinned to
   one Windows path, it looks wherever the OS actually keeps a browser. */
function findChrome() {
  const candidates = process.platform === 'darwin'
    ? ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
       '/Applications/Chromium.app/Contents/MacOS/Chromium',
       '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
       '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser']
    : process.platform === 'win32'
    ? [path.join(process.env['ProgramFiles'] || '', 'Google/Chrome/Application/chrome.exe'),
       path.join(process.env['ProgramFiles(x86)'] || '', 'Google/Chrome/Application/chrome.exe'),
       path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe')]
    : ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
       '/usr/bin/chromium', '/usr/bin/chromium-browser', '/snap/bin/chromium'];
  if (process.env.CHROME_PATH) candidates.unshift(process.env.CHROME_PATH);
  return candidates.find(p => p && fs.existsSync(p)) || null;
}

const CHROME = findChrome();
const PORT = 9334;
const DIR = __dirname;
const PROBE = path.join(DIR, '_probe.html');

if (!CHROME) {
  console.log('No Chrome/Chromium found. Set CHROME_PATH=/path/to/chrome and re-run.');
  process.exit(1);
}

// index.html + a no-op load hook (kept so the probe page is byte-identical in
// structure to the real page; measurement is done remotely over CDP).
fs.writeFileSync(PROBE,
  fs.readFileSync(path.join(DIR, 'index.html'), 'utf8')
    .replace('</body>', '<script>window.__PROBE__=1;</script>\n</body>'),
  'utf8');

const URL = 'file://' + encodeURI(PROBE);
const profile = path.join(os.tmpdir(), 'aj-cdp-' + Date.now());

const chrome = spawn(CHROME, [
  '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
  '--no-default-browser-check', '--disable-extensions',
  '--remote-debugging-port=' + PORT, '--remote-allow-origins=*',
  '--user-data-dir=' + profile, '--window-size=1500,1000', '--hide-scrollbars', URL,
], { stdio: 'ignore' });

const sleep = ms => new Promise(r => setTimeout(r, ms));
let fails = 0;
const ok = (n, c, x) => { console.log((c ? '  PASS  ' : '  FAIL  ') + n + (c ? '' : '   → ' + x)); if (!c) fails++; };

async function getTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
      const p = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
      if (p) return p;
    } catch (_) {}
    await sleep(250);
  }
  throw new Error('DevTools endpoint never came up');
}

const SNAP = `(() => {
  const b = s => { const e=document.querySelector(s); if(!e) return null;
    const r=e.getBoundingClientRect();
    return {w:+r.width.toFixed(1),h:+r.height.toFixed(1),left:+r.left.toFixed(1),top:+r.top.toFixed(1)}; };
  const vis = s => { const e=document.querySelector(s); if(!e) return false;
    return getComputedStyle(e).display !== 'none'; };
  const logo = document.querySelector('#scrLogin .lgform-head img.dbslogo');
  const lr = logo && logo.getBoundingClientRect();
  const isl = b('.island'), tm = b('.sb-time'), sg = b('.sb-right');
  const rgt = o => o ? o.left + o.w : null;
  return JSON.stringify({
    viewport:{w:innerWidth,h:innerHeight},
    activeScr:['scrLogin','appwin','scrJoy'].filter(s=>{const e=document.getElementById(s);return e&&e.classList.contains('on');}),
    loginLogo: lr ? {w:+lr.width.toFixed(1),h:+lr.height.toFixed(1),left:+lr.left.toFixed(1),top:+lr.top.toFixed(1),
                     ratio:+(lr.width/lr.height).toFixed(3),natural:logo.naturalWidth+'x'+logo.naturalHeight} : null,
    device:b('.device'), screen:b('.device-screen'), stage:b('.stage'),
    insVisible: vis('.ins'), ins:b('.ins'),
    island:isl, statusbar:b('.statusbar'),
    gapTimeToIsland:(isl&&tm)?+(isl.left-rgt(tm)).toFixed(1):null,
    gapIslandToSig:(isl&&sg)?+(sg.left-rgt(isl)).toFixed(1):null,
    appbar:b('#appwin .appbar'), appbody:b('.appbody'),
    abar:b('#scrJoy .abar'), chat:b('#chat'), langtog:b('.langtog'), cinput:b('.cinput'),
    loginBtn:b('#lgSubmit'), enableBtn:b('.enable'),
    lgform:b('.lgform'), lgformHead:b('.lgform-head')
  });
})()`;

(async () => {
  let ws;
  try {
    const target = await getTarget();
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let id = 0;
    const send = (method, params) => new Promise(res => {
      const myId = ++id;
      const onMsg = ev => { const m = JSON.parse(ev.data); if (m.id === myId) { ws.removeEventListener('message', onMsg); res(m); } };
      ws.addEventListener('message', onMsg);
      ws.send(JSON.stringify({ id: myId, method, params }));
    });
    const evalJs = async expr => {
      const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
      if (r.result && r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
      return r.result.result.value;
    };
    const snap = async () => JSON.parse(await evalJs(SNAP));
    const click = sel => evalJs(`(document.querySelector('${sel}').dispatchEvent(new MouseEvent('click',{bubbles:true})),1)`);

    await send('Runtime.enable', {});
    await sleep(2500);

    // ---------- LOGIN ----------
    let s = await snap();
    console.log('chrome              : ' + CHROME);
    console.log('viewport            : ' + s.viewport.w + ' x ' + s.viewport.h);
    console.log('\n--- 1. LOGIN FORM (in the device) ---');
    // The User ID / PIN form IS the login screen (no launchpad since 2 Oct), so it should be
    // visible immediately at boot and fill the screen.
    ok('credential form is visible at boot (no launchpad behind it)', s.lgform.h > 0,
       s.lgform.h + 'px tall');
    ok('login form fills the screen width', Math.abs(s.lgform.w - s.screen.w) < 2,
       s.lgform.w + ' vs screen ' + s.screen.w);
    console.log('  device            : ' + s.device.w + ' x ' + s.device.h + '  ratio ' + (s.device.h / s.device.w).toFixed(3));
    ok('device keeps phone proportions (ratio > 1.95)', (s.device.h / s.device.w) > 1.95,
       (s.device.h / s.device.w).toFixed(3));
    console.log('  bezel             : ' + ((s.device.w - s.screen.w) / 2).toFixed(1) + 'px per side');
    ok('bezel is present on both sides', (s.device.w - s.screen.w) / 2 >= 10,
       ((s.device.w - s.screen.w) / 2).toFixed(1) + 'px');
    ok('inspector hidden while in the app', s.insVisible === false);
    ok('dynamic island is horizontally centred in the screen',
       Math.abs((s.island.left + s.island.w / 2) - (s.screen.left + s.screen.w / 2)) < 1.5,
       'island cx ' + (s.island.left + s.island.w / 2).toFixed(1) + ' vs screen cx ' + (s.screen.left + s.screen.w / 2).toFixed(1));
    ok('island does not overlap the status-bar time', s.gapTimeToIsland > 0, s.gapTimeToIsland + 'px');
    ok('island does not overlap the status-bar icons', s.gapIslandToSig > 0, s.gapIslandToSig + 'px');
    // ---------- LOG IN FORM contents (already on screen at boot) ----------
    console.log('\n--- 1b. LOG IN FORM contents ---');
    const L = s.loginLogo;
    if (!L) throw new Error('login logo not found, #scrLogin .lgform-head img.dbslogo');
    console.log('  login logo        : ' + L.w + ' x ' + L.h + '  rendered ratio ' + L.ratio
      + '  (asset ' + L.natural + ' = ' + (1280 / 382).toFixed(3) + ')');
    const drift = Math.abs(L.ratio - 1280 / 382) / (1280 / 382);
    ok('logo aspect ratio preserved (not stretched)', drift < 0.02, 'drift ' + (drift * 100).toFixed(2) + '%');
    // The form's own 22px padding is the content edge the logo should sit on, left-aligned
    // the way the real digibank form is, never centred.
    const formPad = L.left - s.lgform.left;
    const centredLeft = s.lgform.left + (s.lgform.w - L.w) / 2;
    ok('logo is flush to the form content edge (left-aligned, not centred)',
       formPad > 0 && formPad < 30 && Math.abs(L.left - centredLeft) > 20,
       'offset from form box ' + formPad.toFixed(1) + 'px; centred would be ' + centredLeft.toFixed(1)
       + ', actual ' + L.left);
    ok('log-in form fits inside the screen (no clipping)', s.lgform.h <= s.screen.h + 1,
       s.lgform.h + ' vs ' + s.screen.h);
    ok('login button is inside the device', s.loginBtn.left >= s.device.left &&
       s.loginBtn.left + s.loginBtn.w <= s.device.left + s.device.w);

    // ---------- HOME ----------
    await click('#lgSubmit'); await sleep(400);
    s = await snap();
    console.log('\n--- 2. DBS HOME (in the device) ---');
    ok('home screen is active', s.activeScr.length === 1 && s.activeScr[0] === 'appwin', JSON.stringify(s.activeScr));
    ok('inspector still hidden in the app', s.insVisible === false);
    console.log('  app bar           : ' + s.appbar.w + ' x ' + s.appbar.h);
    ok('app bar clears the status bar / island', s.appbar.h >= s.statusbar.h,
       'appbar h ' + s.appbar.h + ' vs statusbar h ' + s.statusbar.h);
    ok('enable button fits the device width', s.enableBtn.w <= s.screen.w, s.enableBtn.w + ' vs ' + s.screen.w);

    // ---------- AUNTIEJOY ----------
    await click('.enable'); await sleep(400);
    s = await snap();
    console.log('\n--- 3. AUNTIEJOY CHAT (in the device) ---');
    ok('chat screen is active', s.activeScr.length === 1 && s.activeScr[0] === 'scrJoy', JSON.stringify(s.activeScr));
    ok('inspector is now shown alongside the device', s.insVisible === true);
    ok('device and inspector sit side by side (inspector has real width)', s.ins.w > 200, s.ins.w + 'px');
    ok('chat header clears the status bar / island', s.abar.h >= s.statusbar.h,
       'abar h ' + s.abar.h + ' vs statusbar h ' + s.statusbar.h);
    console.log('  chat area         : ' + s.chat.w + ' x ' + s.chat.h);
    console.log('  composer          : ' + s.cinput.w + ' x ' + s.cinput.h);
    ok('chat area has real height', s.chat.h > 300, s.chat.h + 'px');
    // .langtog now lives INSIDE .cinput (one composer unit), so its height is already
    // included in s.cinput.h, adding it separately would double-count it.
    const stack = s.abar.h + s.chat.h + s.cinput.h;
    ok('header + chat + composer exactly fill the screen',
       Math.abs(stack - s.screen.h) < 2,
       stack.toFixed(1) + ' vs ' + s.screen.h);
    ok('device + inspector fit the stage width',
       s.device.w + s.ins.w <= s.stage.w + 2,
       s.device.w + ' + ' + s.ins.w + ' vs ' + s.stage.w);
  } catch (e) {
    console.error('\nprobe failed:', e.message);
    process.exitCode = 1;
  } finally {
    try { ws && ws.close(); } catch (_) {}
    chrome.kill();
    await sleep(300);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (_) {}
    try { fs.rmSync(PROBE, { force: true }); } catch (_) {}
    console.log('\n' + (fails === 0 ? 'LAYOUT OK' : fails + ' LAYOUT CHECK(S) FAILED'));
    process.exit(process.exitCode || (fails ? 1 : 0));
  }
})();
