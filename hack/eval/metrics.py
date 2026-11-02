#!/usr/bin/env python3
"""Print scripted metrics for harvested eval runs.

Usage: metrics.py <label.jsonl>...
"""

import collections
import json
import re
import sys


def opener(text: str) -> str:
    return re.split(r"(?<=[.?!])\s", text.strip(), maxsplit=1)[0]


def main() -> int:
    for path in sys.argv[1:]:
        recs = [json.loads(line) for line in open(path)]
        print(f"== {path}")
        grams: collections.Counter[str] = collections.Counter()
        for r in sorted(recs, key=lambda r: (r["pr"], r["run"])):
            if "error" in r:
                print(f"  #{r['pr']}/{r['run']}: ERROR {r['error']}")
                continue
            summary = r["summary"].strip()
            words = re.findall(r"[a-z']+", summary.lower())
            for g in {" ".join(words[i : i + 4]) for i in range(len(words) - 3)}:
                grams[g] += 1
            print(
                f"  #{r['pr']}/{r['run']}: ${r['cost']:.2f} inline={len(r['inline'])} moved={len(r['moved'])}"
                f" summary={len(summary)}c leaks={len(r['leaks'])} complete={r['complete']}"
            )
            print(f"    opener: {opener(summary)}")
        ok = [r for r in recs if "error" not in r]
        if ok:
            print(
                f"  total ${sum(r['cost'] for r in ok):.2f}, mean inline {sum(len(r['inline']) for r in ok) / len(ok):.1f},"
                f" mean summary {sum(len(r['summary'].strip()) for r in ok) / len(ok):.0f}c"
            )
        common = [(g, c) for g, c in grams.most_common(8) if c > 1]
        print(f"  repeated summary 4-grams: {common}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
