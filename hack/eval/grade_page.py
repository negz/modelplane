#!/usr/bin/env python3
"""Build a blind pairwise grading page from two arms' harvested runs.

Usage: grade_page.py <round> <armA.jsonl> <armB.jsonl> <out.html> <key.json>

Each case shows one review from each arm, as X and Y in random order. The key
mapping X and Y back to arms goes to key.json, which the page doesn't read.
"""

import html
import json
import pathlib
import random
import sys

HERE = pathlib.Path(__file__).parent
FORK = "https://github.com/negz/modelplane"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Review grading: {round}</title>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<style>
body {{ font: 15px/1.5 -apple-system, system-ui, sans-serif; margin: 0 auto; max-width: 1500px; padding: 1em 2em; color: #1f2328; }}
.case {{ border-top: 3px solid #d0d7de; margin-top: 2em; padding-top: 1em; }}
.cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5em; }}
.review {{ border: 1px solid #d0d7de; border-radius: 6px; padding: 0.5em 1em; background: #fff; }}
.summary {{ background: #f6f8fa; border-radius: 6px; padding: 0.2em 0.8em; }}
.comment {{ border-left: 3px solid #0969da; margin: 1em 0; padding-left: 0.8em; }}
.loc {{ font: 12px monospace; }}
.labels label {{ margin-right: 1em; font-size: 13px; }}
.verdict {{ background: #fff8c5; padding: 0.6em 1em; border-radius: 6px; margin-top: 1em; }}
pre, code {{ background: #eff1f3; border-radius: 4px; }} pre {{ padding: 0.5em; overflow-x: auto; }}
textarea {{ width: 100%; height: 4em; }}
#export {{ position: fixed; right: 1em; bottom: 1em; padding: 0.8em 1.2em; font-size: 15px; }}
</style></head><body>
<h1>Review grading: {round}</h1>
<p>Each case shows two reviews of the same PR, from different versions of the
reviewer, in random order. For each comment, mark whether you'd want it posted.
Then pick the better review. Your answers save in this browser as you go. Use
<b>Export</b> when you're done and send me the file.</p>
{cases}
<button id="export">Export</button>
<script>
const KEY = "grading-{round}";
const state = JSON.parse(localStorage.getItem(KEY) || "{{}}");
document.querySelectorAll(".md").forEach(e => e.innerHTML = marked.parse(e.textContent));
document.querySelectorAll("input, textarea").forEach(e => {{
  const k = e.name;
  if (e.type === "radio") e.checked = state[k] === e.value;
  else if (e.type === "checkbox") e.checked = !!state[k];
  else e.value = state[k] || "";
  e.addEventListener("change", () => {{
    state[k] = e.type === "checkbox" ? e.checked : e.value;
    localStorage.setItem(KEY, JSON.stringify(state));
  }});
}});
document.getElementById("export").onclick = () => {{
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(state, null, 2)], {{type: "application/json"}}));
  a.download = KEY + ".json"; a.click();
}};
</script></body></html>
"""


def radios(name: str, options: list[tuple[str, str]]) -> str:
    return "".join(
        f'<label><input type="radio" name="{name}" value="{v}"> {html.escape(t)}</label>' for v, t in options
    )


def review_html(case: dict, rec: dict, side: str, pr: int) -> str:
    head = case["head"]
    out = [f'<div class="review"><h3>Review {side}</h3>']
    if "error" in rec:
        out.append(f"<p><i>This run produced no review: {html.escape(rec['error'])}</i></p></div>")
        return "".join(out)
    body = rec["body"].split("<!-- modelplane-review")[0].strip()
    out.append(f'<div class="summary md">{html.escape(body)}</div>')
    for i, c in enumerate(rec["inline"], 1):
        url = f"{FORK}/blob/{head}/{c['path']}#L{c['line']}"
        name = f"{pr}-{side}-c{i}"
        out.append(
            f'<div class="comment"><div class="loc"><a href="{url}" target="_blank">'
            f"{html.escape(c['path'])}:{c['line']}</a></div>"
            f'<div class="md">{html.escape(c["body"])}</div>'
            f'<div class="labels">{radios(name, [("post", "Post"), ("skip", "Not worth posting"), ("wrong", "Wrong")])}</div></div>'
        )
    if not rec["inline"]:
        out.append("<p><i>No inline comments.</i></p>")
    out.append("</div>")
    return "".join(out)


def main() -> int:
    round_, a_path, b_path, out_path, key_path = sys.argv[1:6]
    arms = {}
    for label, path in (("A", a_path), ("B", b_path)):
        recs = [json.loads(line) for line in open(path)]
        by_pr: dict[int, dict] = {}
        for r in sorted(recs, key=lambda r: r["run"]):
            by_pr.setdefault(r["pr"], r)
        arms[label] = (pathlib.Path(path).stem, by_pr)
    rng = random.Random(round_)
    key = {}
    cases = []
    for pr in sorted(set(arms["A"][1]) & set(arms["B"][1])):
        case = json.loads((HERE / "cases" / f"{pr}.json").read_text())
        order = ["A", "B"]
        rng.shuffle(order)
        key[str(pr)] = {"X": arms[order[0]][0], "Y": arms[order[1]][0]}
        diff = f"{FORK}/compare/eval/{pr}-base...eval/{pr}-head"
        cols = "".join(
            review_html(case, arms[arm][1][pr], side, pr) for side, arm in zip(("X", "Y"), order, strict=True)
        )
        cases.append(
            f'<div class="case"><h2>#{pr}: {html.escape(case["context"]["title"])}</h2>'
            f'<p><a href="{diff}" target="_blank">Diff</a> · '
            f'<a href="https://github.com/modelplaneai/modelplane/pull/{pr}" target="_blank">Original PR</a> '
            f"(how it was really reviewed)</p>"
            f'<div class="cols">{cols}</div>'
            f'<div class="verdict"><b>Better review:</b> '
            f"{radios(f'{pr}-pref', [('X', 'X'), ('tie', 'Tie'), ('Y', 'Y')])}"
            f'<br><label><input type="checkbox" name="{pr}-happy"> I\'d be happy to post the better one as is</label>'
            f'<br><textarea name="{pr}-notes" placeholder="Notes (optional)"></textarea></div></div>'
        )
    pathlib.Path(out_path).write_text(PAGE.format(round=html.escape(round_), cases="".join(cases)))
    pathlib.Path(key_path).write_text(json.dumps(key, indent=2) + "\n")
    print(f"{len(cases)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
