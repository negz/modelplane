# Bake-off notes: unittest

## Commands

A to C, against the running environment, from the repo root:

```bash
nix develop -c python3 -m unittest discover -c -s e2e/tests -v
```

D1, against a control plane with AWS credentials:

```bash
MODELPLANE_E2E_EKS_CONTEXT=<kube context> \
  nix develop -c python3 -m unittest discover -c -s e2e/tests -p test_clouds.py -v
```

The first command lists D1 as skipped, with the reason.

## What's here

- `e2e/tests/kube.py`: contexts, a `kubectl` wrapper, `poll`, condition and
  engine pod lookups (about 100 lines with the license header).
- `e2e/tests/gateway.py`: request bodies from `run.sh`, and a curl pod driven
  by `kubectl exec` (about 100 lines).
- `test_serving.py` (A1 to A5), `test_lifecycle.py` (B1 to B3),
  `test_placement.py` (C1, C2), `test_clouds.py` (D1).
- `nix flake check` now runs ruff, ty and the license check on `e2e/tests`.

The suite runs on the host and imports only the stdlib. It needs `python3`
and `kubectl`, which the dev shell already has. Nothing was added to the flake
beyond the checks. Kubernetes calls go from the host; gateway requests run
curl in a pod on the control plane (the image `run.sh` pins), so it should work
on macOS. I only ran it on Linux.

Last run on this environment: A1 to A5, B1 to B3, C1 and C2 passed; D1 skipped.

## What CI would need

Not implemented. Either add a step after `nix run .#e2e -- --verify` that runs
the first command, or have `run.sh --verify` run it after its waits and add
`pkgs.python3` to the `e2e` app's `runtimeInputs`. The shell checks in `run.sh`
that A1 to A5 duplicate would then go, and the ones A to C don't cover (unknown
model, cluster gateway mTLS and plaintext, `/v1/models`) would move into
`test_serving.py`. D1 would need its own `workflow_dispatch` workflow with AWS
secrets and a way to bring up a non-lean control plane, which doesn't exist.

## Limitations

- A5 can't tell its own request's record from anyone else's. A record carries
  no request ID, and every agent calls `ml-team/mock` as caller `e2e` with the
  same token counts. A record looks like this:
  `{"caller":"e2e","duration_ms":9,"endpoint":"mp-ml-team-51733/...","input_tokens":12,"output_tokens":9,"response_model":"ml-team/mock-demo","served_model":"ml-team/mock-demo","service":"ml-team/mock","start_time":"...","status":200,"total_tokens":21}`.
  A5 only shows that a matching record was logged since its request
  (`kubectl logs --since`).
- B leaves `mp-bakeoff-unittest-<hash>` on the workload cluster. InferenceCluster
  `local` composes it through an Object with `managementPolicies:
  ["Observe","Create","Update"]`, so it's outside my sandbox to delete.
- B1 to B3 depend on each other and rely on unittest running methods in name
  order (documented in the `unittest` docs under "Organizing test code"). B2
  and B3 skip rather than fail if the check before them left nothing to act
  on, so running one alone with `-k` skips it.
- A has no retries: each check sends one request. A transient gateway error
  fails that check.
- If a run dies before its cleanups (say, killed outright), it leaves its
  namespaces behind. B's setup deletes what an earlier run left, and A's setup
  reuses it.
- D1 has never run. Its timeouts (60 minutes to Ready, 30 to delete) are
  guesses, and it assumes the control plane doesn't already have
  `eks-us-east` or `l4-1x-g6`.
