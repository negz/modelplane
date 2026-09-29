# Bake-off notes: shell

## Commands

From the worktree root, against the running environment:

```bash
nix develop -c e2e/test.sh                                   # A to C
E2E_CP_CONTEXT=<context> nix develop -c e2e/test.sh clouds   # D
```

`e2e/test.sh serving` (or `lifecycle`, `placement`) runs one scenario. Each is
a script in `e2e/tests/`, run as its own process, sourcing `e2e/lib.sh` (128
lines of reporting, retry, probe pod and request helpers). Each check prints
`=== ID: what must hold`, then `--- PASS|FAIL|SKIP ID: detail`, with
diagnostics indented under a failure. A summary of every result ends the run,
which exits non-zero if any check failed. `run.sh --verify` now waits for the
model to serve, then runs `test.sh`.

## What CI needs

- Nothing for A to C. `nix run .#e2e -- --verify` runs `test.sh`, and the nix
  app's runtimeInputs already include everything the tests use. I couldn't
  run that path, because bring-up is off limits.
- D needs a separate `workflow_dispatch` job: bring up a full control plane
  (the e2e one is lean and can't provision clouds), create the `aws-creds`
  Secret from repository secrets, apply
  `docs/manifests/getting-started/clusterproviderconfig-aws.yaml`, then run
  the D command. Not implemented.

## Implemented

- A1 to A5, B1 to B3, C1, C2 and D1 as the brief numbers them.
- A6 to A9 are the checks `run.sh` had beyond A1 to A5, moved so `--verify`
  keeps them: unknown model not served, cluster gateway refuses a caller with
  no client certificate, no plaintext listener, `/v1/models` lists the model.
- B reports its teardown as a check. It runs on exit, including on SIGINT or
  SIGTERM.
- The C2 allowlist is an array at the top of `tests/placement.sh`, keyed by
  namespace and owning DaemonSet.
- D1 is written but has never run. It refuses to start without
  `E2E_CP_CONTEXT` set, and fails if there's no AWS `ClusterProviderConfig`.

No tools added: bash, kubectl, the crossplane CLI and a pinned curl image.
There's no jq, so JSON is matched with shell patterns. `e2e/.shellcheckrc`
lets shellcheck follow `lib.sh`.

## Limitations

- A5 can't tell this run's request from the other agents'. They send the same
  body as the same caller, and the access log carries no request ID (fields:
  caller, duration_ms, endpoint, input_tokens, output_tokens, response_model,
  served_model, service, start_time, status, total_tokens). It reads only
  records since the probe pod started, so records from before a fault can't
  pass it.
- JSON matching is textual. A3 takes the body's last `"model"` value, and A5
  expects `"key":value` with no spaces. A reformatted log would break A5.
- Retries: A1 120s, A4 60s, A5 30s, B1 360s to RoutingReady then 120s. A2 and
  A6 to A9 are single requests, so a transient gateway error fails them.
- B leaves `mp-bakeoff-shell-<hash>` on the workload cluster. The
  InferenceCluster composes it with management policies Observe, Create and
  Update (`functions/compose-inference-cluster/function/fn.py:475`), so it's
  orphaned by design, and outside this sandbox to delete.
- Needs bash 4 or later, which `nix develop` provides. macOS's `/bin/bash` 3.2
  won't do. I haven't run it on macOS.
- Namespaces are hardcoded as `bakeoff-shell` and `bakeoff-shell-serving`. A
  real PR would name them `e2e-*`.
- My tool harness killed three runs during B3. Each time the INT/TERM trap
  marked B3 `FAIL: interrupted`, teardown deleted `bakeoff-shell`, and
  serving's trap had already deleted its namespaces.

## Last run

A to C passed in 183s: serving about 30s, lifecycle about 150s (RoutingReady
after 96s, 404 on the first request after deleting the ModelService, engine
pods gone 30s after the foreground delete returned), placement about 2s.
