# Bake-off notes: pytest

## Commands

A to C against the running environment, from the repo root:

    nix develop -c uv run --group e2e pytest

D (never run): add `--cloud=eks --aws-credentials=<file> -m cloud`.

`nix run .#e2e` brings up a fresh environment and runs everything
(`pytest --bring-up`). `nix run .#e2e -- --clean` tears it down.

## What CI needs

- `e2e.yml` drops `-- --verify`, which pytest would reject. Done.
- For D: AWS credentials as a secret, written to a file for
  `--aws-credentials`, and a maintainer-only trigger such as
  `workflow_dispatch`. Not done.

## What's implemented

- A1 to A5, B1 to B3, C1, C2 and D1, each a test named for its check
  (`test_a1_…`). A2 is parametrized as `[no key]` and `[wrong key]`.
- Bring-up in `environment.py`: all of `run.sh` before `--verify`, including
  `--no-apply` and `--clean`. It shells out to kind, docker, crossplane and
  kubectl. `run.sh` is deleted.
- Helpers: `kube.py`, `gateway.py` and `wait.py`, 270 lines with license
  headers. `conftest.py` is 160 and `environment.py` 300.
- Failed tests get a report section listing the conditions of every Modelplane
  resource in their namespace.
- A `ty-e2e` flake check. The ruff and license checks now cover `e2e/`.
- Dependencies: pytest, via a new `e2e` group in `pyproject.toml` and `uv.lock`,
  and a uv2nix venv (`nix/python.nix`) for the app. There's no Kubernetes
  client library. Modelplane resources are read through `schemas/python`.

## What isn't

- The `run.sh` verify checks outside A to D went with it: unknown model not
  routed, cluster gateway mTLS and port 80, `/v1/models`, and the usage
  record's `endpoint`.
- No unit tests for the helpers or bring-up.

## Limitations

- Bring-up has never run. `nix eval .#apps.aarch64-linux.e2e.program`
  succeeds. I didn't build the app, because the `uv.lock` change rebuilds 258
  function-image derivations. Its venv builds in `ty-e2e`, and C1 and C2 pass
  under it (Python 3.12). `uv run` uses 3.13.
- Bring-up now does the control plane before the workload cluster, so D can
  skip the latter. `run.sh` did the reverse. Untested.
- D1 has never run. I inferred that the lean control plane can provision EKS
  from `e2e/README.md` (a cloud InferenceCluster composes its own activation
  policy), and didn't check it.
- A5 can't tell its own record from another agent's: all authenticate as `e2e`,
  and records carry no request ID.
- Teardown leaves `mp-bakeoff-pytest-<hash>` on the workload cluster.
  InferenceCluster `local` composes it through an Object with
  `managementPolicies: [Observe, Create, Update]` (see
  `kubectl get objects.kubernetes.m.crossplane.io -n modelplane-system`).
- B's ModelDeployment fixture is a 125-line copy of the platform one.
- B2 passed within a second of the delete on both runs. The gateway answered
  `404: No matching route found`. B2 needs an HTTP answer, so a gateway that's
  down fails it.
- Request checks retry for 2 minutes, because other agents' routes roll the
  proxy pods. A real failure takes that long to report.
- Attached, each test waits only as long as a settled environment needs. Only
  `--bring-up` waits for the serving stack to install (20 minutes at most).

## Last run

A to C passed: 11 passed and D1 skipped, in 2m08s, leaving only
`mp-bakeoff-pytest-<hash>` behind. I also broke A3's and C2's expectations on
purpose, and both failed with the offending value.
