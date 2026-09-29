# Bake-off notes: chainsaw

## Commands

A to C against the running environment (one scenario: add
`--selector scenario=serving`, `lifecycle` or `placement`):

    nix develop -c e2e/chainsaw/test.sh

D, against a control plane that can provision EKS:

    nix develop -c chainsaw test --config e2e/chainsaw/.chainsaw.yaml \
      --kube-context <ctx> e2e/chainsaw/clouds/eks

## What's here

- `e2e/chainsaw/local/{serving,lifecycle,placement}/chainsaw-test.yaml`: one
  step per check, named `a1-...` and so on. `curl.yaml` is the pod requests
  go from. `.chainsaw.yaml` sets default timeouts.
- `e2e/chainsaw/test.sh` (25 lines): writes a kubeconfig per kind cluster
  with `kind get kubeconfig` and runs `local/`. It never runs `clouds/`, so D1
  runs only when a maintainer names its directory. D1's header gives its
  environment: a full install with AWS credentials, and no workload cluster.
- `flake.nix`: `kyverno-chainsaw` (nixpkgs, v0.2.15) in the dev shell.

## CI would need

`kyverno-chainsaw` in the `e2e` app's runtimeInputs, and run.sh's `--verify`
calling test.sh after it applies the manifests. The serving test waits for
RoutingReady and the proxy rollout itself. Add `--report-format JUNIT-TEST`
for a JUnit report.

## Implemented, and not

A1 to A5, B1 to B3 and C1 to C2 passed on the last run (3 tests, B in 102s).
Checks run with `continueOnError`: four injected expectation faults in A gave
four errors, each as expected against actual. D1 passes `chainsaw lint test`
but has never run. run.sh is unchanged: its other `--verify` checks (unknown
model, cluster gateway mTLS and port 80, `/v1/models`) aren't ported.

## Limitations

Source paths are in kyverno/chainsaw at v0.2.15.

- Bindings and outputs don't cross steps (`runStep`, `pkg/runner/runner.go`),
  so each serving step re-reads the model name and endpoint.
- Scripts never retry, and an assert resolves its bindings once before it
  polls (`pkg/runner/operations/assert.go`). "Eventually" checks are shell
  loops (A5, B2) or curl `--retry` (A1, B1).
- A resource assert always gets the test namespace for a namespaced kind
  (`pkg/engine/namespacer`), so cross-namespace checks (C1, C2, B3's pods) are
  JMESPath over `x_k8s_list`. They fail naming offending pods, with no diff.
- A JMESPath evaluation error ends an assert at once instead of retrying
  (`pkg/engine/operations/assert/operation.go`).
- A test without `namespace:` gets `chainsaw-<petname>` (runner.go:160), so
  each test names one. Placement's is created on the workload cluster, unused.
- `--include-test-regex` is Go's `-test.run` against `chainsaw/<test>`, one
  `/` level at a time (`pkg/runner/flags/flags.go`): `placement` matches
  nothing. Label selectors work. `skip: true` has no CLI override.
- A5 can't tell our usage record from another `e2e` caller's sent in the same
  second. It checks the latest one no older than our request, by the curl
  pod's clock.
- Cleanup logs `=== ERROR ... not found` for resources B already deleted.
- `mp-bakeoff-chainsaw-<hash>` outlives the test, as the brief describes.
- B1 took 3m33s to go RoutingReady on one run, so its timeout is 10m.
