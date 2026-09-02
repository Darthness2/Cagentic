"""The web transcript must only ever grow while a reply streams.

`md()` in the gateway's client is a whole-document parser, and the live turn
re-runs it on the half-arrived reply many times a second. Truncated input used
to render as a different block structure than the finished text — an open ```
fence as wrapped prose, a lone "-" as a paragraph, a table header as a
paragraph — so the answer's height fell as often as it rose while the viewport
was pinned to the bottom. That is what made the page bob.

The invariant is one-directional: at no point during a stream may the rendered
height drop. Checked against the real renderer through node, because a Python
reimplementation of it would only test itself.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "cagentic" / "gateway_assets" / "app.js"

# Every block construct the renderer treats specially, since those are exactly
# the ones whose shape changes as the characters arrive.
SAMPLES = {
    "prose": "An ordinary paragraph that runs on for a bit and then stops.",
    "headings": "# One\n\nsome text\n\n### Three\n\nmore text",
    "bullets": "Points:\n\n- first\n- second\n- third\n\nDone.",
    "numbered": "Steps:\n\n1. first\n2. second\n3. third\n",
    "nested": "Steps:\n\n1. first\n   - sub one\n   - sub two\n2. second\n\nOk.",
    "quote": "As noted:\n\n> a quote\n> more quote\n\nEnd.",
    "fence": "Code:\n\n```python\ndef go(x):\n    return x + 1\n\ngo(41)\n```\n\nThat is it.",
    "table": "| name | value |\n|---|---|\n| a | 1 |\n| b | 2 |\n",
    "mixed": (
        "Here it is.\n\n## Summary\n\n- alpha\n- beta\n\n```js\nconst a = 1;\n```\n\n"
        "| k | v |\n|---|---|\n| x | 9 |\n\nFinished."
    ),
    "rule": "before\n\n---\n\nafter",
}

# Pulls the pure renderer out of app.js and replays each sample one character at
# a time, scoring a crude but monotone-sensitive height for every frame.
HARNESS = r"""
const fs=require('fs');
const [APP,SAMPLES]=process.argv.slice(-2);
const src=fs.readFileSync(APP,'utf8');
function grab(name){
  const start=src.indexOf('function '+name+'(');
  if(start<0) throw new Error('app.js no longer defines '+name);
  let depth=0;
  for(let j=src.indexOf('{',start);j<src.length;j++){
    if(src[j]==='{')depth++;
    else if(src[j]==='}'&&!--depth) return src.slice(start,j+1);
  }
  throw new Error('unbalanced '+name);
}
const NAMES=['esc','_codeBlockHTML','_inline','_splitRow','_tableHTML','_renderList',
             '_mdBlocks','md','completeStreamingMarkdown'];
const api={};
new Function('exports','highlight','escText',
  NAMES.map(grab).join('\n')+'\nexports.md=md;exports.complete=completeStreamingMarkdown;'
)(api,x=>x,x=>x);

function height(html){
  let h=0;
  html.replace(/<(p|h[1-6]|li|tr|hr|blockquote|div class="codeblock")[^>]*>([\s\S]*?)(?=<\/)/g,
    (m,tag,inner)=>{
      const text=inner.replace(/<[^>]+>/g,'');
      h+=Math.max(1,Math.ceil(text.length/60))+(tag[0]==='h'?1.4:tag==='p'?0.8:0.2);
      return m;
    });
  return h;
}

const out={};
for(const [name,text] of Object.entries(JSON.parse(SAMPLES))){
  let prev=0, shrinks=0, worst=0;
  for(let n=1;n<=text.length;n++){
    const h=height(api.md(api.complete(text.slice(0,n),false)));
    if(prev-h>0.05){ shrinks++; worst=Math.max(worst,prev-h); }
    prev=h;
  }
  out[name]={shrinks,worst,
             // The settled render has to match a reloaded chat exactly.
             faithful: api.md(api.complete(text,true))===api.md(text)};
}
console.log(JSON.stringify(out));
"""


@unittest.skipIf(shutil.which("node") is None, "needs node to run the real renderer")
class TestStreamingHeightIsMonotone(unittest.TestCase):
    def setUp(self) -> None:
        proc = subprocess.run(
            ["node", "-e", HARNESS, "--", str(APP_JS), json.dumps(SAMPLES)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.report = json.loads(proc.stdout)

    def test_no_sample_ever_shrinks_mid_stream(self) -> None:
        shrinking = {k: v for k, v in self.report.items() if v["shrinks"]}
        self.assertEqual(shrinking, {}, f"the transcript loses height mid-stream: {shrinking}")

    def test_the_settled_render_matches_a_reloaded_chat(self) -> None:
        unfaithful = sorted(k for k, v in self.report.items() if not v["faithful"])
        self.assertEqual(unfaithful, [])

    def test_every_sample_was_actually_exercised(self) -> None:
        # A harness that silently rendered nothing would pass the two above.
        self.assertEqual(sorted(self.report), sorted(SAMPLES))
