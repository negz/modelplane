# Local end-to-end test (no cloud, no GPU)

Exercise the full Modelplane path — publish capacity, register a cluster, deploy
a model, route a request through the InferenceGateway — on local `kind`
clusters, with **no cloud provider and no GPU**.

This is the integration layer. The composition functions run in-cluster against
real providers, so it catches failures unit tests can't: missing RBAC,
cross-cluster routing, CLI parsing, provider readiness gating, and teardown
ordering. It needs no cloud or registry credentials, only public images — so it
can gate a merge where the cloud e2e can't.

It uses **two clusters**, mirroring a real deployment:

- a **control-plane** cluster (crossplane + the Configuration), managed by
  `crossplane project run`;
- a **workload** cluster registered via `source: Existing`, where the serving
  stack, both gateways and the model run.

Two clusters rather than one because that's the shape Modelplane is for: a
control plane that installs nothing on itself, and clusters that run everything.
Both gateways live on the workload cluster: the InferenceGateway as the front
door and the cluster gateway fronting the engines. The control plane runs only
Crossplane and the providers.

Two Modelplane primitives make it cloud-free:

- **`source: Existing`** — the `InferenceCluster` registers a bring-your-own
  cluster (the workload kind cluster) via a kubeconfig Secret instead of
  provisioning EKS/GKE/Nebius.
- **`claim: DRA` + a fake GPU driver** — the `InferenceClass` advertises a
  `gpu.example.com` device backed by the **dra-example-driver**
  (kubernetes-sigs), which publishes fake GPUs with **no real hardware**. The
  engine's `ResourceClaim` binds a fake device on a GPU-less node, so the *real*
  DRA allocation path runs. (A claimable device is required: the fleet scheduler
  rejects an engine whose only device is `Synthetic`.)

The engine is a **mock server** (a few lines of Python) that answers both
`/v1/chat/completions` (OpenAI) and `/v1/messages` (Anthropic), the way a vLLM
server exposes both, so the pod goes Ready without a real model or GPU.

## What this does and does not test

| Tested | Not tested |
|---|---|
| Fleet scheduling / placement (CEL vs declared capacity) | Real token generation (mock engine) |
| `ModelDeployment` → `ModelReplica` → `ModelEndpoint` → `ModelService` wiring | Real GPU drivers / CUDA (fake DRA devices only) |
| DRA `ResourceClaim` → fake device binding (the real allocation path) | Multi-node / disaggregated (`PrefillDecode`) serving |
| Serving-stack install on a real (BYO) workload cluster | Cloud provisioning (EKS/GKE/Nebius) |
| `InferenceGateway` + cross-cluster routing to the replica | |
| Status propagation and foreground-deletion ordering | |

### Why cloud provisioning cannot be tested here

`lean-control-plane.yaml` trims the control plane to what the BYO
(`source: Existing`) scenario needs. That's all the e2e runs, and it carries no
cloud credentials, so it never creates a cloud `InferenceCluster` and never
provisions a cloud cluster. This is what that looks like, and what to expect if
you point this control plane at a real cloud without adding one:

- The lean MRAP activates only `*.kubernetes.m.crossplane.io` and
  `*.helm.m.crossplane.io`, and nothing composes a cloud activation policy, so a
  cloud provider's managed resources are never activated. They sit with **no
  status conditions at all** — which reads as nothing happening rather than as
  an error.
- Because those providers declare the safe-start capability, Crossplane scales
  their controllers to zero while their managed resources are inactive, so a
  dormant cloud provider runs no pod at all.

A cloud `InferenceCluster` would compose its own additive policy activating its
cloud's kinds and wake that provider; the e2e just never creates one. Undoing
these trims is possible but leaves a control plane that is no longer the one CI
runs, so prefer a separate control plane for cloud work.

## Prerequisites

- **Docker** with real headroom — **≥ 16 GB memory** and **plenty of disk**
  (raise Docker Desktop's disk-image size). Two kind clusters, 13 provider
  packages, the built function images, and the serving stack
  (kube-prometheus-stack et al.) add up fast; a full Docker disk surfaces as
  `no space left on device`. Reclaim between runs with `docker builder prune -af`
  and `docker image prune -af`.
- Everything else — `kind`, `kubectl`, `docker`, the `crossplane` CLI, and
  Python with pytest — is provided by the flake via `nix run .#e2e`.

The workload cluster is pinned to **k8s v1.34** (in `environment.py`) for the
`resource.k8s.io` (DRA) APIs, on-by-default in 1.34: both the serving stack's
NVIDIA DRA driver and the dra-example-driver register DeviceClasses, and the
example driver publishes the `ResourceSlice`s the engine's `ResourceClaim` binds
against. The control-plane cluster needs no DRA.

## Run

```bash
nix run .#e2e                # bring up both clusters, then run every test
nix run .#e2e -- -m serving  # arguments go to pytest
nix run .#e2e -- --no-apply  # bring up the clusters only, without the platform
nix run .#e2e -- --clean     # tear both clusters down
```

`nix run .#e2e` is the command the `E2E` CI workflow runs, so a green run
locally and a green CI run mean the same thing. It runs `pytest --bring-up`,
which brings each cluster up before the first test that needs it. A workload
cluster that's already running the pinned version is reused.

Without `--bring-up`, the tests attach to the clusters that are already up.
That's the quick loop while writing a test:

```bash
nix develop -c uv run --group e2e pytest                  # everything but the cloud tests
nix develop -c uv run --group e2e pytest -k b1            # one check
nix develop -c uv run --group e2e pytest --workload-context=kind-other
```

The tests that provision real cloud infrastructure are skipped unless asked
for. They need a control plane and the cloud's credentials, but no workload
cluster, so bringing up an environment for them alone creates only the control
plane:

```bash
nix run .#e2e -- --cloud=eks --aws-credentials=$HOME/.aws/credentials -m cloud
```

To poke at the gateway by hand, curl from a pod: its address is on the kind
Docker subnet, which the host can't route to on macOS. Send the key from
`manifests/platform/10-inference-gateway.yaml`:

```bash
oai=$(kubectl get ig local -o jsonpath='{.status.endpoints.openAI}')
kubectl run curl -n ml-team --rm -it --image=curlimages/curl@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13 -- \
  curl -s "$oai/chat/completions" -H 'authorization: Bearer sk-e2e-caller' \
  -H 'content-type: application/json' \
  -d '{"model":"ml-team/mock","messages":[{"role":"user","content":"hi"}]}'
```

## How it's structured

The tests follow the shape of [Crossplane's e2e tests]. Each test module is a
feature, with a setup, assessments and a teardown:

- **Setup and teardown** are pytest fixtures. A fixture applies a directory of
  YAML manifests under `manifests/`, yields, and deletes them again whether or
  not the tests passed.
- **Assessments** are test functions, named for the check they make: `test_b1_…`
  is check B1. They run in file order.
- **Labels** are pytest markers: `serving`, `lifecycle`, `placement` and
  `cloud`. Select with `-m`.

```
e2e/
  conftest.py          # options, and the fixtures every test shares
  environment.py       # brings the two clusters up and down
  kube.py              # kubectl: get, list, apply, delete, wait for a condition
  gateway.py           # requests to the gateway from a pod, and its usage log
  wait.py              # retry an assertion until it passes or times out
  test_serving.py      # A: auth, routing, translation and metering
  test_lifecycle.py    # B: create, serve, and delete a model
  test_placement.py    # C: where pods land on the workload cluster
  test_clouds.py       # D: provision a real EKS cluster (skipped by default)
  lean-control-plane.yaml
  dra-example-driver.yaml    # vendored fake DRA GPU driver (applied to workload)
  manifests/
    platform/          # applied to the control plane by bring-up
    client/            # the pod the tests send requests from
    lifecycle/         # B's model
    clouds/eks/        # D's InferenceCluster, and its setup
```

Bring-up is the cross-cluster orchestration that `crossplane project run` flags
can't express:

1. `crossplane project run` for the **control plane**, with
   `lean-control-plane.yaml` as `--init-resources` so the provider trims land
   before the providers install. Then finish the setup the getting-started flow
   does by hand: `kubectl apply` the RBAC prerequisites, and point provider-helm
   and provider-kubernetes at their DeploymentRuntimeConfigs.
2. Create the **workload** kind cluster (pinned v1.34). Install MetalLB on it
   (the serving stack doesn't) with a pool inside the detected kind subnet,
   install the **dra-example-driver** (fake GPUs), and label its node for the
   `gpu-synthetic` pool.
3. Add the workload kubeconfig Secret to the control plane (`kind get kubeconfig
   --internal`, reachable from control-plane pods over the shared kind network),
   then apply `manifests/platform/`.

With `--bring-up`, the first test that needs the workload cluster waits for the
platform to serve ml-team's mock model, which takes as long as the serving
stack's install. Without it, each test waits only for what it needs, for as long
as that takes on a settled environment. So attach to one that has settled.

### Adding a test

Add a check to the module for its scenario, or a new module for a new one. Put
the resources it creates in a directory under `manifests/`, in a namespace of its
own, and apply them in a module-scoped fixture that deletes them afterwards.
Assert through `wait.until` wherever the system converges rather than answering
at once, and write each assertion's message so a failure says what was wrong.
Mark the module `diagnose(namespace)` to have a failure report the conditions of
every Modelplane resource in that namespace.

[Crossplane's e2e tests]: https://github.com/crossplane/crossplane/tree/main/test/e2e

## Why the extra moving parts

- **MetalLB on the workload cluster.** Both gateways run there, and both need
  `LoadBalancer` addresses kind can't provide: the serving stack gates the
  cluster gateway's readiness on having one (`READY_CEL` in its `gateway.py`).
  Nothing Modelplane composes installs MetalLB, so bring-up does, with a pool
  inside the detected kind Docker subnet (see caveat) so the control plane can
  route to the addresses it hands out.
- **Fake DRA driver.** A `claim: DRA` engine emits a `ResourceClaim`; with no DRA
  driver it stays Pending and the pod never schedules. Bring-up applies the
  vendored **dra-example-driver**, which publishes fake `gpu.example.com` devices
  so the claim binds on a GPU-less node.
- **Cross-cluster kubeconfig.** `source: Existing` needs a kubeconfig the
  control-plane provider pods can use to reach the workload API server.
  `kind get kubeconfig --internal` gives an address routable across the shared
  kind network; a host kubeconfig (`127.0.0.1:<port>`) wouldn't be.
- **Node label.** On a BYO cluster Modelplane doesn't provision/label pools, so
  bring-up labels the workload node `modelplane.ai/pool=gpu-synthetic` (matching
  `nodePools[].name`); without it worker pods stay Pending.

## Caveats / open questions

- **Cross-cluster networking uses the detected kind subnet.** Bring-up reads the
  `kind` Docker network's subnet (usually 172.18.0.0/16, but kind bumps to
  172.19/... when earlier networks already hold 172.18) and derives the
  workload cluster's MetalLB pool from it. A hardcoded 172.18 would leave the LB
  IPs off-subnet and the cross-cluster curl would time out.
- **Reconcile runs after the command returns.** `crossplane project run` waits
  for the config to install, then applies the resources and exits — it doesn't
  block on XR readiness. The serving-stack install (the long pole) and the model
  rollout happen after, so watch the `ModelService`'s `RoutingReady` rather than
  the command's exit. `--timeout` in `environment.py` bounds the build and config
  install.
- **Two DRA drivers on a GPU-less node.** The serving stack's **NVIDIA** DRA
  driver targets NFD-GPU-labelled nodes, so it sits at 0/0 (inert) yet its Helm
  release still reports Ready. The **dra-example-driver** bring-up installs is the
  active one — it publishes the fake `gpu.example.com` devices the engine binds.
- **Serving-stack weight.** cert-manager, Envoy Gateway, Envoy AI Gateway, GAIE
  CRDs, kube-prometheus-stack, LeaderWorkerSet, NFD, DRA driver — all on the
  workload node. Give Docker headroom.
- **Package version.** Installs the **current branch** build (via `crossplane
  project run`), not the published `v0.1.0`, because the `source: Existing` /
  DRA-on-BYO path may postdate that tag.
