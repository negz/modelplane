#!/usr/bin/env python3
"""Write one blinded judge input per case from harvested runs.

Usage: judge_inputs.py <gt dir> <out dir> <label.jsonl>...

Every review of a case, from every arm and run, gets an opaque ID. The judge
input lists them with the case's ground truth. The key mapping IDs back to
arms and runs goes to <out dir>/key.json.
"""

import json
import pathlib
import random
import sys

HERE = pathlib.Path(__file__).parent


def main() -> int:
    gt_dir, out_dir = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    by_pr: dict[int, list[dict]] = {}
    for path in sys.argv[3:]:
        for line in open(path):
            r = json.loads(line)
            by_pr.setdefault(r["pr"], []).append(r)
    rng = random.Random(0)
    key = {}
    for pr, recs in sorted(by_pr.items()):
        case = json.loads((HERE / "cases" / f"{pr}.json").read_text())
        gt = json.loads((gt_dir / f"{pr}.json").read_text())
        rng.shuffle(recs)
        parts = [
            f"# PR #{pr}: {case['context']['title']}\n",
            f"base: {case['base']}\nhead: {case['head']}\n",
            "## PR description at review time\n",
            case["context"]["body"] + "\n",
            "## Ground truth\n",
            "```json\n" + json.dumps(gt["issues"], indent=2) + "\n```\n",
            "## Reviews\n",
        ]
        for i, r in enumerate(recs, 1):
            rid = f"R{pr}-{i}"
            key[rid] = {"label": r["label"], "run": r["run"]}
            parts.append(f"### Review {rid}\n")
            if "error" in r:
                parts.append(f"(no review: {r['error']})\n")
                continue
            body = r["body"].split("<!-- modelplane-review")[0].strip()
            for c in r["moved"]:
                body = body.replace(f"\n\n`{c['path']}` line {c['line']}: {c['body']}", "")
            parts.append("#### Summary\n\n" + body + "\n")
            for j, c in enumerate(r["inline"], 1):
                parts.append(f"#### c{j}: {c['path']}:{c['line']}\n\n{c['body']}\n")
            for j, c in enumerate(r["moved"], 1):
                parts.append(f"#### moved{j}: {c['path']}:{c['line']}\n\n{c['body']}\n")
        (out_dir / f"{pr}.md").write_text("\n".join(parts))
    (out_dir / "key.json").write_text(json.dumps(key, indent=2) + "\n")
    print(f"{len(by_pr)} cases, {len(key)} reviews")
    return 0


if __name__ == "__main__":
    sys.exit(main())
