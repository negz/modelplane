#!/usr/bin/env python3
"""Turn a Review Eval run's artifacts into one JSON record per review.

Usage: harvest.py <artifacts dir> <git dir> <out.jsonl>

Each artifact dir holds the out.tar a review VM wrote. This parses the review
the same way post_review.py does, places its comments against the case's diff
as GitHub would, and records the cost and timing of each agent session, and
whether any session read the upstream PR it replays.
"""

import io
import json
import pathlib
import re
import subprocess
import sys
import tarfile

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import post_review  # noqa: E402


def patches(git_dir: str, base: str, head: str) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    names = subprocess.run(
        ["git", "-C", git_dir, "diff", "--name-only", base, head], check=True, capture_output=True, text=True
    ).stdout.split()
    for name in names:
        diff = subprocess.run(
            ["git", "-C", git_dir, "diff", base, head, "--", name], check=True, capture_output=True, text=True
        ).stdout
        hunks = diff[diff.find("\n@@") + 1 :] if "\n@@" in diff else ""
        out[name] = post_review.commentable(hunks)
    return out


def leaks(pr: int, text: str) -> list[str]:
    pats = [
        rf"modelplaneai/modelplane/pull/{pr}\b",
        rf"modelplaneai/modelplane/pulls/{pr}\b",
        rf"pr (?:view|diff|checks) {pr}\b[^\n]*modelplaneai",
        rf"modelplaneai/modelplane[^\n]*pr (?:view|diff) {pr}\b",
        rf"repos/modelplaneai/modelplane/(?:pulls|issues)/{pr}\b",
    ]
    return sorted({m.group(0) for p in pats for m in re.finditer(p, text)})


def main() -> int:
    arts, git_dir, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    records = []
    for d in sorted(pathlib.Path(arts).iterdir()):
        tar_path = d / "out.tar"
        label, pr, run = d.name.rsplit("-", 2)
        case = json.loads((HERE / "cases" / f"{pr}.json").read_text())
        rec: dict = {"label": label, "pr": int(pr), "run": int(run)}
        if not tar_path.exists():
            rec["error"] = "no output"
            records.append(rec)
            continue
        data = tar_path.read_bytes()
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            files = {m.name.removeprefix("./"): m for m in t.getmembers() if m.isfile()}
            sessions = []
            transcript_text = ""
            for name, m in files.items():
                if name.startswith("transcript/") and name.endswith(".json"):
                    raw = t.extractfile(m).read().decode()
                    transcript_text += raw
                    try:
                        info = json.loads(raw)["info"]
                    except (ValueError, KeyError):
                        continue
                    sessions.append(
                        {
                            "title": info.get("title"),
                            "agent": info.get("agent"),
                            "cost": info.get("cost"),
                            "minutes": round((info["time"]["updated"] - info["time"]["created"]) / 60000, 1),
                        }
                    )
            rec["sessions"] = sessions
            rec["cost"] = round(sum(s["cost"] or 0 for s in sessions), 2)
            rec["leaks"] = leaks(int(pr), transcript_text)
            raw_files = {}
            for name, m in files.items():
                if name.startswith("review/"):
                    raw_files[name.removeprefix("review/")] = t.extractfile(m).read().decode()
            rec["raw"] = raw_files
            t.fileobj.seek(0)
            try:
                review = post_review.parse(tarfile.open(fileobj=io.BytesIO(data)))
            except ValueError as e:
                rec["error"] = str(e)
                records.append(rec)
                continue
        body, inline = post_review.place(review, patches(git_dir, case["base"], case["head"]), case["head"])
        rec["complete"] = review.complete
        rec["summary"] = review.summary
        rec["body"] = body
        rec["inline"] = [c._asdict() for c in inline]
        rec["moved"] = [c._asdict() for c in review.comments if c not in inline]
        rec["parsed_comments"] = len(review.comments)
        rec["raw_comments"] = len([n for n in raw_files if n.startswith("comments/")])
        records.append(rec)
    with open(out_path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print(f"{len(records)} records, {sum(1 for r in records if 'error' in r)} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
