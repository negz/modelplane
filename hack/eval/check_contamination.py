#!/usr/bin/env python3
"""Fail if the reviewer's prompts mention or quote an eval case's PR.

Usage: check_contamination.py <reviewer checkout> <raw review dumps dir>

The raw dumps are each case PR's reviews and threads, as GraphQL returns them.
A prompt that names a case's PR number, or shares eight consecutive words with
a comment on it, has seen the answer the eval grades against.
"""

import json
import pathlib
import re
import sys

N = 8


def shingles(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9_`.'-]+", text.lower())
    return {tuple(words[i : i + N]) for i in range(len(words) - N + 1)}


def comments(dump: dict) -> list[str]:
    pr = dump["data"]["repository"]["pullRequest"]
    out = [r["body"] for r in pr["reviews"]["nodes"]]
    out += [c["body"] for t in pr["reviewThreads"]["nodes"] for c in t["comments"]["nodes"]]
    out += [c["body"] for c in pr["comments"]["nodes"]]
    return [b for b in out if b]


def main() -> int:
    root, dumps = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    prompts = {
        p: p.read_text()
        for d in (".agents", ".opencode")
        for p in (root / d).rglob("*.md")
    }
    found = 0
    for dump_path in sorted(dumps.glob("*.json")):
        n = dump_path.stem
        theirs = set().union(*(shingles(b) for b in comments(json.loads(dump_path.read_text()))))
        for path, text in prompts.items():
            if re.search(rf"#{n}\b|/pull/{n}\b", text):
                print(f"{path.relative_to(root)}: mentions #{n}")
                found += 1
            shared = shingles(text) & theirs
            if shared:
                print(f"{path.relative_to(root)}: quotes #{n}: {' '.join(sorted(shared)[0])}")
                found += 1
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
