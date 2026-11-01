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

The agent runs in a VM that writes its output as a tar: the review under
review/, as summary.md and one file per inline comment under review/comments/,
each starting with front matter that gives its path and line. The agent runs
the PR's code, so its output is untrusted. This reads the tar's members in
memory, without extracting it, checks the review's shape and size, and always
posts a COMMENT review: the reviewer never approves or requests changes. GitHub
rejects a whole review if one comment targets a line outside the diff, so
comments on such lines move into the summary instead. The summary ends with a
marker recording the commit the review covered, which the next review reads to
review only what's new.

A re-review passes --since, the commit the last review covered. Its inline
comments may then only target lines the PR adds that also changed since that
commit, so a re-review can't reopen the parts of the PR it already reviewed.
Comments on other lines are dropped rather than moved into the summary.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tarfile
import typing

# The bar decides how many comments a review has. This only stops a runaway.
MAX_COMMENTS = 25
MAX_SUMMARY = 20_000
MAX_COMMENT = 4_000

# The skill limits a comment's prose, outside any code block, to this many
# characters. A sketch of an alternative can run longer.
MAX_PROSE = 600

MARKER = re.compile(r"<!--\s*modelplane-review\b.*?-->", re.DOTALL)
FRONT_MATTER = re.compile(r"\A---\n(?P<meta>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
CODE_BLOCK = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


class Comment(typing.NamedTuple):
    """An inline review comment."""

    path: str
    line: int
    body: str


class Review(typing.NamedTuple):
    """A review summary and its inline comments."""

    summary: str
    comments: list[Comment]


def parse_comment(raw: str) -> Comment:
    """The comment in raw, or ValueError if it isn't a well-formed comment
    file."""
    match = FRONT_MATTER.match(raw)
    if match is None:
        raise ValueError("comment has no front matter")
    meta = dict(line.split(":", 1) for line in match["meta"].splitlines() if ":" in line)
    # A marker the agent wrote would set where the next review starts.
    body = MARKER.sub("", match["body"]).strip()
    path, line = meta.get("path", "").strip(), meta.get("line", "").strip()
    if not path or not line.isdigit() or int(line) < 1 or not body:
        raise ValueError(f"comment needs a path, a line and a body: {match['meta']!r}")
    if len(body) > MAX_COMMENT:
        raise ValueError(f"comment on {path}:{line} is longer than {MAX_COMMENT} characters")
    if len(CODE_BLOCK.sub("", body).strip()) > MAX_PROSE:
        raise ValueError(f"comment on {path}:{line} has more than {MAX_PROSE} characters of prose")
    return Comment(path, int(line), body)


def parse(output: tarfile.TarFile) -> Review:
    """The review in the VM's output, or ValueError if it isn't the shape the
    agent was told to write."""
    files: dict[str, str] = {}
    for member in output.getmembers():
        name = member.name.removeprefix("./")
        if not member.isfile() or not name.startswith("review/"):
            continue
        if member.size > MAX_SUMMARY:
            raise ValueError(f"{name} is larger than {MAX_SUMMARY} bytes")
        f = output.extractfile(member)
        if f is not None:
            files[name.removeprefix("review/")] = f.read().decode()
    summary = files.get("summary.md", "")
    if not summary.strip():
        raise ValueError("summary.md is missing or empty")
    if len(summary) > MAX_SUMMARY:
        raise ValueError(f"summary is longer than {MAX_SUMMARY} characters")
    comments = sorted(n for n in files if n.startswith("comments/") and n.endswith(".md"))
    if len(comments) > MAX_COMMENTS:
        raise ValueError(f"review has {len(comments)} comments, more than {MAX_COMMENTS}")
    return Review(MARKER.sub("", summary), [parse_comment(files[c]) for c in comments])


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


def added(patch: str) -> set[int]:
    """The lines of a file's new version that its patch adds."""
    lines: set[int] = set()
    line = 0
    for text in patch.splitlines():
        if match := HUNK.match(text):
            line = int(match["start"])
            continue
        if text.startswith("-") or text.startswith("\\"):
            continue
        if text.startswith("+"):
            lines.add(line)
        line += 1
    return lines


def changed(diff: str) -> dict[str, set[int]]:
    """The lines of each file's new version that a zero-context git diff
    changes, by file."""
    out: dict[str, set[int]] = {}
    path = ""
    for text in diff.splitlines():
        if text.startswith("+++ "):
            path = text.removeprefix("+++ ").removeprefix("b/")
            continue
        if (match := HUNK.match(text)) and path != "/dev/null":
            start, count = int(match["start"]), int(match["count"] or 1)
            out.setdefault(path, set()).update(range(start, start + count))
    return out


def place(
    review: Review, diff: dict[str, set[int]], sha: str, scope: dict[str, set[int]] | None = None
) -> tuple[str, list[Comment]]:
    """The review body and the comments GitHub accepts inline. Without a scope,
    a comment on a line outside the diff moves into the body. With one, only
    comments on lines in the scope are posted, and the rest are dropped."""
    allowed = diff if scope is None else scope
    inline = [c for c in review.comments if c.line in allowed.get(c.path, set())]
    outside = [c for c in review.comments if c not in inline]
    body = review.summary.strip()
    if outside and scope is None:
        body += "\n\n" + "\n\n".join(f"`{c.path}` line {c.line}: {c.body}" for c in outside)
    for c in outside if scope is not None else []:
        sys.stderr.write(f"dropping a comment on {c.path}:{c.line}, which hasn't changed since the last review\n")
    body += f"\n\n<!-- modelplane-review sha={sha} -->"
    return body, inline


def gh(*args: str, stdin: str | None = None) -> str:
    """Run gh, returning its output."""
    result = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def main() -> int:
    """Post the review."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--since", help="the commit the last review covered, for a re-review")
    parser.add_argument("--git-dir", type=pathlib.Path, help="a repository holding --sha and --since")
    parser.add_argument("output", type=pathlib.Path, help="the tar the review VM wrote")
    args = parser.parse_args()

    try:
        with tarfile.open(args.output) as output:
            review = parse(output)
    except (ValueError, tarfile.TarError, UnicodeDecodeError) as e:
        sys.stderr.write(f"invalid review: {e}\n")
        return 1

    base = gh("api", f"repos/{args.repo}/pulls/{args.pr}", "--jq", ".base.sha").strip()
    compare = json.loads(gh("api", f"repos/{args.repo}/compare/{base}...{args.sha}"))
    diff = {f["filename"]: commentable(f.get("patch", "")) for f in compare["files"]}

    scope = None
    if args.since:
        since = subprocess.run(
            ["git", f"--git-dir={args.git_dir}", "diff", "-U0", "--no-color", "--no-ext-diff", args.since, args.sha],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        new = changed(since)
        scope = {f["filename"]: added(f.get("patch", "")) & new.get(f["filename"], set()) for f in compare["files"]}

    body, inline = place(review, diff, args.sha, scope)
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
