# A review agent for pull requests

**Status:** Draft
**Date:** October 2026
**Author:** Nic Cope

## Background

Agents write all of Modelplane's code, and they do most of its review too. Each
maintainer points their own agent at a PR, asks it what the PR does and whether
it's correct, then decides what to post. Each of us runs a different harness
with different skills. A PR gets reviewed against whichever conventions its
reviewer's agent happens to know. Nobody reviews a PR until someone remembers
to.

GitHub Copilot reviews PRs automatically, but we can't give it our conventions,
and it often runs out of credits. Most of its accepted findings are code that
contradicts its own docstrings. It misses most of what maintainers raise, such
as narrating comments, needless helpers and Crossplane's garbage collection of
dropped resources. It also re-reviews every push, finding something new each
time.

That last problem applies to any agent reviewer. Asked for problems, an agent
finds them indefinitely, and the author's agent dutifully addresses each one.
Most of the findings maintainers decline are one of three kinds: backward
compatibility for pre-1.0 software, guards for states the schema rules out, and
confident claims about external tools that turn out to be false.

## Goals

- Review every PR from an org member when it's ready, with no one asking.
- Raise what maintainers would raise, phrased as they'd phrase it, and little
  else.
- Keep the conventions in the repo, where any maintainer can tune them.
- Let the reviewer run the PR's code, as our local agents do, without exposing a
  credential to it.

## Proposal

An agent reviews a PR when an org member opens it or marks it ready for review.
To review anyone else's PR, or to re-review after new pushes, a maintainer
comments `/review`. A re-review covers only what `git range-diff` shows changed
since the last review. It also reads the existing threads, so it doesn't repeat
them.

Each review posts as a GitHub App, as one comment review. It never approves or
requests changes. The review opens with a sentence on whether the PR looks
ready, then a collapsed summary of what it does, where it departs from its issue
or design, and any API it changes. At most eight inline comments follow.

The review agent runs three reviewers in parallel, each with fresh context:

- **Prose** checks comments, docstrings, field descriptions and docs against the
  code.
- **Craft** checks whether each addition needs to exist, and whether it matches
  the repo's conventions.
- **Correctness** looks for bugs, and verifies each by tracing code, running
  tests or reading upstream source.

The review agent then checks each finding against the bar the
`reviewing-pull-requests` skill sets. A finding has to be true, introduced by
this PR, triggered by a real input, and something the author would want to
change. The agent merges duplicates and posts what's left. The same skill sets
the phrasing rules, and lists real findings from this repo for calibration.

The agents live in `.opencode/agents/`, and the skills they load live in
`.agents/skills/`. Any maintainer can run the same review locally, from a
checkout of the PR's head commit:

```bash
gh pr checkout 556
opencode run --agent review "Review PR #556 in modelplaneai/modelplane."
```

### Running untrusted code

Every PR comes from a fork, and the workflow runs on `pull_request_target`
because only that trigger gives a fork's PR access to secrets. The agent runs
the PR's code with a full shell and network, but never holds a secret:

- The agent runs in a [Firecracker](https://firecracker-microvm.github.io/)
  microVM on the runner, which GitHub's x86 runners support through KVM. The VM
  gets neither a GitHub token nor the Anthropic key. A proxy on the runner adds
  the key to the agent's model requests, and forwards only requests to create a
  message. The key belongs to a workspace used for nothing else.
- The VM doesn't share files with the runner. Its job arrives as a tar on a
  read-only disk, and it writes the review as a tar to another disk.
- A firewall on the runner allows the VM's connections to the proxy and the
  internet. It drops connections to the runner itself, its private network and
  the cloud metadata service.
- A second job, with no model in it, validates the review and posts it with a
  token scoped to PR reviews. It reads the tar without extracting it.

The worst a malicious PR can do is spend API budget and post a misleading
review. A spend limit on the key bounds the first, and a maintainer reads the
second. That includes a PR steering its own review. The agent's config,
instructions and skills come from `main`, but the agent runs the PR's code in
the same VM, and that code can rewrite them mid-review.

The VM runs a full Linux kernel, so the agent can run anything our local agents
can, including `nix flake check` and a kind cluster.

## Alternatives considered

**opencode's GitHub Action.** It handles triggers and posting, but the agent
posts directly, so it holds a token that can write to the repo. The default
setup also exchanges tokens through opencode's own GitHub App. Running `opencode
run` directly costs a short script and keeps posting outside the agent's reach.

**A read-only agent with no sandbox.** Denying the agent a shell would make the
VM unnecessary, but it would review worse than our local agents. Those
run tests and read upstream source to verify findings, and unverified findings
are the main source of noise.

**An agent that checks non-member PRs are safe before review.** It has to read
the PR to judge it, so a PR can prompt-inject it into approving. With no secrets
in the VM, non-member code doesn't need one, and `/review` limits what we
spend on it.

**Copilot.** It's free and already set up, but there's no way to give it our
conventions or tell it what not to raise.