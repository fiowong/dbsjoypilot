/* Step 4 gate: run the four jsdom suites AS A SET.
   Each suite is spawned as its own child process, one after another. Chaining them in a
   single shell dies at exit 137 partway through (heap not released between runs), so the
   parent here only spawns, waits, and collects -- it never loads jsdom itself. */
const { spawnSync } = require('child_process');
const path = require('path');

const NODE = process.execPath;
const DIR = process.argv[2];
const SUITES = ['shell.test.js', 'popup.test.js', 'confirm.test.js', 'langpill.test.js'];

let allOk = true;
const rows = [];

for (const s of SUITES) {
  const r = spawnSync(NODE, ['--max-old-space-size=3072', path.join(DIR, s)], {
    cwd: DIR, encoding: 'utf8', timeout: 240000,
    env: { ...process.env, NODE_PATH: process.env.NODE_PATH },
  });
  const out = (r.stdout || '') + (r.stderr || '');
  const fails = out.split('\n').filter(l => /\bFAIL\b|CHECK\(S\) FAILED/.test(l));
  const passed = /ALL CHECKS PASSED/.test(out);
  const nPass = (out.match(/^\s*(ok|PASS)\b/gm) || []).length;
  const ok = r.status === 0 && passed;
  if (!ok) allOk = false;
  rows.push({ suite: s, exit: r.status, passed, ok, fails: fails.slice(0, 6) });
}

console.log('══════════ STEP 4 — FOUR SUITES AS A SET ══════════');
for (const r of rows) {
  console.log(
    `${r.ok ? 'PASS' : 'FAIL'}  ${r.suite.padEnd(18)} exit=${String(r.exit).padStart(4)}  ` +
    `allChecksPassed=${r.passed}`);
  for (const f of r.fails) console.log('        ' + f.trim());
}
console.log('───────────────────────────────────────────────────');
console.log(allOk ? 'SET RESULT: ALL FOUR GREEN' : 'SET RESULT: FAILURES ABOVE');
process.exit(allOk ? 0 : 1);
