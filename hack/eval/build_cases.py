#!/usr/bin/env python3
"""Freeze upstream PRs as review eval cases.

Each case replays a PR at the commit its maintainers first reviewed. Its
context holds the PR as it stood then, with no reviews or comments, so the
agent can't read how the review went. Run from a clone with an `upstream`
remote. It writes hack/eval/cases/<pr>.json and prints the git refs to push.
"""

import json
import pathlib
import subprocess
import sys

UPSTREAM = "modelplaneai/modelplane"
FORK = "negz/modelplane"
OUT = pathlib.Path(__file__).parent / "cases"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def body_at(number: int, when: str, current: str) -> str:
    q = (
        'query($n:Int!){repository(owner:"modelplaneai",name:"modelplane"){pullRequest(number:$n)'
        "{userContentEdits(first:100){nodes{editedAt diff}}}}}"
    )
    data = json.loads(gh("api", "graphql", "-f", f"query={q}", "-F", f"n={number}"))
    edits = data["data"]["repository"]["pullRequest"]["userContentEdits"]["nodes"]
    if not edits:
        return current
    before = [e for e in edits if e["editedAt"] <= when]
    if before:
        return max(before, key=lambda e: e["editedAt"])["diff"]
    return min(edits, key=lambda e: e["editedAt"])["diff"]


def issues(pr: dict, when: str) -> list[dict]:
    out = []
    for ref in pr["closingIssuesReferences"]:
        i = json.loads(gh("issue", "view", str(ref["number"]), "-R", UPSTREAM, "--json", "number,title,body,comments"))
        i["comments"] = [c for c in i["comments"] if c["createdAt"] <= when]
        out.append(i)
    return out


def main() -> int:
    OUT.mkdir(exist_ok=True)
    for line in sys.stdin:
        number, head, base, when = line.split()[:4]
        n = int(number)
        pr = json.loads(gh("pr", "view", number, "-R", UPSTREAM, "--json", "title,body,author,closingIssuesReferences"))
        commits = []
        for oid in git("rev-list", "--reverse", f"{base}..{head}").split():
            commits.append(
                {
                    "oid": oid,
                    "messageHeadline": git("log", "-1", "--format=%s", oid),
                    "messageBody": git("log", "-1", "--format=%b", oid),
                    "authors": [{"name": git("log", "-1", "--format=%an", oid)}],
                }
            )
        files = []
        for row in git("diff", "--numstat", base, head).splitlines():
            add, delete, path = row.split("\t")
            files.append(
                {
                    "path": path,
                    "additions": int(add) if add != "-" else 0,
                    "deletions": int(delete) if delete != "-" else 0,
                }
            )
        case = {
            "pr": n,
            "base": base,
            "head": head,
            "reviewed_at": when,
            "context": {
                "number": n,
                "title": pr["title"],
                "body": body_at(n, when, pr["body"]),
                "author": pr["author"],
                "baseRefName": f"eval/{n}-base",
                "headRefOid": head,
                "commits": commits,
                "closingIssuesReferences": issues(pr, when),
                "files": files,
            },
        }
        (OUT / f"{n}.json").write_text(json.dumps(case, indent=2) + "\n")
        print(f"{base}:refs/heads/eval/{n}-base {head}:refs/heads/eval/{n}-head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
