# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compose an InferenceCluster.

This function orchestrates the internal XRs that make up an inference
cluster. An Existing cluster is one the user registers with a kubeconfig.
A Provisioned cluster is one a cluster provider creates: this function
composes a ProvisionedCluster, the contract every provider implements,
and a provider's Composition provisions it from the cluster config and
node classes it references. Either way it then composes a ServingStack
on the cluster. See design/cluster-providers.md.

This function names no provider. It selects the provider's Composition
for a ProvisionedCluster by the cluster config's kind, and reads the
optional parts of the contract the provider implements off that
Composition, so it never asks a provider for something it can't do.

Node pools reference InferenceClasses. On a provisioned cluster a class's
node class says how to provision the pool; on an Existing cluster the
class is a pure description of a pool that already exists. Either way,
the class's devices populate status.schedulablePools so the scheduler
can match models.
"""

from typing import Final, Literal

import grpc
import pydantic
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from modelplane_sdk import contract
from models.ai.modelplane.agent.poolobservation import v1alpha1 as pov1alpha1
from models.ai.modelplane.inferenceclass import v1alpha1 as iclv1alpha1
from models.ai.modelplane.inferencecluster import v1alpha1
from models.ai.modelplane.inferencegateway import v1alpha1 as igv1alpha1
from models.ai.modelplane.infrastructure.provisionedcluster import v1alpha1 as pcv1alpha1
from models.ai.modelplane.infrastructure.servingstack import v1alpha1 as ssv1alpha1
from models.ai.modelplane.modelcache import v1alpha1 as mcv1alpha1
from models.io.crossplane.m.kubernetes.clusterproviderconfig import (
    v1alpha1 as k8scpcv1alpha1,
)
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.protection.clusterusage import v1beta1 as clusterusagev1beta1
from models.io.crossplane.protection.usage import v1beta1 as usagev1beta1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Cluster source discriminator values from the XRD enum.
SOURCE_EXISTING: Final = "Existing"
SOURCE_PROVISIONED: Final = "Provisioned"

# Condition types and reasons for the InferenceCluster XR.
CONDITION_TYPE_CLUSTER_READY = "ClusterReady"
CONDITION_TYPE_BACKEND_READY = "BackendReady"

CONDITION_REASON_CLUSTER_RUNNING = "ClusterRunning"
CONDITION_REASON_PROVISIONING = "Provisioning"
CONDITION_REASON_PROVIDER_ERROR = "ProviderError"
CONDITION_REASON_WAITING_FOR_CLUSTER = "WaitingForCluster"
CONDITION_REASON_WAITING_FOR_CLASSES = "WaitingForClasses"
CONDITION_REASON_BACKEND_HEALTHY = "BackendHealthy"
CONDITION_REASON_INSTALLING = "Installing"
CONDITION_REASON_INVALID_NODE_POOL = "InvalidNodePool"
CONDITION_REASON_UNSUPPORTED_DEVICES = "UnsupportedDevices"
CONDITION_REASON_NO_PROVIDER = "NoProvider"
CONDITION_REASON_CONFLICTING_PROVIDERS = "ConflictingProviders"
CONDITION_REASON_UNSUPPORTED_BY_PROVIDER = "UnsupportedByProvider"

# The DRA drivers whose device stacks the serving stack installs on every
# cluster, whoever provisioned it. A hand-kept mirror of the drivers
# compose-serving-stack's External joins tag - functions are self-contained
# by design - which the supported-drivers-mirror flake check holds in sync.
# Every provider supports them, because the contract obliges a provider to
# label a CPU pool's nodes for dra-driver-cpu.
_SERVING_STACK_DRA_DRIVERS = frozenset({"dra.cpu"})

# The driver whose devices a CPU pool publishes. A pool whose class claims no
# other DRA driver is a CPU pool.
_DRA_DRIVER_CPU = "dra.cpu"

# InferenceClass device claim values. An unset claim means DRA.
_CLAIM_DRA = "DRA"

# Composed resource keys for the backend XR, the ProvisionedCluster, and the
# Usage that holds the ProvisionedCluster until the backend is gone.
BACKEND_RESOURCE_KEY = "serving-stack"
PROVISIONED_RESOURCE_KEY = "provisioned-cluster"
_USAGE_PROVISIONED_BY_BACKEND = "usage-provisioned-cluster-by-backend"

# Composed resource key for the ClusterUsage that blocks the InferenceCluster's
# deletion while anything composes objects onto it.
#
# NOTE(negz): The key predates the guard covering more than ModelReplicas.
# Crossplane deletes a composed resource whose key disappears before it applies
# the one that replaces it, so renaming the key would drop the guard for a
# moment on upgrade.
_DELETION_GUARD_RESOURCE_KEY = "usage-replicas"

# Label stamped on ModelReplicas by compose-model-deployment, and on ModelRoutes
# by compose-model-service, carrying the name of the InferenceCluster the object
# is scheduled to. This function selects both by it to hold the deletion guard
# and to find the namespaces whose mirror it must compose on the cluster. Kept
# in sync with those functions' _LABEL_CLUSTER.
_LABEL_CLUSTER = "modelplane.ai/cluster"

# A namespaced resource in control-plane namespace `ml-team` composes its objects
# into child_name("mp", "ml-team") on the cluster, e.g. `mp-ml-team-1a2b3`, so a
# name need only be unique within a team. The hash keeps a long team namespace
# within Kubernetes' 63 character limit, and the prefix keeps a team namespace
# named `default` or `kube-system` off the cluster's own. provider-kubernetes
# won't create that namespace, so this function does, one per team that has a
# ModelReplica, ModelRoute or ModelCache on the cluster. The name and label are a
# cross-function contract with compose-model-replica, compose-model-route and
# compose-model-cache, whose objects land there, and with both gateways'
# listeners, which select routes by the label.
_NS_PREFIX = "mp"
_NS_LABEL = "modelplane.ai/namespace"

# The modelplane-system namespace. Used for the ServingStack XR,
# ClusterProviderConfig secretRefs, and status.namespace.
_NAMESPACE_SYSTEM = "modelplane-system"

# The cluster agent, which the serving stack installs, reports a pool's nodes
# and free devices in the status of a PoolObservation. It reports its Healthy
# condition Unknown, with this reason, when it can't observe the pool.
_AGENT_CONDITION_HEALTHY = "Healthy"
_AGENT_REASON_OBSERVATION_FAILED = "ObservationFailed"

# provider-kubernetes stops receiving watch events once the token in a
# cluster's kubeconfig expires, which on some clouds is after 15 minutes. A watched
# PoolObservation then only reaches this XR when it's polled, every 10 minutes
# by default, so it's polled more often.
# TODO(negz): Remove once provider-kubernetes fixes this.
# https://github.com/crossplane-contrib/provider-kubernetes/issues/598
_ANNOTATION_POLL_INTERVAL = "crossplane.io/poll-interval"
_POOL_OBSERVATION_POLL_INTERVAL = "30s"


def _name(meta: metav1.ObjectMeta | None) -> str:
    """The object's name, always set on resources read from the API server."""
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _group(ref: v1alpha1.ConfigRef) -> str:
    """The API group of the cluster config a reference names."""
    return ref.apiVersion.split("/")[0]


def _qualified_kind(ref: v1alpha1.ConfigRef) -> str:
    """The kind a reference names, qualified by its group, such as AcmeClusterConfig.acme.example.org."""
    return f"{ref.kind}.{_group(ref)}"


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    """The object's namespace, always set on namespaced resources read from the API server."""
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


def _gateway_hostname(cluster_name: str) -> str:
    """The internal name an InferenceGateway addresses this cluster's gateway by.

    Modelplane derives and resolves this itself, so a platform publishes no DNS
    per cluster: compose-inference-gateway composes a Service of this name on
    each InferenceGateway's cluster, pointing at this cluster's gateway address.
    The name doubles as the SNI an InferenceGateway sends and the SAN on this
    cluster
    gateway's serving certificate, so it is stable and identical from every
    cluster.

    The first label is the Service's name, which must be a DNS-1035 label:
    lowercase alphanumeric and '-', starting with a letter. A cluster name is a
    DNS-1123 subdomain, so it may contain dots or start with a digit; the
    "gateway" literal keeps the label starting with a letter. child_name hashes
    the raw cluster name, so two clusters whose names differ only in '.' versus
    '-' still get distinct labels; the dots are replaced afterwards, in the
    result rather than the input, so the sanitising can't collapse that
    distinction. NOTE(negz): assumes the cluster's DNS domain is the default
    cluster.local.
    """
    label = resource.child_name("gateway", cluster_name).replace(".", "-")
    return f"{label}.{_NAMESPACE_SYSTEM}.svc.cluster.local"


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self) -> None:
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext | None
    ) -> fnv1.RunFunctionResponse:  # ty: ignore[invalid-method-override]  # the generated grpc servicer base is untyped
        """Run the function."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")

        rsp = response.to(req)
        c = Composer(req, rsp)
        c.compose()
        return rsp


class Composer:
    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.InferenceCluster(**resource.struct_to_dict(req.observed.composite.resource))
        # Resolved InferenceClasses, keyed by class name. Populated by
        # resolve_classes().
        self.classes: dict[str, iclv1alpha1.InferenceClass] = {}
        # Client CA per InferenceGateway, keyed by gateway name.
        self.gateway_cas: dict[str, str] = {}

    def compose(self) -> None:
        # Crossplane discards a function's desired resources on any reconcile
        # that still has unfulfilled required resources, and re-invokes it once
        # they resolve. So every resource this function reads is required here,
        # up front, before any desired state is composed: that keeps them all on
        # the first invocation for Crossplane to resolve in one pass, rather than
        # a later require costing another round-trip on which this run's desired
        # state is thrown away.
        self.require_inputs()

        # The deletion guard, namespaces, and gateway CAs all run before any
        # early return: none depends on the cluster's source. Gating the guard
        # behind it would drop its ClusterUsage on a reconcile that returns
        # early without failing, and let the cluster be deleted while something
        # still uses it.
        self.compose_deletion_guard()
        self.compose_namespaces()
        self.resolve_gateway_cas()

        if not self.resolve_classes():
            return

        self.compose_input_usages()

        cluster = self.xr.spec.cluster
        if cluster.source == SOURCE_EXISTING and cluster.existing:
            self.compose_existing(cluster.existing)
        elif cluster.source == SOURCE_PROVISIONED and cluster.provisioned:
            if not self.compose_provisioned(cluster.provisioned):
                return
        else:
            # The XRD requires the block its source names.
            response.warning(self.rsp, f"spec.cluster.{cluster.source.lower()} is required")
            return

        self.compose_pool_observations()

    def require_inputs(self) -> None:
        """Require every resource this function reads. See compose() for why up front.

        The ModelReplicas and ModelRoutes scheduled to this cluster (by
        _LABEL_CLUSTER), and the ModelCaches staging onto it, drive the deletion
        guard and the mirrored namespaces; the InferenceGateways carry the
        client CAs the cluster gateway must accept, and those running here also
        drive the guard; the InferenceClasses back the node pools; and a
        provisioned cluster's provider is the Composition for its
        ProvisionedCluster.
        """
        name = _name(self.xr.metadata)
        for kind, key in (("ModelReplica", "model-replicas"), ("ModelRoute", "model-routes")):
            response.require_resources(
                self.rsp,
                name=key,
                api_version="modelplane.ai/v1alpha1",
                kind=kind,
                match_labels={_LABEL_CLUSTER: name},
            )
        # ModelCaches can't be label-selected to a cluster (one cache fans out to
        # many), so require them all and filter by status.clusters[] in
        # caches_on_cluster. A ModelCacheHydration, one per cluster and
        # _LABEL_CLUSTER-selectable like the replicas and routes, is the eventual
        # replacement.
        response.require_resources(
            self.rsp,
            name="model-caches",
            api_version="modelplane.ai/v1alpha1",
            kind="ModelCache",
        )
        response.require_resources(
            self.rsp,
            name="gateways",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceGateway",
        )
        for class_name in sorted({p.className for p in (self.xr.spec.nodePools or [])}):
            response.require_resources(
                self.rsp,
                name=f"class-{class_name}",
                api_version="modelplane.ai/v1alpha1",
                kind="InferenceClass",
                match_name=class_name,
            )
        provisioned = self.xr.spec.cluster.provisioned
        if self.xr.spec.cluster.source == SOURCE_PROVISIONED and provisioned:
            response.require_resources(
                self.rsp,
                name="provider",
                api_version="apiextensions.crossplane.io/v1",
                kind="Composition",
                match_labels={
                    contract.LABEL_CONFIG_GROUP: _group(provisioned.configRef),
                    contract.LABEL_CONFIG_KIND: provisioned.configRef.kind,
                },
            )

    def compose_deletion_guard(self) -> None:
        """Block deletion of the InferenceCluster while anything composes onto it.

        ModelReplicas, ModelRoutes, ModelCaches and InferenceGateways all compose
        provider-kubernetes Objects onto the cluster through its
        ClusterProviderConfig. Deleting the InferenceCluster out from under any
        of them strands those Objects: they lose the ClusterProviderConfig (and
        the cluster) they need to finalize, and they wedge until their
        finalizers are removed by hand.

        Most of them are namespaced and the InferenceCluster is cluster scoped,
        so a Usage can't reference the cluster from them: a namespaced Usage's
        `by` can't reach a namespaced resource from the cluster's scope, and a
        ClusterUsage's `by` can't reach a namespaced resource at all. So instead
        of protecting the cluster *by* its users, this protects it with a
        ClusterUsage that has no `by` at all. A reason-only Usage blocks
        deletion of its `of` resource until the Usage itself is gone.

        The guard is composed while any user is observed (required in
        require_inputs): a ModelReplica or ModelRoute labelled for this cluster,
        a ModelCache staging onto it, or an InferenceGateway running on it. When
        the last goes the function stops composing it; if a delete was already
        attempted, replayDeletion re-issues it once the ClusterUsage is gone.
        """
        name = _name(self.xr.metadata)
        gateways = [
            igv1alpha1.InferenceGateway.model_validate(g) for g in request.get_required_resources(self.req, "gateways")
        ]
        in_use = {
            "ModelReplicas": bool(request.get_required_resources(self.req, "model-replicas")),
            "ModelRoutes": bool(request.get_required_resources(self.req, "model-routes")),
            "ModelCaches": bool(self.caches_on_cluster()),
            "InferenceGateways": any(gw.spec.clusterName == name for gw in gateways),
        }
        users = [kind for kind, used in in_use.items() if used]
        if not users:
            return
        # Names the kinds in use, so whoever's delete is refused knows what to
        # delete first.
        reason = f"{users[0]} use this InferenceCluster"
        if len(users) > 1:
            reason = f"{', '.join(users[:-1])} and {users[-1]} use this InferenceCluster"

        resource.update(
            self.rsp.desired.resources[_DELETION_GUARD_RESOURCE_KEY],
            clusterusagev1beta1.ClusterUsage(
                spec=clusterusagev1beta1.Spec(
                    of=clusterusagev1beta1.Of(
                        apiVersion="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        resourceRef=clusterusagev1beta1.ResourceRef(name=name),
                    ),
                    reason=reason,
                    replayDeletion=True,
                ),
            ),
        )
        self.rsp.desired.resources[_DELETION_GUARD_RESOURCE_KEY].ready = fnv1.READY_TRUE

    def compose_input_usages(self) -> None:
        """Block deletion of the InferenceClasses the node pools reference.

        The function fails every reconcile while one of them is missing, so
        deleting one in use would stop this cluster reconciling until it's
        recreated. Each ClusterUsage blocks that delete while this cluster
        references the class. Once the cluster no longer references it, the
        function stops composing the ClusterUsage, and replayDeletion re-issues
        a delete that was already attempted. A provider guards the cluster
        config, node classes and reservations it reads the same way.

        The ClusterUsages give a reason rather than naming this cluster as
        their `by`. Crossplane keeps a Usage with a `by` until the `by` is gone,
        and a foreground delete of this cluster waits for its composed
        ClusterUsages, so neither would ever finish.
        """
        name = _name(self.xr.metadata)
        for class_name in self.classes:
            key = f"usage-inferenceclass-{class_name}"
            resource.update(
                self.rsp.desired.resources[key],
                clusterusagev1beta1.ClusterUsage(
                    spec=clusterusagev1beta1.Spec(
                        of=clusterusagev1beta1.Of(
                            apiVersion="modelplane.ai/v1alpha1",
                            kind="InferenceClass",
                            resourceRef=clusterusagev1beta1.ResourceRef(name=class_name),
                        ),
                        reason=f"InferenceCluster {name} uses this InferenceClass",
                        replayDeletion=True,
                    ),
                ),
            )
            self.rsp.desired.resources[key].ready = fnv1.READY_TRUE

    def caches_on_cluster(self) -> list[mcv1alpha1.ModelCache]:
        """The ModelCaches staging onto this cluster.

        Caches carry no cluster label, since one fans out to many clusters, so
        this filters the fleet-wide list to those whose status.clusters[] names
        this cluster.
        """
        name = _name(self.xr.metadata)
        staged = []
        for c in request.get_required_resources(self.req, "model-caches"):
            cache = mcv1alpha1.ModelCache.model_validate(c)
            if cache.status and cache.status.clusters and any(sc.name == name for sc in cache.status.clusters):
                staged.append(cache)
        return staged

    def compose_namespaces(self) -> None:
        """Compose the mirrored namespace each team's objects land in on the cluster.

        A ModelReplica, ModelRoute or ModelCache in control-plane namespace
        `ml-team` composes its objects into `mp-ml-team-<hash>` here, but
        provider-kubernetes won't create that namespace, so this does, one per team
        with any of the three on the cluster.

        The namespace is composed no-Delete so a team's namespace outlives the
        removal of any one of its objects. It's marked ready unconditionally: a
        namespace appearing for a newly-scheduled team must not flap the whole
        cluster's readiness, and a real failure to create it surfaces on the
        object whose own objects can't land.
        """
        name = _name(self.xr.metadata)
        # Replicas and routes are label-selected to this cluster, so every one
        # returned counts.
        namespaces = {
            _namespace(metav1.ObjectMeta.model_validate(o["metadata"]))
            for key in ("model-replicas", "model-routes")
            for o in request.get_required_resources(self.req, key)
        }
        namespaces |= {_namespace(c.metadata) for c in self.caches_on_cluster()}

        pc = resource.child_name(name, "cluster-kubeconfig")
        for ns in sorted(namespaces):
            obj = k8sobjv1alpha1.Object(
                metadata=metav1.ObjectMeta(namespace=_NAMESPACE_SYSTEM),
                spec=k8sobjv1alpha1.Spec(
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ClusterProviderConfig",
                        name=pc,
                    ),
                    readiness=k8sobjv1alpha1.Readiness(policy="SuccessfulCreate"),
                    forProvider=k8sobjv1alpha1.ForProvider(
                        manifest={
                            "apiVersion": "v1",
                            "kind": "Namespace",
                            "metadata": {"name": resource.child_name(_NS_PREFIX, ns), "labels": {_NS_LABEL: ns}},
                        }
                    ),
                ),
            )
            obj.spec.managementPolicies = ["Observe", "Create", "Update"]
            key = f"namespace-{ns}"
            resource.update(self.rsp.desired.resources[key], obj)
            self.rsp.desired.resources[key].ready = fnv1.READY_TRUE

    def compose_pool_observations(self) -> None:
        """Compose a PoolObservation on the cluster for each of its pools.

        The cluster agent reports each pool's nodes and free devices in the
        status of its PoolObservation, which schedulable_pools publishes. The
        agent and the PoolObservation CRD arrive with the serving stack, so
        they're first composed once the stack is ready, and kept after.

        provider-kubernetes watches each one, and polls it often in case the
        watch has stopped (see _POOL_OBSERVATION_POLL_INTERVAL), so the agent's
        reports reach this XR soon after they change. Each is
        marked ready unconditionally: the scheduler falls back to whole-node
        accounting for a pool it has no observation of, so a missing one mustn't
        hold up the cluster.

        Each is composed no-Delete. Nothing holds the cluster until they're
        gone, so deleting one could race the cluster's own deletion and never
        finish. A PoolObservation left on a cluster, or for a pool that's since
        been removed, has no effect on scheduling.
        """
        backend_ready = (
            resource.get_condition(self.req.observed.resources.get(BACKEND_RESOURCE_KEY), "Ready").status == "True"
        )
        pc = resource.child_name(_name(self.xr.metadata), "cluster-kubeconfig")
        for pool in self.xr.spec.nodePools or []:
            key = f"pool-observation-{pool.name}"
            if not (backend_ready or key in self.req.observed.resources):
                continue
            obj = k8sobjv1alpha1.Object(
                metadata=metav1.ObjectMeta(
                    namespace=_NAMESPACE_SYSTEM,
                    annotations={_ANNOTATION_POLL_INTERVAL: _POOL_OBSERVATION_POLL_INTERVAL},
                ),
                spec=k8sobjv1alpha1.Spec(
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(kind="ClusterProviderConfig", name=pc),
                    readiness=k8sobjv1alpha1.Readiness(policy="SuccessfulCreate"),
                    watch=True,
                    forProvider=k8sobjv1alpha1.ForProvider(
                        manifest=pov1alpha1.PoolObservation(
                            apiVersion="agent.modelplane.ai/v1alpha1",
                            kind="PoolObservation",
                            metadata=metav1.ObjectMeta(name=pool.name),
                            spec=pov1alpha1.Spec(poolName=pool.name),
                        ).model_dump(exclude_unset=True),
                    ),
                ),
            )
            obj.spec.managementPolicies = ["Observe", "Create", "Update"]
            resource.update(self.rsp.desired.resources[key], obj)
            self.rsp.desired.resources[key].ready = fnv1.READY_TRUE

    def observed_pool(self, pool_name: str) -> pov1alpha1.Status | None:
        """The status of a pool's PoolObservation, or None until the agent has observed it.

        The agent reports its conditions Unknown, and no nodes or devices, when
        it can't observe a pool. That's no observation at all, rather than an
        observation of an empty pool. So is a status that doesn't match the
        agent's schema, which an agent other than the version Modelplane
        installs could report. Raising would fail the whole cluster.
        """
        observed = self.req.observed.resources.get(f"pool-observation-{pool_name}")
        if not observed:
            return None
        try:
            obj = k8sobjv1alpha1.Object.model_validate(resource.struct_to_dict(observed.resource))
            if not obj.status or not obj.status.atProvider or not obj.status.atProvider.manifest:
                return None
            po = pov1alpha1.PoolObservation.model_validate(obj.status.atProvider.manifest)
        except pydantic.ValidationError as err:
            response.warning(
                self.rsp,
                f"Ignoring the cluster agent's observation of pool {pool_name}: it does not match the agent's "
                f"schema ({err.error_count()} problems), so the pool is counted in whole nodes.",
            )
            return None
        if po.status is None:
            return None
        healthy = next((c for c in po.status.conditions or [] if c.type == _AGENT_CONDITION_HEALTHY), None)
        if healthy is None or healthy.reason == _AGENT_REASON_OBSERVATION_FAILED:
            return None
        return po.status

    def resolve_gateway_cas(self) -> None:
        """Collect the client CA of every InferenceGateway in the fleet.

        Any gateway may forward to this cluster, and each signs its client
        certificate with a CA of its own, so the cluster gateway has to accept
        all of them. Read here rather than on the ServingStack because a
        ServingStack knows only its own cluster, and this is fleet-wide state.

        A gateway that hasn't published a CA yet is skipped. Its cluster's
        cert-manager may still be installing, and refusing traffic from every
        gateway because one isn't ready would be worse than accepting the rest.
        """
        for g in request.get_required_resources(self.req, "gateways"):
            gw = igv1alpha1.InferenceGateway.model_validate(g)
            if gw.status and gw.status.clientCACertificate:
                self.gateway_cas[_name(gw.metadata)] = gw.status.clientCACertificate

    def resolve_classes(self) -> bool:
        """Declare and fetch every InferenceClass referenced by
        spec.nodePools[].className (required in require_inputs). Returns False,
        having failed the reconcile, if any is missing."""
        class_names = sorted({p.className for p in (self.xr.spec.nodePools or [])})

        missing: list[str] = []
        for name in class_names:
            # Crossplane only fetches a required resource after the function
            # asks for it, and won't fetch at all if the result is fatal. An
            # absent key means Crossplane hasn't fetched it yet, so wait for it.
            if f"class-{name}" not in self.req.required_resources:
                return False
            d = request.get_required_resource(self.req, f"class-{name}")
            if d is None:
                missing.append(name)
                continue
            self.classes[name] = iclv1alpha1.InferenceClass.model_validate(d)

        if missing:
            self.fail(CONDITION_REASON_WAITING_FOR_CLASSES, f"Waiting for InferenceClasses: {', '.join(missing)}")
            return False

        return True

    def dra_drivers(self) -> list[str]:
        """DRA driver names claimed across the referenced classes' devices.

        Collected verbatim onto ServingStack spec.draDrivers, which filters
        the serving stack's tagged components to the device stacks the
        cluster's classes actually claim. Sorted, so the ServingStack spec is
        stable across reconciles.
        """
        return sorted(set().union(*(self.claimed_drivers(cls) for cls in self.classes.values())))

    def fail(self, reason: str, msg: str) -> None:
        """Stop composing the cluster, reporting why on its ClusterReady condition.

        The result is fatal, so Crossplane applies none of this reconcile's
        desired state. Its caller returns before composing the cluster XR, and
        Crossplane would otherwise delete an existing cluster XR that's missing
        from desired state, for example because someone deleted an
        InferenceClass or edited a node pool.
        """
        response.set_conditions(
            self.rsp,
            resource.Condition(typ=CONDITION_TYPE_CLUSTER_READY, status="False", reason=reason, message=msg),
        )
        response.fatal(self.rsp, msg)

    def compose_existing(self, existing: v1alpha1.Existing) -> None:
        """Compose an InferenceCluster backed by a user-supplied cluster.

        No gating needed: the user provides the kubeconfig Secret. Nothing
        provisions this cluster's device stacks, so its serving stack
        installs them.
        """
        secrets = [
            ssv1alpha1.Secret(
                type=contract.SECRET_TYPE_KUBECONFIG, name=existing.secretRef.name, key=existing.secretRef.key
            ),
        ]
        identity = existing.identitySecretRef
        if identity:
            secrets.append(
                ssv1alpha1.Secret(
                    type=identity.type,  # the XRD defaults it
                    name=identity.name,
                    key=identity.key,
                )
            )
        self.compose_cluster_provider_config(secrets)
        self.compose_serving_stack(secrets, device_stacks="Managed")

        cache = existing.cache.storageClassName if existing.cache else None
        self.write_status(self.schedulable_pools(), cache_storage_class=cache)
        self.derive_conditions(cluster_ready=True)

    def compose_provisioned(self, provisioned: v1alpha1.Provisioned) -> bool:
        """Compose an InferenceCluster a cluster provider provisions.

        Composes the ProvisionedCluster for the provider to implement, and
        once the provider reports how to reach the cluster, its
        ClusterProviderConfig and ServingStack. The ServingStack waits for
        the cluster's secrets rather than for the ProvisionedCluster to be
        Ready, because the provider's device stack depends on the serving
        stack's cert-manager.

        Returns False, having failed the reconcile, if the provider isn't
        installed or can't provision what the node pools ask for.
        """
        provider = self.resolve_provider(provisioned.configRef)
        if provider is None:
            return False
        capabilities, drivers = provider

        pools = self.provisioned_pools(provisioned.configRef, capabilities)
        if pools is None or not self.check_devices_supported(provisioned.configRef, drivers):
            return False

        self.compose_provisioned_cluster(provisioned, pools)

        observed = self.observed_provisioned()
        cluster_ready = resource.get_condition(self.req.observed.resources.get(PROVISIONED_RESOURCE_KEY), "Ready")
        backend_exists = BACKEND_RESOURCE_KEY in self.req.observed.resources

        relayed = self.provisioned_relay(observed)
        if relayed:
            secrets, annotations, ignored = relayed
            self.compose_cluster_provider_config(secrets)
            self.compose_serving_stack(
                secrets, device_stacks="External", service_annotations=annotations, ignored_namespaces=ignored
            )
            if not backend_exists:
                response.normal(self.rsp, "Cluster is reachable, composing its serving stack")
        if relayed or backend_exists:
            self.compose_provisioned_usage()

        status = observed.status if observed else None

        if cluster_ready.status == "True":
            self.rsp.desired.resources[PROVISIONED_RESOURCE_KEY].ready = fnv1.READY_TRUE

        cache = status.cache.storageClassName if status and status.cache else None
        self.write_status(self.schedulable_pools(), cache_storage_class=cache)

        # A provider that can't provision what it was asked for fails its
        # ProvisionedCluster's reconcile, which Crossplane reports on Synced.
        synced = resource.get_condition(self.req.observed.resources.get(PROVISIONED_RESOURCE_KEY), "Synced")
        if synced.status == "False":
            self.derive_conditions(
                cluster_ready=False, reason=CONDITION_REASON_PROVIDER_ERROR, message=synced.message or None
            )
            return True
        self.derive_conditions(cluster_ready=cluster_ready.status == "True")
        return True

    def resolve_provider(self, ref: v1alpha1.ConfigRef) -> tuple[frozenset[str], frozenset[str]] | None:
        """The capabilities and DRA drivers of the provider that provisions from ref's kind.

        Read from the provider's Composition for a ProvisionedCluster, which
        require_inputs requires by the labels naming ref's kind. Returns
        None, having failed the reconcile, if no installed provider
        provisions from that kind.
        """
        # Wait for Crossplane to fetch it. See resolve_classes.
        if "provider" not in self.req.required_resources:
            return None
        kind = _qualified_kind(ref)
        # Crossplane only selects a Composition for the ProvisionedCluster's
        # own type and version, whatever its labels.
        found = [
            c
            for c in request.get_required_resources(self.req, "provider")
            if c.get("spec", {}).get("compositeTypeRef") == {"apiVersion": contract.API_VERSION, "kind": contract.KIND}
        ]
        if not found:
            self.fail(
                CONDITION_REASON_NO_PROVIDER,
                f"No installed cluster provider provisions clusters from {kind}; install the provider that owns it",
            )
            return None
        if len(found) > 1:
            names = ", ".join(sorted(_name(metav1.ObjectMeta.model_validate(c["metadata"])) for c in found))
            self.fail(
                CONDITION_REASON_CONFLICTING_PROVIDERS,
                f"Several Compositions provision clusters from {kind} ({names}); uninstall all but one provider",
            )
            return None
        annotations = metav1.ObjectMeta.model_validate(found[0]["metadata"]).annotations or {}

        def listed(annotation: str) -> frozenset[str]:
            return frozenset(v.strip() for v in annotations.get(annotation, "").split(",") if v.strip())

        return listed(contract.ANNOTATION_CAPABILITIES), listed(contract.ANNOTATION_DRA_DRIVERS)

    def provisioned_pools(
        self, ref: v1alpha1.ConfigRef, capabilities: frozenset[str]
    ) -> list[pcv1alpha1.NodePool] | None:
        """The node pools as the ProvisionedCluster's spec.nodePools.

        Each pool's class must name a node class, and a pool may only use the
        optional parts of the contract the provider declares. Returns None,
        having failed the reconcile naming every offending pool, if not.
        """
        kind = _qualified_kind(ref)
        invalid: list[str] = []
        unsupported: list[str] = []
        pools: list[pcv1alpha1.NodePool] = []
        for pool in self.xr.spec.nodePools or []:
            cls = self.classes[pool.className]
            if not cls.spec.nodeClassRef:
                invalid.append(f"node pool {pool.name}'s InferenceClass {pool.className} has no nodeClassRef")
                continue
            wants: list[str] = []
            if pool.zones:
                wants.append(contract.CAPABILITY_ZONES)
            if pool.reservationRef:
                wants.append(contract.CAPABILITY_CAPACITY_RESERVATIONS)
            missing = [c for c in wants if c not in capabilities]
            if missing:
                unsupported.append(f"node pool {pool.name} needs {', '.join(missing)}")
                continue

            # A pool whose class claims a DRA driver other than dra.cpu runs
            # engines on that driver's devices. Any other pool is a CPU pool.
            drivers = self.claimed_drivers(cls)
            node_pool = pcv1alpha1.NodePool(
                name=pool.name,
                nodeClassRef=pcv1alpha1.NodeClassRef(
                    apiVersion=cls.spec.nodeClassRef.apiVersion,
                    kind=cls.spec.nodeClassRef.kind,
                    name=cls.spec.nodeClassRef.name,
                ),
                role="Accelerator" if drivers - {_DRA_DRIVER_CPU} else "CPU",
                nodeCount=pool.nodeCount,  # the XRD defaults it
            )
            # Set only when given. resource.update serializes with
            # exclude_unset, and assigning None would emit null.
            if pool.minNodeCount is not None:
                node_pool.minNodeCount = pool.minNodeCount
            if pool.maxNodeCount is not None:
                node_pool.maxNodeCount = pool.maxNodeCount
            if pool.zones:
                node_pool.zones = [pcv1alpha1.Zone(z.root) for z in pool.zones]
            if pool.reservationRef:
                node_pool.reservationRef = pcv1alpha1.ReservationRef(
                    apiVersion=pool.reservationRef.apiVersion,
                    kind=pool.reservationRef.kind,
                    name=pool.reservationRef.name,
                )
            pools.append(node_pool)

        if invalid:
            self.fail(CONDITION_REASON_INVALID_NODE_POOL, f"Can't provision node pools: {'; '.join(invalid)}")
            return None
        if unsupported:
            have = ", ".join(sorted(capabilities)) or "none"
            self.fail(
                CONDITION_REASON_UNSUPPORTED_BY_PROVIDER,
                f"{'; '.join(unsupported)}, which the provider for {kind} doesn't support (it supports {have})",
            )
            return None
        return pools

    def claimed_drivers(self, cls: iclv1alpha1.InferenceClass) -> set[str]:
        """The DRA drivers a class's claim: DRA devices name.

        A Synthetic device is described for scheduling and never claimed, and
        its driver is only the attribute domain its CEL selectors read, not an
        installable stack.
        """
        return {d.driver for d in cls.spec.devices if (d.claim or _CLAIM_DRA) == _CLAIM_DRA}

    def check_devices_supported(self, ref: v1alpha1.ConfigRef, provided: frozenset[str]) -> bool:
        """Gate on a class claiming a driver the cluster's provider can't serve.

        A provider supports the drivers its Composition declares, and every
        provider supports the drivers the serving stack installs on every
        cluster. Checked here, before the ProvisionedCluster or ServingStack
        is composed, because the cluster would provision devices nothing can
        drive. A typo'd or not-yet-supported driver lands here too; unknown
        and unsupported are the same failure. Gating is a fatal result, so a
        cluster already running when a class turns unsupported holds its
        existing resources. Every offending class is named, so fixing one
        doesn't just surface the next.
        """
        supported = provided | _SERVING_STACK_DRA_DRIVERS
        offending = []
        for name in sorted(self.classes):
            unsupported = self.claimed_drivers(self.classes[name]) - supported
            if unsupported:
                offending.append(f"class {name} claims {', '.join(sorted(unsupported))}")
        if not offending:
            return True
        kind = _qualified_kind(ref)
        msg = f"{'; '.join(offending)}; clusters from {kind} support {', '.join(sorted(supported))}"
        # A fatal result, not a bare return: everything after this gate
        # composes the cluster, so returning an empty desired state would
        # have Crossplane garbage-collect a running cluster - the
        # ProvisionedCluster, ServingStack and Usages - the moment a shared
        # class starts claiming an unsupported driver.
        self.fail(CONDITION_REASON_UNSUPPORTED_DEVICES, msg)
        return False

    def compose_provisioned_cluster(self, provisioned: v1alpha1.Provisioned, pools: list[pcv1alpha1.NodePool]) -> None:
        """Compose the ProvisionedCluster, selecting the provider's Composition by the config's kind.

        Crossplane picks a Composition at random when a selector matches
        several, and never reselects once it has picked one, so the selector
        names the config kind exactly, and the InferenceCluster XRD makes that
        kind immutable.
        """
        ref = provisioned.configRef
        resource.update(
            self.rsp.desired.resources[PROVISIONED_RESOURCE_KEY],
            pcv1alpha1.ProvisionedCluster(
                metadata=metav1.ObjectMeta(name=_name(self.xr.metadata)),
                spec=pcv1alpha1.Spec(
                    crossplane=pcv1alpha1.Crossplane(
                        compositionSelector=pcv1alpha1.CompositionSelector(
                            matchLabels={
                                contract.LABEL_CONFIG_GROUP: _group(ref),
                                contract.LABEL_CONFIG_KIND: ref.kind,
                            },
                        ),
                    ),
                    configRef=pcv1alpha1.ConfigRef(apiVersion=ref.apiVersion, kind=ref.kind, name=ref.name),
                    draDrivers=[pcv1alpha1.DraDriver(d) for d in self.dra_drivers()],
                    nodePools=pools,
                ),
            ),
        )

    def compose_provisioned_usage(self) -> None:
        """Block ProvisionedCluster deletion until the backend is deleted.

        The serving stack uninstalls its components through the cluster, so
        the cluster must outlive it. A ClusterUsage can't name a namespaced
        ServingStack, so this is a Usage in the ServingStack's namespace. It
        names the cluster-scoped ProvisionedCluster's namespace as "": with
        none, Crossplane's deletion webhook looks the Usage up as one of a
        ProvisionedCluster in the Usage's own namespace, finds none, and lets
        the ProvisionedCluster go.
        """
        resource.update(
            self.rsp.desired.resources[_USAGE_PROVISIONED_BY_BACKEND],
            usagev1beta1.Usage(
                metadata=metav1.ObjectMeta(namespace=_NAMESPACE_SYSTEM),
                spec=usagev1beta1.Spec(
                    of=usagev1beta1.Of(
                        apiVersion=contract.API_VERSION,
                        kind=contract.KIND,
                        resourceRef=usagev1beta1.ResourceRefModel(name=_name(self.xr.metadata), namespace=""),
                    ),
                    by=usagev1beta1.By(
                        apiVersion="infrastructure.modelplane.ai/v1alpha1",
                        kind="ServingStack",
                        resourceSelector=usagev1beta1.ResourceSelector(matchControllerRef=True),
                    ),
                    replayDeletion=True,
                ),
            ),
        )
        self.rsp.desired.resources[_USAGE_PROVISIONED_BY_BACKEND].ready = fnv1.READY_TRUE

    def observed_provisioned(self) -> pcv1alpha1.ProvisionedCluster | None:
        """The observed ProvisionedCluster, or None until Crossplane has created it."""
        observed = self.req.observed.resources.get(PROVISIONED_RESOURCE_KEY)
        if observed is None:
            return None
        return pcv1alpha1.ProvisionedCluster.model_validate(resource.struct_to_dict(observed.resource))

    def provisioned_relay(
        self, observed: pcv1alpha1.ProvisionedCluster | None
    ) -> tuple[list[ssv1alpha1.Secret], dict[str, str] | None, list[str] | None] | None:
        """What the serving stack needs from the provider, or None until there's a kubeconfig.

        The secrets it reaches the cluster with, the annotations its gateways'
        Service needs, and the namespaces its collector ignores. From the
        provider's status once it publishes a Kubeconfig secret. Before then,
        if the ServingStack already exists, from its own spec, so a provider's
        status briefly losing them doesn't delete the ServingStack or change
        it.
        """
        status = observed.status if observed else None
        reported = (status.secrets if status else None) or []
        if status and any(s.type == contract.SECRET_TYPE_KUBECONFIG for s in reported):
            gateway, telemetry = status.gateway, status.telemetry
            return (
                [ssv1alpha1.Secret(type=s.type, name=s.name, key=s.key, namespace=s.namespace) for s in reported],
                gateway.service.annotations if gateway and gateway.service else None,
                [n.root for n in telemetry.ignoredNamespaces or []] if telemetry else None,
            )

        backend = self.req.observed.resources.get(BACKEND_RESOURCE_KEY)
        if backend is None:
            return None
        spec = ssv1alpha1.ServingStack.model_validate(resource.struct_to_dict(backend.resource)).spec
        return (
            list(spec.secrets),
            spec.gateway.service.annotations if spec.gateway.service else None,
            [n.root for n in spec.telemetry.ignoredNamespaces or []] if spec.telemetry else None,
        )

    def compose_serving_stack(
        self,
        secrets: list[ssv1alpha1.Secret],
        *,
        device_stacks: Literal["Managed", "External"],
        service_annotations: dict[str, str] | None = None,
        ignored_namespaces: list[str] | None = None,
    ) -> None:
        """Compose a ServingStack XR with the given secrets.

        device_stacks says whether the serving stack installs the device
        stacks for the classes' DRA drivers (an Existing cluster) or the
        cluster's provider does. spec.draDrivers is always set - an empty
        list when no class claims a device - so the stack filters its tagged
        device stacks to the drivers the classes claim. service_annotations
        and ignored_namespaces are what the provider reports its cluster's
        gateways and telemetry collector need.
        """
        # The gateway's name and the CAs it should accept client certificates
        # from. The name is Modelplane's own, derived from this cluster's name;
        # the CAs are from every InferenceGateway in the fleet, because any of
        # them may forward here and each signs its client certificate with its
        # own CA.
        gateway = ssv1alpha1.Gateway(hostname=_gateway_hostname(_name(self.xr.metadata)))
        if self.gateway_cas:
            gateway.clientCAs = [
                ssv1alpha1.ClientCA(name=name, certificate=cert) for name, cert in sorted(self.gateway_cas.items())
            ]
        if service_annotations:
            gateway.service = ssv1alpha1.Service(annotations=service_annotations)
        spec = ssv1alpha1.Spec(
            secrets=secrets,
            stack=self.xr.spec.stack,
            deviceStacks=device_stacks,
            gateway=gateway,
            draDrivers=[ssv1alpha1.DraDriver(d) for d in self.dra_drivers()],
        )
        if ignored_namespaces:
            spec.telemetry = ssv1alpha1.Telemetry(
                ignoredNamespaces=[ssv1alpha1.IgnoredNamespace(n) for n in ignored_namespaces]
            )
        resource.update(
            self.rsp.desired.resources[BACKEND_RESOURCE_KEY],
            ssv1alpha1.ServingStack(
                metadata=metav1.ObjectMeta(
                    name=resource.child_name(_name(self.xr.metadata), "serving-stack"),
                    namespace=_NAMESPACE_SYSTEM,
                ),
                spec=spec,
            ),
        )

    def compose_cluster_provider_config(self, secrets: list[ssv1alpha1.Secret]) -> None:
        """Compose a ClusterProviderConfig for provider-kubernetes so that
        ModelReplicas can create Objects on the remote cluster.

        It authenticates with the Kubeconfig secret, and as the identity in
        the first other secret, if any, on top of it. A secret is in
        modelplane-system unless it says otherwise: a provider may reuse the
        credential its cloud's ProviderConfig references, wherever that is.
        """
        kubeconfig = next(s for s in secrets if s.type == contract.SECRET_TYPE_KUBECONFIG)
        cpc = k8scpcv1alpha1.ClusterProviderConfig(
            metadata=metav1.ObjectMeta(name=resource.child_name(_name(self.xr.metadata), "cluster-kubeconfig")),
            spec=k8scpcv1alpha1.Spec(
                credentials=k8scpcv1alpha1.Credentials(
                    source="Secret",
                    secretRef=k8scpcv1alpha1.SecretRef(
                        namespace=kubeconfig.namespace or _NAMESPACE_SYSTEM,
                        name=kubeconfig.name,
                        key=kubeconfig.key,
                    ),
                ),
            ),
        )
        identity = next((s for s in secrets if s.type != contract.SECRET_TYPE_KUBECONFIG), None)
        if identity:
            cpc.spec.identity = k8scpcv1alpha1.Identity(
                type=identity.type,
                source="Secret",
                secretRef=k8scpcv1alpha1.SecretRef(
                    namespace=identity.namespace or _NAMESPACE_SYSTEM,
                    name=identity.name,
                    key=identity.key,
                ),
            )
        resource.update(self.rsp.desired.resources["cluster-provider-config-kubernetes"], cpc)
        self.rsp.desired.resources["cluster-provider-config-kubernetes"].ready = fnv1.READY_TRUE

    def write_status(
        self, schedulable_pools: list[v1alpha1.SchedulablePool], *, cache_storage_class: str | None
    ) -> None:
        """Write the InferenceCluster status.

        cache_storage_class is the effective ReadWriteMany StorageClass for
        ModelCache volumes, relayed up so ModelCache can target it: the one a
        provider reports, or the one the user names on an Existing cluster.
        """
        status = v1alpha1.Status(
            providerConfigRef=v1alpha1.ProviderConfigRef(
                name=resource.child_name(_name(self.xr.metadata), "cluster-kubeconfig"),
            ),
            namespace=_NAMESPACE_SYSTEM,
            schedulablePools=schedulable_pools,
        )
        if cache_storage_class:
            status.cache = v1alpha1.CacheModel(storageClassName=cache_storage_class)
        gateway_address = self.observed_gateway_address()
        if gateway_address:
            status.gateway = v1alpha1.Gateway(address=gateway_address)
            # Republished from the ServingStack so an InferenceGateway can
            # validate this cluster's gateway without reading a ServingStack,
            # which is machine-generated and not something another composition
            # should depend on the shape of.
            ca = self.observed_gateway_ca()
            if ca:
                status.gateway.caCertificate = ca
            # The hostname is what makes this cluster schedulable, so it is
            # published only once traffic to it is mutually authenticated in
            # both directions. That needs three things:
            #
            # An address, because an InferenceGateway addresses this cluster by
            # name, so publishing a name that resolves to nothing strands every
            # request routed to it.
            #
            # This cluster's CA, so an InferenceGateway can tell it reached this
            # cluster rather than whatever else answers on that address.
            #
            # At least one InferenceGateway CA, because the cluster gateway only
            # demands a client certificate when it has one to check against, and
            # a cluster gateway with nothing to trust serves nothing (see the
            # serving stack's serves_gateway). Publishing the hostname anyway
            # would make the cluster schedulable when it has no front door, so
            # every request routed to it would be stranded.
            if ca and self.gateway_cas:
                status.gateway.hostname = _gateway_hostname(_name(self.xr.metadata))
        # The SDK dumps a model in Python mode, leaving a pool's
        # lastAllocationTime a datetime a protobuf Struct can't hold.
        resource.update_status(
            self.rsp.desired.composite,
            status.model_dump(mode="json", exclude_unset=True, by_alias=True, warnings=False),
        )

    def derive_conditions(self, *, cluster_ready: bool, reason: str | None = None, message: str | None = None) -> None:
        """Derive ClusterReady and BackendReady conditions.

        reason and message, when given, explain a cluster that isn't ready.
        """
        backend_ready = (
            resource.get_condition(self.req.observed.resources.get(BACKEND_RESOURCE_KEY), "Ready").status == "True"
        )
        if BACKEND_RESOURCE_KEY in self.rsp.desired.resources and backend_ready:
            self.rsp.desired.resources[BACKEND_RESOURCE_KEY].ready = fnv1.READY_TRUE

        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_CLUSTER_READY,
                status="True" if cluster_ready else "False",
                reason=CONDITION_REASON_CLUSTER_RUNNING if cluster_ready else reason or CONDITION_REASON_PROVISIONING,
                message=message,
            ),
        )

        if not cluster_ready:
            backend_reason = CONDITION_REASON_WAITING_FOR_CLUSTER
        elif backend_ready:
            backend_reason = CONDITION_REASON_BACKEND_HEALTHY
        else:
            backend_reason = CONDITION_REASON_INSTALLING

        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_BACKEND_READY,
                status="True" if backend_ready else "False",
                reason=backend_reason,
            ),
        )

    def schedulable_pools(self) -> list[v1alpha1.SchedulablePool]:
        """Derive status.schedulablePools from each pool's class and observation.

        The class declares the node's devices (DRA-style); the pool declares how
        many nodes it can have. We copy the class's devices verbatim so
        ModelDeployment.nodeSelector can match against them, and record the node
        count. Once the cluster agent has observed the pool, we also record the
        nodes it has now and how many devices are free on them, which the
        scheduler fits replicas against. Nothing here is specific to GPUs: a CPU
        pool's dra.cpu device lists like any other.
        """
        pools = []
        for pool in self.xr.spec.nodePools or []:
            cls = self.classes[pool.className]
            # Copy the class's devices verbatim, from the InferenceClass's model
            # to the InferenceCluster's. model_dump drops None fields, keeping
            # the typed attribute value objects (string/version/bool/int) one-of
            # clean. by_alias keeps DRA's wire names (bool/int) rather than the
            # generated bool_/int_ attributes.
            schedulable = v1alpha1.SchedulablePool(
                name=pool.name,
                nodes=pool.maxNodeCount or pool.nodeCount,
                devices=[
                    v1alpha1.Device.model_validate(d.model_dump(by_alias=True, exclude_none=True))
                    for d in cls.spec.devices
                ],
            )
            observed = self.observed_pool(pool.name)
            if observed is not None:
                schedulable.observedNodes = observed.nodes.total
                schedulable.nodesByFreeDevices = [
                    v1alpha1.NodesByFreeDevice.model_validate(b.model_dump(exclude_none=True))
                    for b in observed.nodesByFreeDevices or []
                ]
                # The agent reports a time once a claim has allocated devices
                # on the pool.
                if observed.lastAllocationTime is not None:
                    schedulable.lastAllocationTime = observed.lastAllocationTime
            pools.append(schedulable)
        return pools

    def observed_gateway_ca(self) -> str | None:
        """The cluster gateway's CA certificate, from the observed backend.

        Read by dict rather than through a typed model, matching
        observed_gateway_address, so it works for any backend following the
        status.gateway contract.
        """
        observed = self.req.observed.resources.get(BACKEND_RESOURCE_KEY)
        if not observed:
            return None
        d = resource.struct_to_dict(observed.resource)
        return d.get("status", {}).get("gateway", {}).get("caCertificate")

    def observed_gateway_address(self) -> str | None:
        """Read the backend's gateway address from observed state.
        Uses dict access instead of a typed model so it works for any
        backend that follows the status.gateway.address contract."""
        observed = self.req.observed.resources.get(BACKEND_RESOURCE_KEY)
        if not observed:
            return None
        d = resource.struct_to_dict(observed.resource)
        return d.get("status", {}).get("gateway", {}).get("address")
