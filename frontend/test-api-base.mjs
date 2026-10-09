/* Proves the frontend's API-base resolver picks the right backend for every way the page
   can be served. Run with:  node frontend/test-api-base.mjs
   (No dependencies, no jsdom -- the resolver is evaluated in a bare vm sandbox.)

   Why this file exists: the resolver is the ONLY thing that lets the same frontend talk to a
   laptop on :8078 and to a deployed nginx origin, with no code change. If it breaks, the app
   loads and then silently talks to nothing. That is worth a test.

   The case that matters most for a domain-less deploy is #4: served over plain HTTP on a bare
   IP, port 80. The old rule only treated https as "deployed", so it returned http://<ip>:8078
   -- a port that is deliberately closed to the internet. It must return the origin. */
import { readFileSync } from 'node:fs';
import { createContext, runInContext } from 'node:vm';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, 'scenarios.js'), 'utf8');

const start = source.indexOf('const API_BASE = (() => {');
const end = source.indexOf('})();', start);
if (start < 0 || end < 0) {
  console.error('FAIL: could not locate the API_BASE block in scenarios.js');
  process.exit(1);
}
// `const` at the top level of a vm script does not attach to the sandbox global, so export it.
const block = source.slice(start, end + 5) + '\n;globalThis.__OUT = API_BASE;';

const CASES = [
  ['window override wins',      { AUNTIEJOY_API_BASE: 'https://api.example.com/' }, 'https:', 'x',           '',   'https://x',              'https://api.example.com'],
  ['?api= wins',                {},                                                'http:',  '127.0.0.1',   '8098', 'http://127.0.0.1:8098',  'https://tunnel.ngrok.io'],
  ['https -> own origin',       {},                                                'https:', 'dbsjoy.space', '',   'https://dbsjoy.space',   'https://dbsjoy.space'],
  ['http:80 bare IP -> origin', {},                                                'http:',  '43.156.1.2',  '',   'http://43.156.1.2',      'http://43.156.1.2'],
  ['http:8098 dev -> :8078',    {},                                                'http:',  '127.0.0.1',   '8098', 'http://127.0.0.1:8098',  'http://127.0.0.1:8078'],
  ['http:8077 jsdom -> :8078',  {},                                                'http:',  '127.0.0.1',   '8077', 'http://127.0.0.1:8077',  'http://127.0.0.1:8078'],
];

let failures = 0;
for (const [name, globals, protocol, hostname, port, origin, expected] of CASES) {
  const loc = { protocol, hostname, port, origin, search: '' };
  if (name === '?api= wins') loc.search = '?api=https://tunnel.ngrok.io';

  const sandbox = { window: { location: loc, ...globals }, location: loc, URLSearchParams };
  createContext(sandbox);

  let actual;
  try {
    runInContext(block, sandbox);
    actual = sandbox.__OUT;
  } catch (err) {
    actual = `THREW ${err.message}`;
  }

  const ok = actual === expected;
  if (!ok) failures += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(26)} -> ${actual}`);
  if (!ok) console.log(`        expected ${expected}`);
}

console.log('');
console.log(`${CASES.length - failures}/${CASES.length} API-base resolver cases passed.`);
process.exit(failures ? 1 : 0);
