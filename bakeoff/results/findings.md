# Orchestrator findings

These are observations the run logs don't show on their own. Each is marked
as verified or inferred.

## Runs

Every run was serial against the same shared environment. Times are wall
seconds, from `summary.txt`.

| Option | Healthy 1 | Faulted | Healthy 2 |
|---|---|---|---|
| shell | pass, 153 | fail, 461 | pass, 147 |
| unittest | pass, 147 | fail, 244 | pass, 143 |
| pytest | pass, 140 | fail, 715 | pass, 139 |
| chainsaw | pass, 113 | fail, 150 | pass, 107 |

Chainsaw runs its three tests in parallel. The others run their scenarios
one after another.

## Deleting a namespace that still holds a ModelDeployment wedges

**Verified.** My harness killed the first faulted unittest run during B1, so
`bakeoff-unittest` still held a live ModelDeployment. I deleted the namespace
by hand, and it stuck in `Terminating`.

Fifteen `objects.kubernetes.m.crossplane.io` kept
`finalizer.managedresource.crossplane.io`, and each reported:

```
connect failed: cannot track ProviderConfig usage: cannot apply
ProviderConfigUsage: ... unable to create new content in namespace
bakeoff-unittest because it is being terminated
```

I cleared it by removing the finalizers by hand.

This bears on teardown:
- **shell** (`e2e/tests/lifecycle.sh`) and **unittest**
  (`e2e/tests/test_lifecycle.py` `teardown()`) tear down by deleting the
  namespace. That's safe only when B3 has already deleted the ModelDeployment.
- **pytest** (`e2e/test_lifecycle.py` fixture) and **chainsaw** (per-step
  cleanup) delete the ModelService and ModelDeployment first, then the
  namespace.

**Inferred.** With namespace-only teardown, a run that stops before B3 would
leave its namespace stuck. In the faulted runs B3 still ran for shell and
unittest, so they didn't hit this.

## Other

- **Leftover namespaces.** Every option leaves `mp-bakeoff-<option>-<hash>` on
  the workload cluster. The brief anticipated this. InferenceCluster `local`
  composes those namespaces and never deletes them.
- **pytest `uv.lock` rebuild: corrected after judging.** I first blamed the
  pytest option's `uv.lock` change for rebuilding every function image. That
  was wrong. Judges 1 and 3 found that the function-image derivations differ
  between all five worktrees, including shell, whose diff touches only `e2e/`.
  My own `nix run .#e2e` in the orchestrator worktree built 294 derivations,
  and `--clean` built 258, after changes only to `e2e/` and `bakeoff/`. Any
  source change rebuilds the images, whichever option you pick.
- **pytest bring-up.** See the next section.

## The pytest option's Python bring-up works on a fresh environment

**Verified.** I tore the shared environment down with `nix run .#e2e --
--clean`, then ran this from the pytest worktree:

```
nix develop -c uv run --group e2e pytest --bring-up
```

It created both clusters, installed Modelplane, applied the platform
manifests and passed the suite: 11 passed and D1 skipped, in 930 seconds. The
log is `/tmp/bakeoff/runs/pytest-bringup.log`. For comparison, the original
`nix run .#e2e -- --verify` took 997 seconds, and that included the nix
build.

Afterwards, `python -m e2e.environment down` deleted both clusters and exited
0. That log is `pytest-down.log`.

Two caveats:
- **The nix app wasn't exercised.** I skipped `nix run .#e2e` to avoid the
  function-image rebuild, and instead pointed `_output/functions` at the
  function images already built for the shared environment.
- **Capacity.** For this run only, I set
  `e2e/manifests/platform/30-inference-cluster.yaml` to `nodeCount: 5`, and
  reverted it afterwards. With the committed `nodeCount: 1`, B's second model
  would have no declared capacity. The other options share that limitation,
  because the brief's capacity tweak applied to the environment, not to their
  code.
