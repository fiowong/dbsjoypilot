#!/usr/bin/env python3
"""Build a layout probe from the REAL index.html + styles.css.

Copies index.html, strips every <script src> (they need a backend), then appends a script that
fills #agents with realistically tall content -- the post-turn state in the user's screenshot --
and writes measured geometry into a <pre id="probe"> that Chrome's --dump-dom prints.

TWO scenarios, because the fix must not trade one panel for the other:
  A  audit TRIMMED (the default 3 rows) + tall agents   <- the reported bug
  B  audit EXPANDED (all rows, i.e. "View full audit trail") + tall agents
     <- this is the case the `max-height` cap on .psplit exists to protect: the audit must not
        be allowed to starve the control chain above it.

Run from the frontend dir so relative styles.css / assets/ paths resolve.
"""
import io
import re
import sys

src = io.open('index.html', encoding='utf-8').read()
# Drop the app scripts: they fetch a backend that is not running. Layout does not need them.
src = re.sub(r'<script src="[^"]*"></script>\s*', '', src)

PROBE = r"""
<pre id="probe" style="position:fixed;left:-9999px">pending</pre>
<script>
(function(){
  const card = (h, label) => {
    let rows = '';
    for (let i = 0; i < h; i++) rows += '  "check_' + i + '": "PASS",\n';
    return '<div class="acard"><div class="ah">' + label + '</div>'
         + '<details class="raw" open><summary>RAW PAYLOAD</summary>'
         + '<pre>{\n' + rows + '}</pre></details></div>';
  };
  document.getElementById('agents').innerHTML =
      card(22, 'INTENT CLASSIFIER') + card(3, 'PAYMENT AGENT')
    + card(3, 'INDEPENDENT VALIDATION AGENT') + card(3, 'PERSONALIZATION AGENT')
    + card(9, 'MOCK LEDGER');

  const audit = document.getElementById('audit');
  audit.classList.remove('placeholder');
  let rows = '';
  for (let i = 0; i < 32; i++) rows += '<div class="ae"><span class="t">00:46:0' + (i % 10)
      + '.000</span> EVENT_' + i + '</div>';
  audit.innerHTML = rows;

  function measure(tag){
    const panels = document.querySelectorAll('.ins > .panel');
    const out = [];
    panels.forEach((p, i) => {
      const pr = p.getBoundingClientRect();
      const list = p.querySelector('.agents, .chain');
      const ps = p.querySelector('.psplit');
      const lr = list.getBoundingClientRect(), sr = ps.getBoundingClientRect();
      out.push({
        tag: tag,
        panel: i === 0 ? 'LEFT  chain+audit' : 'RIGHT agents+outcome',
        panelH: Math.round(pr.height),
        listH: Math.round(lr.height),
        listScrollH: list.scrollHeight,
        listScrolls: list.scrollHeight > Math.ceil(lr.height) + 1,
        listUsable: Math.round(lr.height) >= 60,
        psplitH: Math.round(sr.height),
        psplitContentH: ps.scrollHeight,
        psplitSqueezed: ps.scrollHeight > Math.ceil(sr.height) + 1,
        psplitBottomInsidePanel: Math.round(sr.bottom) <= Math.round(pr.bottom) + 1,
      });
    });
    return out;
  }

  const results = measure('A trimmed');
  // Scenario B: the operator pressed "View full audit trail".
  document.getElementById('audit').classList.remove('trim');
  document.getElementById('auditMore').hidden = false;
  results.push.apply(results, measure('B expanded'));
  document.getElementById('probe').textContent = JSON.stringify(results);
})();
</script>
"""
io.open('_probe.html', 'w', encoding='utf-8').write(src + PROBE)
print('wrote _probe.html', file=sys.stderr)
