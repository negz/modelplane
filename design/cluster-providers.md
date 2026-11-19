# Cluster providers

**Status:** Draft
**Date:** November 2026
**Author:** Nic Cope

## Summary

Modelplane provisions inference clusters on seven clouds, all of them part of
Modelplane itself. Each cloud has fields in the `InferenceCluster` and
`InferenceClass` APIs and logic in Modelplane's composition functions. Its part
of the serving stack and its Crossplane providers are in Modelplane's package
too. I propose moving each cloud into a cluster provider, a Configuration
package of its own that implements a contract Modelplane defines, the way a
Crossplane provider implements managed resources for Crossplane.

Modelplane's API references a provider's objects rather than embedding them.
An `InferenceCluster` references a cluster config and an `InferenceClass` a
node class, both kinds the provider defines:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: gke-reserved
spec:
  cluster:
    source: Provisioned
    provisioned:
      configRef:
        apiVersion: gke.modelplane.ai/v1alpha1
        kind: GKEClusterConfig
        name: us-central1
  nodePools:
  - name: l4-pool
    className: gke-l4-1x-g2
    nodeCount: 2
    maxNodeCount: 2
    zones: [us-central1-a]
    reservationRef:
      apiVersion: gke.modelplane.ai/v1alpha1
      kind: GKECapacityReservation
      name: l4-reservation
```

Modelplane turns each such cluster into a `ProvisionedCluster`, an internal XR.
Modelplane defines its XRD, and every provider supplies a Composition for it.
Modelplane selects the provider's Composition by the config's kind and reads
from it what the provider supports. It never asks a provider for
something the provider can't do.

The providers live in this repository under `providers/`, and build and release
as separate packages at Modelplane's version. A provider doesn't depend on
Modelplane's source, so moving one into a repository of its own is a packaging
change.

## Background

An `InferenceCluster` either registers a cluster the user runs (`source:
Existing`) or provisions one. Provisioning knows every cloud by name. The
`InferenceCluster` XRD has a block per cloud under `spec.cluster`, and the
`InferenceClass` XRD has one under `spec.provisioning`. About thirty CEL rules
exist only to tie the source to its block or a block's accelerator to the
class's devices. `compose-inference-cluster` dispatches on the source, and
about 1,400 of its 2,400 lines translate an `InferenceCluster` into each
cloud's internal XR. The serving stack keeps a component list per
cloud. Modelplane's package depends on thirteen cloud Crossplane providers, so a
control plane that only registers existing clusters installs every cloud's CRDs
and controllers anyway.

Adding a cloud touches all of that, and a cross-cutting change touches every
cloud. A cloud's code has nowhere to go but Modelplane, so nobody outside
Modelplane can maintain it or release it on their own cadence, let alone add a
cloud Modelplane doesn't want to carry.

Other Kubernetes projects split a core API from provider configuration the same
way. Karpenter's `NodePool` references a cloud's `EC2NodeClass` through
`nodeClassRef`. Gateway API's `GatewayClass` references controller-specific
parameters through `parametersRef`. Cluster API's `Cluster` references an
infrastructure provider's `AWSCluster` through `infrastructureRef`, and Cluster
API versions the contract providers implement. Each keeps the core API typed and
provider-neutral, and puts the provider's configuration in a typed kind the
provider defines rather than in an opaque blob inside the core object.

## Goals

- Modelplane's APIs, functions, package and activation policy don't mention
  any cloud.
- A cloud's API, logic and device stack live in its provider, built and tested
  against Modelplane's published package alone.
- Every field a user writes or the contract defines has a type, and the API
  server validates it.
- A provider that lags Modelplane fails with a clear condition, never by
  silently ignoring a field.
- A control plane that only registers existing clusters doesn't install any
  cloud provider.

## Proposal

### The API

`spec.cluster.source` is `Existing` or `Provisioned`. Existing clusters work as
they do today. A provisioned cluster references its cluster config, and the
class of each node pool references a node class:

```yaml
apiVersion: gke.modelplane.ai/v1alpha1
kind: GKEClusterConfig
metadata:
  name: us-central1
spec:
  region: us-central1
  kubernetesVersion: "1.35"
  credentials:
    type: ClusterProviderConfig
    name: default
---
apiVersion: gke.modelplane.ai/v1alpha1
kind: GKENodeClass
metadata:
  name: g2-standard-8-l4
spec:
  machineType: g2-standard-8
  diskSizeGb: 100
  accelerator:
    type: nvidia-l4
    count: 1
---
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  name: gke-l4-1x-g2
spec:
  description: GKE g2-standard-8, 1x NVIDIA L4
  nodeClassRef:
    apiVersion: gke.modelplane.ai/v1alpha1
    kind: GKENodeClass
    name: g2-standard-8-l4
  devices:
  - name: gpu
    claim: DRA
    driver: gpu.nvidia.com
    deviceClassName: gpu.nvidia.com
    count: 1
    attributes:
      architecture: { string: Ada Lovelace }
    capacity:
      memory: { value: "23034Mi" }
---
apiVersion: gke.modelplane.ai/v1alpha1
kind: GKECapacityReservation
metadata:
  name: l4-reservation
spec:
  zone: us-central1-a
  name: l4-reservation
```

A provisioned cluster needs two more objects than today, a cluster config and a
node class. Both are reusable. The cluster config contains what a
cluster needs beyond its node pools, such as its region, Kubernetes version and
credentials, and every cluster in a region can share one. A node class
describes a machine, and every class that runs on that machine shares it, as
does every pool using those classes.

Where a cloud setting goes depends on what it describes. Hardware facts go on
the node class, beside the instance type that decides them: an EKS node class
says `fabric: EFA`, an AKS or Nebius node class `fabric: InfiniBand`. Location
facts go on the cluster config. The InfiniBand fabric a Nebius cluster's pools
join is `infinibandFabric` on its `NebiusClusterConfig`. A pool references its
capacity reservation directly, because a reservation is neither hardware nor
location, and because it has a lifecycle of its own. `nodePools[]` keeps only
fields that mean something on every cloud, plus that reference.

The kind and group of `configRef` are immutable. Once Crossplane selects a
Composition for an XR it never selects another, so moving a cluster to another
provider means creating a new `InferenceCluster`.

### The contract

`ProvisionedCluster` is a cluster-scoped XR in `infrastructure.modelplane.ai`,
an API group Modelplane's package defines. `compose-inference-cluster` composes
one for the cluster above:

```yaml
apiVersion: infrastructure.modelplane.ai/v1alpha1
kind: ProvisionedCluster
metadata:
  name: gke-reserved
spec:
  crossplane:
    compositionSelector:
      matchLabels:
        infrastructure.modelplane.ai/config-group: gke.modelplane.ai
        infrastructure.modelplane.ai/config-kind: GKEClusterConfig
  configRef:
    apiVersion: gke.modelplane.ai/v1alpha1
    kind: GKEClusterConfig
    name: us-central1
  draDrivers: [gpu.nvidia.com]
  nodePools:
  - name: l4-pool
    role: Accelerator
    nodeClassRef:
      apiVersion: gke.modelplane.ai/v1alpha1
      kind: GKENodeClass
      name: g2-standard-8-l4
    nodeCount: 2
    maxNodeCount: 2
    zones: [us-central1-a]
    reservationRef:
      apiVersion: gke.modelplane.ai/v1alpha1
      kind: GKECapacityReservation
      name: l4-reservation
status:
  secrets:
  - type: Kubeconfig
    name: gke-reserved-kubeconfig
    namespace: modelplane-system
    key: kubeconfig
  - type: GoogleApplicationCredentials
    name: gke-reserved-sa-key
    namespace: modelplane-system
    key: private_key
  cache:
    storageClassName: modelplane-rwx-fs
```

The spec references the cluster config, and lists the pools and the DRA drivers
their classes claim. A pool's `role` is `Accelerator` if its class claims a DRA
driver other than `dra.cpu`, and `CPU` otherwise. Because Modelplane sets the
role, every provider treats the same class the same way. The drivers are the
cluster's rather than each pool's: a provider installs a driver's device stack
on the whole cluster, and shapes a pool by its role and its node class, so a
per-pool driver list would give it nothing to act on.

The status reports what Modelplane needs back. `secrets` are the kubeconfig
and, optionally, an identity that provider-kubernetes and provider-helm use
alongside it to authenticate. `cache.storageClassName` is the ReadWriteMany
class `ModelCache` volumes use, where the provider offers one.
`gateway.service.annotations` are what the cloud's load balancer controller
needs on the gateways' Service, such as CoreWeave's request for a public
address. `telemetry.ignoredNamespaces` are namespaces the serving stack's
collector doesn't scrape, such as the one holding CoreWeave's own GPU exporter.
The CoreWeave device stack runs a second exporter that attributes
DRA-allocated GPUs to pods.

Some obligations no schema can express. A provider:

- Adds a system pool for Modelplane's own components, untainted.
- Labels every pool's nodes `modelplane.ai/pool=<name>`.
- Labels and taints a CPU pool's nodes `modelplane.ai/cpu=true:NoSchedule`, and
  taints an accelerator pool only with a key engines tolerate: `nvidia.com/gpu`
  or `amd.com/gpu`, effect `NoSchedule`, any value.
- Installs the device stack for every driver in `draDrivers` it declares,
  except the drivers the serving stack installs on every cluster, today
  `dra.cpu`.
- Runs any GPU exporter it installs so the collector can find it. The
  exporter's pod has a name label for DCGM or the metrics exporter, and its
  metrics port is `metrics` or `exporter-port`. The exporter attaches the
  GPU-holding pod's `modelplane.ai/deployment`, `replica`, `role` and `engine`
  labels. A provider whose platform runs its own exporter, as Vultr's does,
  needn't install one, but then its GPU metrics may not be attributed to the
  pods using the GPUs.
- Doesn't install cert-manager. The serving stack installs it on every cluster,
  as release `mp-cert-manager` in namespace `cert-manager`, and a device stack
  that needs it waits for its webhook.
- Publishes `secrets` once the cluster is reachable, not once its device stack
  is ready. The device stack waits for cert-manager, which the serving stack
  can't install without the secrets.
- Publishes `gateway.service.annotations` no later than `secrets`. Modelplane
  composes the serving stack as soon as the kubeconfig appears, and a cloud's
  load balancer may keep the address it assigned the gateways' Service first.
- Fails its reconcile with a fatal result when it can't provision what the
  `ProvisionedCluster` specifies, such as a node class whose accelerator doesn't
  match its pool's role. Crossplane reports that on the XR's `Synced`
  condition, which Modelplane relays to the `InferenceCluster`.

### Capabilities and versioning

The contract's version is the XRD's version, `v1alpha1`, and a provider
references it in its Composition's `compositeTypeRef`. Within a version the
contract grows by adding optional fields. A field a provider might not act on
belongs to a named capability, which the provider declares on its Composition:

```yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: gke.provisionedclusters.infrastructure.modelplane.ai
  labels:
    infrastructure.modelplane.ai/config-group: gke.modelplane.ai
    infrastructure.modelplane.ai/config-kind: GKEClusterConfig
  annotations:
    infrastructure.modelplane.ai/capabilities: Zones,CapacityReservations
    infrastructure.modelplane.ai/dra-drivers: gpu.nvidia.com
spec:
  compositeTypeRef:
    apiVersion: infrastructure.modelplane.ai/v1alpha1
    kind: ProvisionedCluster
  mode: Pipeline
  pipeline:
  - functionRef:
      name: modelplane-modelplane-gkecompose-provisioned-cluster
    step: compose-provisioned-cluster
```

`compose-inference-cluster` reads that Composition before composing anything,
and fails the `InferenceCluster` with a condition rather than composing a field
the provider would drop:

| Reason | When |
| --- | --- |
| `NoProvider` | No installed provider's Composition for this contract version names the config's kind. |
| `ConflictingProviders` | More than one does. |
| `UnsupportedByProvider` | A pool uses a field whose capability the provider doesn't declare: `zones` needs `Zones`, `reservationRef` needs `CapacityReservations`. |
| `UnsupportedDevices` | A class claims a DRA driver neither the provider nor the serving stack installs. |
| `InvalidNodePool` | A pool's class names no node class. |
| `ProviderError` | The provider failed its reconcile; the message is the provider's. |

Capabilities let a newer Modelplane work with an older provider, because each
field Modelplane adds comes with a capability the older provider doesn't
declare. Any status field Modelplane starts reading is optional and has a
fallback. Where a change can't be additive it's a new contract version, and a
provider adopts it by publishing a Composition for it.

A provider declares which Modelplane releases it works with as a package
dependency, `>=` the first release with the contract version and capabilities it
uses. Crossplane resolves that when it installs the provider.

### The serving stack

The serving stack splits along the same line. Modelplane's half installs what
every cluster needs whoever provisioned it: cert-manager, the gateways and their
PKI, the cluster agent, dra-driver-cpu, and the Standard or Dynamo stack. On an
existing cluster it also installs the NVIDIA and AMD device stacks, because no
provider does. A provider's half is its device stack: NVIDIA's DRA driver, and
whatever the cloud needs around it, such as the GPU Operator on EKS, AKS, GKE
and Civo, or a second DCGM exporter on CoreWeave.

A provider installs its device stack through a namespaced XR of its own, such as
`GKEDeviceStack`, which its `ProvisionedCluster` composes once the cluster is
reachable. Its components are a build-time list like the serving stack's,
rendered the same way. The device stack composes them without the `Delete`
management policy, because they only configure a cluster the provider deletes
itself. It depends on cert-manager through an observed component that watches
cert-manager's webhook on the cluster, and the components that need
cert-manager wait for it.

The provider SDK, a Python library in `sdk/python` that Modelplane's serving
stack uses too, contains what providers would otherwise each duplicate:

- **Component types and rendering.** `Chart`, `Manifests` and `Observed`
  entries, the checks a list must pass, the DRA driver filter, and the renderer
  that turns a list into Releases, Objects and teardown Usages.
- **Vendor stacks.** NVIDIA's and AMD's charts, pinned once. A hand-written
  device stack takes them, adapting one where its cloud differs. An
  AICR-generated one is checked against them, so one Modelplane release runs
  one version of each NVIDIA component whichever provider provisioned the
  cluster.
- **The AICR generator.** The SDK contains the engine and the tables every
  cloud shares. A provider runs the generator into its own tree, with its own
  coordinate, overlays and cloud-specific values.
- **The DCGM metrics CSV**, which the generator checks every provider's
  resolved CSV against.
- **Contract helpers** that resolve a `ProvisionedCluster`'s references, guard
  them against deletion, activate the provider's managed resource kinds, and
  compose its device stack.

### Teardown

Deleting an `InferenceCluster` deletes its `ServingStack` before its
`ProvisionedCluster`. A Usage in `modelplane-system` blocks deletion of the
`ProvisionedCluster`, by name, until Crossplane deletes the `ServingStack`. That
way the serving stack uninstalls through a cluster that still exists. A
ClusterUsage can't name the namespaced `ServingStack`, so it's a namespaced
Usage of a cluster-scoped object, which must name the object's namespace as
`""`. Without it, Crossplane's deletion webhook looks for Usages of a
`ProvisionedCluster` in the Usage's own namespace, finds none, and allows the
delete. The
device stack goes with the cluster, as the in-cluster parts of every
provisioned cluster do.

A provider guards its own inputs. Its `ProvisionedCluster` composes a
ClusterUsage for its cluster config, each node class and each reservation.
While a cluster uses one of them, Crossplane blocks its deletion.

### Activation

A provider's package includes an activation policy for the provider-helm and
provider-kubernetes kinds its device stack composes. Its `ProvisionedCluster`
composes a second policy, for the cloud's managed resource kinds, so that a
cloud's Crossplane providers run only once a control plane has a cluster on
that cloud. Modelplane's own policy activates only provider-helm and
provider-kubernetes.

### Packaging

Each provider is a Crossplane project under `providers/<name>/`, with its own
`crossplane-project.yaml`, functions, uv workspace and generated models. Its
package depends on Modelplane's package, its cloud's Crossplane providers,
provider-helm and provider-kubernetes. The Crossplane CLI generates the
provider's models from Modelplane's package into the provider's own tree,
including the `ProvisionedCluster` model the provider imports.

Package object names are global across packages, so a provider's are named
for it: its Composition is `<provider>.provisionedclusters.infrastructure.modelplane.ai`
and its activation policy `modelplane-<provider>`. The provider SDK's check
enforces both.

A flake check enforces the boundary. Modelplane's XRDs, functions, project file
and the SDK don't mention any cloud. A provider uses nothing outside its
directory but the SDK, and the only Modelplane model it imports is the
contract's.

### Conformance

A provider shows it meets the contract with the conformance suite, which runs on
two kind clusters with no cloud credentials. The provider's `conformance.yaml`
lists the drivers it supports. It also describes the labels and taints a real
accelerator or CPU pool has, and the DaemonSets its device stack runs there.
The suite installs Modelplane and the provider, then checks two things.

On a workload cluster whose accelerator and CPU pools KWOK fakes, it installs a
serving stack and the device stack. Each DaemonSet must schedule onto its pool,
and nothing may tolerate or schedule onto a pool it doesn't belong on.

On the control plane it applies an `InferenceCluster` with an accelerator pool
and a CPU pool, built from the provider's cluster config and node classes.
Modelplane must select the provider's Composition. The provider must accept the
contract, activate its kinds and compose managed resources of them. Last, the
`InferenceCluster` has to report its cluster as provisioning rather than
failed.

### Cloud workarounds

Each workaround below exists because of one cloud. The table shows where it
goes:

| Workaround | Where it lives |
| --- | --- |
| CoreWeave's public load balancer annotation | The CoreWeave provider, through `status.gateway.service.annotations` |
| Dropping CoreWeave's own GPU exporter from collection | The CoreWeave provider, through `status.telemetry.ignoredNamespaces` |
| Civo's single-H100 NVLink driver setting | The Civo provider's device stack |
| GKE's ResourceQuota admitting the DRA driver's critical pods | The GKE provider's device stack, and the serving stack's device stacks on an existing cluster, which may be on GKE |
| The same quota for dra-driver-cpu | The serving stack, on every cluster: it's harmless where nothing restricts critical pods |
| Endpoints beside EndpointSlices for kube-dns | `compose-inference-gateway`, on every cluster: any cluster may run kube-dns |
| Endpoint picker spreading across nodes, not zones | `compose-model-replica`, on every cluster: any cluster's nodes may lack zone labels |

## Alternatives considered

**Embed the provider's configuration.** The cluster config could live inside
the `InferenceCluster` as an `x-kubernetes-embedded-resource` or a
preserve-unknown-fields object the provider validates. It saves writing the two
objects. But the field becomes opaque to the API server, `kubectl explain` and
the docs. Validation moves from admission to reconcile, and the schema of
a user's own object would then depend on which provider the control plane has.
Typed references keep every field validated where it's written.

**Make the serving stack a second contract.** A provider could supply a
Composition for `ServingStack` whose pipeline runs Modelplane's function and
then its own, which adds the cloud's components to the same XR. Ordering across
the stacks would then be ordinary ordering inside one XR. But the provider
would depend on two things that aren't an API: the name Modelplane's function
installs under, and the composed-resource keys inside Modelplane's half.
Depending on cert-manager through an observed component keeps the boundary at
what's on the cluster.

**Pass the device stack through the contract.** The provider could return its
component list in the `ProvisionedCluster`'s status for the serving stack to
install. A component list contains arbitrary Helm values, which would make that
field opaque, and the contract exists to avoid opaque fields.

**Reference the provider's kind by API group alone.** Cluster API's contract and
Gateway API reference a provider's kind by group, and resolve the version
themselves. Modelplane never fetches a provider's objects, so the version in a
reference is the provider's to read. A reference with an `apiVersion` also
matches how Crossplane refers to everything else.

**Select the provider's Composition by name.** `compose-inference-cluster` could
set `compositionRef` to a name derived from the config's kind. The name would
have to encode the config's kind and group, and a name only identifies one
Composition: with labels, Modelplane can tell an uninstalled provider from two
conflicting ones, and a provider's Composition for a newer contract version
can carry the same labels.

**Report capabilities in the contract's status.** The provider could publish
what it supports on each `ProvisionedCluster`. Modelplane would then learn that
a provider ignores a field only after composing an XR that sets it. Reading the
Composition lets Modelplane decide before it composes anything.

**A cluster config per cluster.** Cluster API pairs every `Cluster` with an
infrastructure object of its own. The fields of a cluster config aren't specific
to one cluster, and sharing one config across a region's clusters means fewer
objects to write and to change. A user who wants one per cluster can still
write one per cluster.
