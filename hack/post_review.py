#!/usr/bin/env python3
# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Post a review the review agent wrote as a GitHub pull request review.

The agent runs the PR's code, so its output is untrusted. This checks the
review's shape and size, and always posts a COMMENT review: the reviewer never
approves or requests changes. GitHub rejects a whole review if one comment
targets a line outside the diff, so comments on such lines move into the
summary instead. The summary ends with a marker recording the commit the
review covered, which the next review reads to review only what's new.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys
import typing

MAX_COMMENTS = 8
MAX_SUMMARY = 20_000
MAX_COMMENT = 4_000

MARKER = re.compile(r"<!--\s*modelplane-review\b.*?-->", re.DOTALL)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")


class Comment(typing.NamedTuple):
    """An inline review comment."""

    path: str
    line: int
    body: str


class Review(typing.NamedTuple):
    """A review summary and its inline comments."""

    summary: str
    comments: list[Comment]


def parse(raw: str) -> Review:
    """The review in raw, or ValueError if it isn't the shape the agent was told
    to write."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("review must be an object")
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary must be a non-empty string")
    if len(summary) > MAX_SUMMARY:
        raise ValueError(f"summary is longer than {MAX_SUMMARY} characters")
    comments = data.get("comments", [])
    if not isinstance(comments, list):
        raise ValueError("comments must be a list")
    if len(comments) > MAX_COMMENTS:
        raise ValueError(f"review has {len(comments)} comments, more than {MAX_COMMENTS}")
    out = []
    for c in comments:
        if not isinstance(c, dict):
            raise ValueError(f"malformed comment: {c!r}")
        path, line, body = c.get("path"), c.get("line"), c.get("body")
        if not isinstance(path, str) or not isinstance(line, int) or line < 1 or not isinstance(body, str):
            raise ValueError(f"malformed comment: {c!r}")
        if len(body) > MAX_COMMENT:
            raise ValueError(f"comment on {path}:{line} is longer than {MAX_COMMENT} characters")
        out.append(Comment(path, line, body))
    return Review(summary, out)


def commentable(patch: str) -> set[int]:
    """The lines of a file's new version that a review comment can target: the
    added and context lines of its patch."""
    lines: set[int] = set()
    line = 0
    for text in patch.splitlines():
        if match := HUNK.match(text):
            line = int(match["start"])
            continue
        if text.startswith("-") or text.startswith("\\"):
            continue
        lines.add(line)
        line += 1
    return lines


def place(review: Review, diff: dict[str, set[int]], sha: str) -> tuple[str, list[Comment]]:
    """The review body and the comments GitHub accepts inline. A comment on a
    line outside the diff moves into the body."""
    inline = [c for c in review.comments if c.line in diff.get(c.path, set())]
    outside = [c for c in review.comments if c not in inline]
    # A marker the agent wrote would set where the next review starts.
    body = MARKER.sub("", review.summary).strip()
    if outside:
        body += "\n\n" + "\n\n".join(f"`{c.path}` line {c.line}: {c.body}" for c in outside)
    body += f"\n\n<!-- modelplane-review sha={sha} -->"
    return body, inline


def gh(*args: str, stdin: str | None = None) -> str:
    """Run gh, returning its output."""
    return subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=True).stdout


def main() -> int:
    """Post the review."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("review", type=pathlib.Path)
    args = parser.parse_args()

    try:
        review = parse(args.review.read_text())
    except ValueError as e:
        sys.stderr.write(f"invalid review: {e}\n")
        return 1

    base = gh("api", f"repos/{args.repo}/pulls/{args.pr}", "--jq", ".base.sha").strip()
    compare = json.loads(gh("api", f"repos/{args.repo}/compare/{base}...{args.sha}"))
    diff = {f["filename"]: commentable(f.get("patch", "")) for f in compare["files"]}

    body, inline = place(review, diff, args.sha)
    payload = {
        "commit_id": args.sha,
        "event": "COMMENT",
        "body": body,
        "comments": [{"path": c.path, "line": c.line, "side": "RIGHT", "body": c.body} for c in inline],
    }
    gh(
        "api",
        "--method",
        "POST",
        f"repos/{args.repo}/pulls/{args.pr}/reviews",
        "--input",
        "-",
        stdin=json.dumps(payload),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
