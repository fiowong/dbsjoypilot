/* Step 5 (frontend half): the merged UI must actually load.
   The merge paired MY index.html with HIS shell.js/live.js/ui.js -- a clean merge of two files
   that reference different ids is still a broken page. So check, against the shipped files:
     1. every <script src>, <link href> and assets/ reference resolves to a real file
     2. every id the JS looks up exists in the markup (or is one the JS itself creates)
   Reports; does not fix. */
const fs = require('fs'), path = require('path');
const dir = process.argv[2];
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');

const jsFiles = [...html.matchAll(/<script src="([^"]+)"/g)].map(m => m[1].split('?')[0]);
const linkFiles = [...html.matchAll(/<link[^>]+href="([^"]+)"/g)].map(m => m[1].split('?')[0]);
const assetRefs = [...html.matchAll(/(?:src|href)="(assets\/[^"?]+)/g)].map(m => m[1]);

console.log('=== 1. REFERENCED FILES EXIST ===');
let missing = 0;
for (const f of [...new Set([...jsFiles, ...linkFiles, ...assetRefs])]) {
  const p = path.join(dir, f);
  const ok = fs.existsSync(p);
  if (!ok) missing++;
  console.log(`  ${ok ? 'OK  ' : 'MISS'} ${f}`);
}
console.log(`  -> ${missing} missing`);

console.log('\n=== 2. SCRIPTS THE PAGE DECLARES ===');
console.log('  ' + jsFiles.join(', '));

const jsSrc = jsFiles.filter(f => fs.existsSync(path.join(dir, f)))
  .map(f => fs.readFileSync(path.join(dir, f), 'utf8')).join('\n');
const uiSrc = jsSrc;

// ids the markup defines
const declared = new Set([...html.matchAll(/\sid="([^"]+)"/g)].map(m => m[1]));

// ids the JS looks up
const looked = new Set();
for (const re of [/\$\('([^']+)'\)/g, /getElementById\('([^']+)'\)/g,
                  /querySelector\('#([A-Za-z][\w-]*)'\)/g, /\$\$?\('#([A-Za-z][\w-]*)'\)/g]) {
  for (const m of jsSrc.matchAll(re)) looked.add(m[1]);
}
// ids the JS creates (so a lookup for one is legitimate)
const created = new Set();
for (const re of [/id="([A-Za-z][\w-]*)"/g, /\.id\s*=\s*'([^']+)'/g,
                  /setAttribute\('id',\s*'([^']+)'\)/g]) {
  for (const m of jsSrc.matchAll(re)) created.add(m[1]);
}

const broken = [...looked].filter(id => !declared.has(id) && !created.has(id)).sort();
console.log('\n=== 3. JS LOOKS UP AN ID THE PAGE MUST HAVE ===');
console.log(`  declared in markup : ${declared.size}`);
console.log(`  looked up by JS    : ${looked.size}`);
console.log(`  created by JS      : ${created.size}`);
console.log(broken.length ? '  BROKEN: ' + broken.join(', ') : '  none broken');
process.exit(missing === 0 && broken.length === 0 ? 0 : 1);
