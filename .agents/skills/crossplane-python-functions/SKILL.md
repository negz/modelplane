---
name: crossplane-python-functions
description: Write Python composition functions for Crossplane v2 using the up CLI. Use when writing, debugging, or refactoring composition functions, when working with the compose(req, rsp) pattern, when dealing with required resources, readiness tracking, resource gating, deletion ordering, or when the user mentions function-python, compose functions, or Crossplane compositions.
compatibility: Requires the up CLI and Docker with buildx.
---

# Writing Crossplane v2 Python Composition Functions

## The Function Contract

A function is an embedded Python script that `function-python` executes. You
write each one in `functions/{name}/main.py`, and it exports one function:

```python
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    pass  # Mutate rsp in place. Don't return anything.
```

Do NOT use the class-based `FunctionRunner` pattern from standalone SDK docs.
Do NOT call `response.to(req)`. Do NOT return `rsp`.

Each function directory contains `main.py` and a `model` symlink:
```
functions/my-function/
├── main.py
└── model -> ../../.up/python/models
```

See [references/example.py](references/example.py) for a complete example
function demonstrating all key patterns, including Pydantic models, gating,
readiness, events, status, deletion ordering.

Functions need the `model` symlink. Without it, the function crashes at runtime
with `ModuleNotFoundError: No module named 'function.model'`. Use
`up function generate` to create new functions, or create the symlink manually.

## Build Cycle

```bash
# 1. Write XRD (apis/myresource/definition.yaml)
# 2. Write composition (apis/myresource/composition.yaml)
# 3. Build to generate Pydantic models
up project build
# 4. Write the function (functions/compose-myresource/main.py)
# 5. Write test (tests/test-myresource/main.py)
# 6. Build again, then test
up project build
up test run tests/test-myresource
```

Build before writing the function, because it generates Pydantic models under
`.up/python/models/` that the function imports.

### The Build Process (No Docker Buildx)

`up project build` does NOT use Docker or buildx to build function images.
It pulls the `function-interpreter-python` base image from the registry,
tars the function directory (following symlinks like `model`), and appends
it as a layer using `go-containerregistry`. It writes the result to the
`.uppkg` file, not to the Docker daemon.

This means:
- `up project build` always picks up code changes (it reads the filesystem
  directly, without caching)
- `docker images` shows stale images from previous `up test run` or
  `up project run` sessions, not the latest build
- `docker run <image>` inspects the stale Docker cache, not the `.uppkg`
- `up project push` reads from the `.uppkg`, not Docker

Do NOT use `docker run` to verify build output. It shows stale images.
The build is always fresh.

### Deploying Changes to a Cluster

Each build produces new digests for changed functions. The Configuration's
dependency metadata includes these digests. Updating the Configuration tag
is sufficient. Crossplane detects the new digests and updates the Functions
automatically:

```bash
up project build
up project push --tag v0.1.0-dev.N    # bump N every time
kubectl patch configuration <name> --type=merge \
  -p '{"spec":{"package":"xpkg.upbound.io/<org>/<project>:v0.1.0-dev.N"}}'
```

Three commands, and you don't need to delete any Functions. Crossplane handles
the rest via digest comparison in the ConfigurationRevision's dependencies.

## Import Paths

Both functions and tests use the `.m.` (managed/namespaced) variants:
```python
from .model.io.upbound.m.gcp.compute.network import v1beta1 as networkv1beta1
from .model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
```

When in doubt, check what exists under `.up/python/models/`.

XR models have no `.m.` segment:
```python
from .model.example.acme.platform import v1alpha1
```

## Composing Resources

Use Pydantic models for everything with a generated model. They catch field
name mistakes at write time:

```python
resource.update(
    rsp.desired.resources["network"],
    networkv1beta1.Network(
        spec=networkv1beta1.Spec(
            forProvider=networkv1beta1.ForProvider(
                project="my-project",
                autoCreateSubnetworks=False,
            ),
        ),
    ),
)
```

Raw dicts are the fallback for CRDs without generated models (Gateway API,
MetalLB, Usages, etc.):

```python
resource.update(rsp.desired.resources["gateway-class"], {
    "apiVersion": "gateway.networking.k8s.io/v1",
    "kind": "GatewayClass",
    "metadata": {"name": "envoy"},
    "spec": {"controllerName": "gateway.envoyproxy.io/gatewayclass-controller"},
})
```

### Protobuf Number Coercion

All numbers from XRD fields arrive as Python `float`, not `int`. Cast
explicitly:

```python
desired_envs = int(xr.spec.environments)  # protobuf delivers as float
```

## Readiness

Set XR readiness via `rsp.desired.composite.ready`:

```python
rsp.desired.composite.ready = fnv1.READY_TRUE
```

Do NOT set the `Ready` condition via `fnv1.Condition`, because it's reserved.
Do NOT use `TARGET_COMPOSITE_AND_CLAIM`, because Crossplane v2 has no claims.

For composed resources, check the appropriate condition (not always `Ready`):

```python
from crossplane.function import resource

def _has_condition(req, name, cond):
    """Check if an observed composed resource has a condition set to True."""
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    return resource.get_condition(observed.resource, cond).status == "True"

# Crossplane MRs use Ready
if _has_condition(req, "my-cluster", "Ready"):
    rsp.desired.resources["my-cluster"].ready = fnv1.READY_TRUE

# Gateway API uses Accepted
if _has_condition(req, "my-gateway", "Accepted"):
    rsp.desired.resources["my-gateway"].ready = fnv1.READY_TRUE
```

Mark resources without conditions (ProviderConfigs, ConfigMaps) as
always-ready:

```python
rsp.desired.resources["my-providerconfig"].ready = fnv1.READY_TRUE
```

## Results as Kubernetes Events

`response.normal()` and `response.warning()` emit Kubernetes events visible
in `kubectl describe`. Use them deliberately:

- **Normal (every reconcile, time-bounded):** "Waiting for: gke-cluster".
  Expected, self-resolving. Stops when the resource becomes ready.
- **Normal (transition, once):** "GKE cluster ready, composing KServeStack".
  A milestone. Detect transitions by comparing current state to observed state.
  <!-- vale off -->
- **Warning (every reconcile, persistent):** "spec.kserve.cluster.gke is
  required".
  <!-- vale on -->
  The user can fix it, and it won't self-resolve.

Detect transitions:
```python
was_ready = resource.get_condition(
    req.observed.composite.resource, "Ready"
).status == "True"

if not not_ready:
    rsp.desired.composite.ready = fnv1.READY_TRUE
    if not was_ready:
        response.normal(rsp, "Ready, gateway: 34.55.233.135")
else:
    response.normal(rsp, f"Waiting for: {', '.join(not_ready)}")
```

Never emit Normal events on every reconcile in the steady state. They
accumulate as Kubernetes Event objects.

## Gating Resources on Dependencies

Only compose a resource once Crossplane observes its dependencies. But once
composed, always keep emitting it. If a function omits a resource from desired
state, Crossplane deletes it.

```python
pc_observed = "provider-config" in req.observed.resources
release_exists = "cert-manager" in req.observed.resources

# Gate on dependency, but always emit once it exists
if pc_observed or release_exists:
    resource.update(rsp.desired.resources["cert-manager"], ...)
```

For readiness-based gating (e.g., KServe waits for cert-manager):

```python
cert_ready = _has_condition(req, "cert-manager", "Ready")
kserve_exists = "kserve" in req.observed.resources

if cert_ready or kserve_exists:
    resource.update(rsp.desired.resources["kserve"], ...)
```

## Required Resources (Cross-XR Reads)

Use required resources to read resources the function doesn't own:

```python
from crossplane.function import request, response

response.require_resources(
    rsp, name="cluster",
    api_version="platform.example.org/v1alpha1",
    kind="Cluster",
    match_name="my-cluster",
)

cluster = request.get_required_resource(req, "cluster")
if cluster is None:
    return  # Crossplane will re-call with the resource
```

Required resources are dicts, not Pydantic models. They're external resources
that Crossplane resolves.

The empty `match_labels={}` selector doesn't work. Protobuf serialization loses
the empty map, causing the `oneof match` to be unset. Use a real label to match
on, or use `match_name` for specific resources.

## Deletion Ordering with Usages

When you delete a composite, all composed resources get deletion timestamps
simultaneously. Resources with implicit dependencies (ProviderConfigs used by
Helm releases, clusters used by stacks installed on them) need explicit ordering.

Use `protection.crossplane.io/v1beta1` Usages:

```python
resource.update(rsp.desired.resources["usage-pc-by-release"], {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "metadata": {"namespace": "my-namespace"},
    "spec": {
        "of": {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "ProviderConfig",
            "resourceRef": {"name": "my-provider-config"},
        },
        "by": {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "Release",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"my-label": "my-release"},
            },
        },
        "replayDeletion": True,
    },
})
```

Key rules:

- **One Usage per protected-by pair.** The selector resolves to the first
  matching resource and stops. A pair of releases needs a separate Usage for
  each, with distinct labels.
- **Use labels to distinguish resources** of the same kind in the selector.
- **`replayDeletion: True`** means that deleting the "by" resource also
  deletes the Usage, and Crossplane then retries deleting the "of" resource.
- **Usages don't prevent Terminating state.** A namespace with a
  `deletionTimestamp` blocks new object creation even if a Usage prevents its
  final deletion. Use shared long-lived namespaces instead of per-resource
  namespaces.

## managementPolicies for Undeletable Resources

Some resources can't be cleanly deleted (webhook-protected CRDs, resources with
controller-managed finalizers). Use `managementPolicies` to skip deletion:

```python
release.spec.managementPolicies = ["Create", "Observe", "Update"]
```

This is appropriate when the underlying infrastructure is going away anyway
(e.g., KServe on a GKE cluster that you're deprovisioning). Crossplane orphans
the resource, and it goes away with the cluster.

## Cluster-Scoped XRs Composing Namespaced Resources

Cluster-scoped XRs don't auto-populate `metadata.namespace` on composed
namespaced resources. Set it explicitly:

```python
helmv1beta1.Release(
    metadata=metav1.ObjectMeta(namespace="my-namespace"),
    spec=...,
)
```

Without this, the resource has no namespace in the XR's `resourceRefs` and
Crossplane can't GET it.

## Debugging

When something is slow or stuck during deletion, describe the resources:

```bash
kubectl describe <resource> -n <namespace>
```

Every "slow" deletion has a specific stuck resource with a specific error.
Common causes:

- **ProviderConfig not found:** deleted before the resources that use it. Fix
  with a Usage.
- **Namespace is terminating:** blocks ProviderConfigUsage creation. Fix by
  using a shared namespace.
- **Webhook has no endpoints:** something deleted the webhook server pod before
  the webhook-protected resources. Fix with `managementPolicies`.
- **Finalizer blocking deletion:** a remote controller hasn't processed the
  deletion yet (e.g., GatewayClass `gateway-exists-finalizer`). Fix with
  `managementPolicies` or a Usage to ensure Crossplane deletes the dependent
  first.

Never increase timeouts without first describing the stuck resources. The fix
is always in the deletion ordering, not the timeout.
