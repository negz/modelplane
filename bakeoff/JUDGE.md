# E2E bake-off: judging instructions

You're judging a bake-off between four ways of writing end-to-end tests for
the Modelplane repo. Issue #473 (`gh issue view 473`) is the question behind
it. Four agents each built a prototype of one option against the same brief,
and the orchestrator then ran all four identically. Your job is to score them
impartially against the rubric, from the evidence.

Nobody has a preferred outcome. Judge the prototypes as evidence about each
option, and separate the two:
- a weakness of the option, which would persist however well it was
  implemented;
- a weakness of this prototype, which a better implementation would fix.

## Read first

- **The brief:**
  `/home/negz/control/modelplaneai/modelplane-soggy-bottom/bakeoff/BRIEF.md`.
  It covers the options, the scenarios (A1 to D1), the rules the builders
  followed, and the rubric.
- **The current e2e:** `e2e/run.sh` on upstream main. Run
  `git -C /home/negz/control/modelplaneai/modelplane show upstream/main:e2e/run.sh`.

## The prototypes

Each is a git worktree. See what changed with
`git -C <worktree> diff upstream/main..HEAD`. Each also has builder notes in
`e2e/BAKEOFF.md`.

| Option | Worktree |
|---|---|
| {ORDER} |

## The runs

Logs are in `/tmp/bakeoff/runs/`. `summary.txt` has each run's exit code and
wall time.

- **`<option>-healthy1.log`.** A to C against a healthy environment.
- **`<option>-faulted.log`.** A to C with two faults injected at the same
  time:
  - **F1.** The caller's API key in Secret `modelplane-system/e2e-callers`
    was changed from `sk-e2e-caller` to `sk-broken`. So every request using
    `sk-e2e-caller` gets 401.
  - **F2.** A pod `cert-manager/bakeoff-rogue` with a wildcard toleration
    (`operator: Exists`, no key) was created on the workload cluster.

  A good suite should:
  - fail A1, A3, A4, A5 and B1, or skip them as consequences;
  - pass A2 and C1;
  - fail C2, naming the rogue pod;
  - clean up after itself anyway.
- **`<option>-healthy2.log`.** A to C again, after the faults were reverted.
  This is a second healthy run, to check for flakes.

Notes on the runs:
- The orchestrator's harness killed the first faulted unittest run at a
  10-minute tool timeout. That wasn't the prototype's fault, so it was rerun,
  and only the rerun's log is kept.
- All four builders tested against the shared environment at the same time,
  and some of their notes mention interference from that. The orchestrator's
  runs were serial.
- `/tmp/bakeoff/findings.md`, if it exists, holds the orchestrator's other
  observations, such as whether the pytest option's Python bring-up worked on a
  fresh environment. Treat it as evidence to weigh, like everything else.

## Rules

- **Don't touch the environment.** Don't run any suite, and don't change
  either cluster. Read code, logs and docs only. You may run read-only
  commands such as `git`, `wc`, `grep`, linters and `nix flake check`.
- **Verify claims.** Where a builder's notes claim something, check it
  against the code or logs. Where you rely on how a tool behaves, check its
  source or docs.

## Output

Write your judgment to `{OUT}` with:

1. A score table: rows are the options, columns are the seven rubric
   criteria, and each cell is 1 to 5.
2. For every score, one or two sentences of evidence, with file:line or log
   excerpts.
3. An overall ranking, with the reasoning in under 150 words.
4. For each option, the single biggest risk if the project adopted it.
5. Where you think the experiment itself is unfair or misleading. Say which
   option it favours, and how.

Your final message should be the score table and ranking, in under 250
words.
