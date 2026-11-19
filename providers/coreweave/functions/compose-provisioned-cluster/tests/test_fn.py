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

"""Tests for the compose-provisioned-cluster function."""

import asyncio
import dataclasses
import json

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.coreweave.coreweaveclusterconfig import v1alpha1 as configv1alpha1
from models.ai.modelplane.coreweave.coreweavenodeclass import v1alpha1 as ncv1alpha1
from models.ai.modelplane.infrastructure.provisionedcluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-provisioned-cluster."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(*, config_ref: v1alpha1.ConfigRef, node_pools: list[v1alpha1.NodePool]) -> fnv1.Resource:
    """The prod ProvisionedCluster XR, claiming NVIDIA GPUs, with the given configRef and node pools."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.ProvisionedCluster(
                metadata=metav1.ObjectMeta(name="prod"),
                spec=v1alpha1.Spec(
                    configRef=config_ref,
                    draDrivers=[v1alpha1.DraDriver("gpu.nvidia.com")],
                    nodePools=node_pools,
                ),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        ),
    )


def _desired_xr(*, secrets: list[dict[str, str]] | None, storage_class_name: str | None) -> fnv1.Resource:
    """The desired ProvisionedCluster, reporting what CKS gateways and telemetry need, plus secrets and cache unless they're None."""
    status: dict = {
        "gateway": {"service": {"annotations": {"service.beta.kubernetes.io/coreweave-load-balancer-type": "public"}}},
        "telemetry": {"ignoredNamespaces": ["cw-exporters"]},
    }
    if secrets is not None:
        status["secrets"] = secrets
    if storage_class_name is not None:
        status["cache"] = {"storageClassName": storage_class_name}
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}))


def _config() -> fnv1.Resource:
    """The us-east CoreWeaveClusterConfig, as Crossplane fetches it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "coreweave.modelplane.ai/v1alpha1",
                "kind": "CoreWeaveClusterConfig",
                "metadata": {"name": "us-east"},
                "spec": {
                    "zone": "US-EAST-04A",
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ClusterProviderConfig", "name": "coreweave"},
                },
            }
        ),
    )


def _node_class(*, name: str, instance_type: str, accelerator: str | None) -> fnv1.Resource:
    """A CoreWeaveNodeClass as Crossplane fetches it, naming an accelerator unless it's None."""
    spec: dict = {"instanceType": instance_type}
    if accelerator is not None:
        spec["accelerator"] = {"type": accelerator}
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "coreweave.modelplane.ai/v1alpha1",
                "kind": "CoreWeaveNodeClass",
                "metadata": {"name": name},
                "spec": spec,
            }
        ),
    )


def _usage(*, kind: str, name: str, reason: str) -> fnv1.Resource:
    """A composed ClusterUsage blocking deletion of a CoreWeave object the ProvisionedCluster references."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "ClusterUsage",
                "spec": {
                    "of": {
                        "apiVersion": "coreweave.modelplane.ai/v1alpha1",
                        "kind": kind,
                        "resourceRef": {"name": name},
                    },
                    "reason": reason,
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _activation(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed ManagedResourceActivationPolicy activating the kinds compose-coreweave-cluster composes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "apiextensions.crossplane.io/v1alpha1",
                "kind": "ManagedResourceActivationPolicy",
                "spec": {
                    "activate": [
                        "clusters.cks.coreweave.m.upbound.io",
                        "vpcs.networking.coreweave.m.upbound.io",
                    ],
                },
            }
        ),
        ready=ready,
    )


def _observed_activation(*, activated: list[str]) -> fnv1.Resource:
    """The ManagedResourceActivationPolicy as observed, reporting the kinds it has activated."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "apiextensions.crossplane.io/v1alpha1",
                "kind": "ManagedResourceActivationPolicy",
                "metadata": {"name": "prod-a1b2c"},
                "spec": {
                    "activate": [
                        "clusters.cks.coreweave.m.upbound.io",
                        "vpcs.networking.coreweave.m.upbound.io",
                    ],
                },
                "status": {"activated": activated},
            }
        ),
    )


def _cluster(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed CoreWeaveCluster, with a GPU pool and a CPU pool built from their node classes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "coreweave.modelplane.ai/v1alpha1",
                "kind": "CoreWeaveCluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "zone": "US-EAST-04A",
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ClusterProviderConfig", "name": "coreweave"},
                    "nodePools": [
                        {
                            "name": "gpu",
                            "role": "GPU",
                            "instanceType": "gd-8xh100ib-i128",
                            "gpu": {"acceleratorType": "nvidia-h100"},
                            "nodeCount": 1,
                            "minNodeCount": 1,
                            "maxNodeCount": 4,
                        },
                        {
                            "name": "cpu",
                            "role": "CPU",
                            "instanceType": "turin-gp",
                            "nodeCount": 2,
                        },
                    ],
                },
            }
        ),
        ready=ready,
    )


def _observed_cluster(*, ready: bool) -> fnv1.Resource:
    """The CoreWeaveCluster as observed, reporting its kubeconfig Secret and cache StorageClass, with a Ready condition that's True if ready and False if not."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "coreweave.modelplane.ai/v1alpha1",
                "kind": "CoreWeaveCluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "zone": "US-EAST-04A",
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ClusterProviderConfig", "name": "coreweave"},
                    "nodePools": [
                        {
                            "name": "gpu",
                            "role": "GPU",
                            "instanceType": "gd-8xh100ib-i128",
                            "gpu": {"acceleratorType": "nvidia-h100"},
                            "nodeCount": 1,
                            "minNodeCount": 1,
                            "maxNodeCount": 4,
                        },
                        {
                            "name": "cpu",
                            "role": "CPU",
                            "instanceType": "turin-gp",
                            "nodeCount": 2,
                        },
                    ],
                },
                "status": {
                    "secrets": [{"type": "Kubeconfig", "name": "prod-kubeconfig-6c1d0", "key": "kubeconfig"}],
                    "cache": {"storageClassName": "modelplane-rwx-fs"},
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True" if ready else "False",
                            "reason": "Available" if ready else "Unavailable",
                            "lastTransitionTime": "2024-01-01T00:00:00Z",
                        },
                    ],
                },
            }
        ),
    )


def _device_stack(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed CoreWeaveDeviceStack, reaching the cluster through its kubeconfig Secret."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "coreweave.modelplane.ai/v1alpha1",
                "kind": "CoreWeaveDeviceStack",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "secrets": [
                        {
                            "type": "Kubeconfig",
                            "name": "prod-kubeconfig-6c1d0",
                            "namespace": "modelplane-system",
                            "key": "kubeconfig",
                        },
                    ],
                    "draDrivers": ["gpu.nvidia.com"],
                },
            }
        ),
        ready=ready,
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    Case(
        name="ClusterConfig",
        reason="A CoreWeaveClusterConfig XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        configv1alpha1.CoreWeaveClusterConfig(
                            metadata=metav1.ObjectMeta(name="us-east"),
                            spec=configv1alpha1.Spec(zone="US-EAST-04A"),
                        ).model_dump(exclude_none=True, mode="json", by_alias=True)
                    ),
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="NodeClass",
        reason="A CoreWeaveNodeClass XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        ncv1alpha1.CoreWeaveNodeClass(
                            metadata=metav1.ObjectMeta(name="h100"),
                            spec=ncv1alpha1.Spec(
                                instanceType="gd-8xh100ib-i128",
                                accelerator=ncv1alpha1.Accelerator(type="nvidia-h100"),
                            ),
                        ).model_dump(exclude_none=True, mode="json", by_alias=True)
                    ),
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="FirstPass",
        reason="With nothing fetched yet, a ProvisionedCluster requires its config and node classes and composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveClusterConfig", name="us-east"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="turin"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveClusterConfig",
                        match_name="us-east",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="h100",
                    ),
                    "node-class-turin": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="turin",
                    ),
                },
            ),
        ),
    ),
    # The cpu pool was added since Crossplane last fetched the required
    # resources, so it hasn't fetched the turin node class yet.
    Case(
        name="NodeClassNotFetched",
        reason="A node class Crossplane hasn't fetched yet holds back composing without failing the reconcile.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveClusterConfig", name="us-east"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="turin"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[_node_class(name="h100", instance_type="gd-8xh100ib-i128", accelerator="nvidia-h100")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveClusterConfig",
                        match_name="us-east",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="h100",
                    ),
                    "node-class-turin": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="turin",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="NodeClassMissing",
        reason="A node class that doesn't exist fails the reconcile, naming it as missing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveClusterConfig", name="us-east"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="turin"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[_node_class(name="h100", instance_type="gd-8xh100ib-i128", accelerator="nvidia-h100")],
                ),
                "node-class-turin": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_FATAL, message="Missing CoreWeaveNodeClass turin"),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveClusterConfig",
                        match_name="us-east",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="h100",
                    ),
                    "node-class-turin": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="turin",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ForeignKinds",
        reason="A configRef and a nodeClassRef to another provider's kinds fail the reconcile, naming both.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="gke.modelplane.ai/v1alpha1", kind="GKEClusterConfig", name="us-central"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="turin"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message=(
                        "configRef is a GKEClusterConfig.gke.modelplane.ai, not a "
                        "CoreWeaveClusterConfig.coreweave.modelplane.ai; node pool gpu's nodeClassRef is a "
                        "GKENodeClass.gke.modelplane.ai, not a CoreWeaveNodeClass.coreweave.modelplane.ai"
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-central",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="h100",
                    ),
                    "node-class-turin": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="turin",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="Reservation",
        reason="A node pool with a reservationRef fails the reconcile, because CoreWeave has no capacity reservations.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveClusterConfig", name="us-east"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="gke.modelplane.ai/v1alpha1",
                                kind="GKECapacityReservation",
                                name="h100-reserved",
                            ),
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="turin"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message=(
                        "node pool gpu's reservationRef is a GKECapacityReservation.gke.modelplane.ai, "
                        "not nothing, because this provider has no reservations"
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveClusterConfig",
                        match_name="us-east",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="h100",
                    ),
                    "node-class-turin": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="turin",
                    ),
                    "reservation-h100-reserved": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKECapacityReservation",
                        match_name="h100-reserved",
                    ),
                },
            ),
        ),
    ),
    # Each pool names the other's node class.
    Case(
        name="RoleMismatch",
        reason=(
            "An Accelerator pool whose node class names no accelerator and a CPU pool whose node class names one "
            "fail the reconcile, naming both, once the ClusterUsages guard the classes."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveClusterConfig", name="us-east"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="turin"
                            ),
                            nodeCount=1,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="h100"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[_node_class(name="h100", instance_type="gd-8xh100ib-i128", accelerator="nvidia-h100")],
                ),
                "node-class-turin": fnv1.Resources(
                    items=[_node_class(name="turin", instance_type="turin-gp", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-coreweaveclusterconfig-us-east": _usage(
                        kind="CoreWeaveClusterConfig",
                        name="us-east",
                        reason="ProvisionedCluster prod uses this CoreWeaveClusterConfig",
                    ),
                    "usage-coreweavenodeclass-h100": _usage(
                        kind="CoreWeaveNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                    "usage-coreweavenodeclass-turin": _usage(
                        kind="CoreWeaveNodeClass",
                        name="turin",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message=(
                        "node pool gpu runs accelerators, but CoreWeaveNodeClass turin names no accelerator; "
                        "node pool cpu is a CPU pool, but CoreWeaveNodeClass h100 names an accelerator"
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveClusterConfig",
                        match_name="us-east",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="h100",
                    ),
                    "node-class-turin": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="turin",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="NotActivated",
        reason=(
            "Until the activation policy reports every kind active, a ProvisionedCluster composes the policy and "
            "ClusterUsages but no CoreWeaveCluster, and reports what CKS gateways and telemetry need."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveClusterConfig", name="us-east"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="turin"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(activated=["clusters.cks.coreweave.m.upbound.io"]),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[_node_class(name="h100", instance_type="gd-8xh100ib-i128", accelerator="nvidia-h100")],
                ),
                "node-class-turin": fnv1.Resources(
                    items=[_node_class(name="turin", instance_type="turin-gp", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-coreweaveclusterconfig-us-east": _usage(
                        kind="CoreWeaveClusterConfig",
                        name="us-east",
                        reason="ProvisionedCluster prod uses this CoreWeaveClusterConfig",
                    ),
                    "usage-coreweavenodeclass-h100": _usage(
                        kind="CoreWeaveNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                    "usage-coreweavenodeclass-turin": _usage(
                        kind="CoreWeaveNodeClass",
                        name="turin",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveClusterConfig",
                        match_name="us-east",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="h100",
                    ),
                    "node-class-turin": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="turin",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="Activated",
        reason=(
            "Once the activation policy reports every kind active, a ProvisionedCluster composes a CoreWeaveCluster "
            "with a GPU pool and a CPU pool from their node classes, and the version and credentials from its config."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveClusterConfig", name="us-east"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="turin"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=["clusters.cks.coreweave.m.upbound.io", "vpcs.networking.coreweave.m.upbound.io"],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[_node_class(name="h100", instance_type="gd-8xh100ib-i128", accelerator="nvidia-h100")],
                ),
                "node-class-turin": fnv1.Resources(
                    items=[_node_class(name="turin", instance_type="turin-gp", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-coreweaveclusterconfig-us-east": _usage(
                        kind="CoreWeaveClusterConfig",
                        name="us-east",
                        reason="ProvisionedCluster prod uses this CoreWeaveClusterConfig",
                    ),
                    "usage-coreweavenodeclass-h100": _usage(
                        kind="CoreWeaveNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                    "usage-coreweavenodeclass-turin": _usage(
                        kind="CoreWeaveNodeClass",
                        name="turin",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "coreweave-cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveClusterConfig",
                        match_name="us-east",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="h100",
                    ),
                    "node-class-turin": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="turin",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ActivatedDefaults",
        reason=(
            "A ProvisionedCluster whose config and pool leave optional fields unset composes a CoreWeaveCluster that "
            "leaves them unset too."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveClusterConfig", name="us-west"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="l40"
                            ),
                            nodeCount=1,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=["clusters.cks.coreweave.m.upbound.io", "vpcs.networking.coreweave.m.upbound.io"],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "coreweave.modelplane.ai/v1alpha1",
                                    "kind": "CoreWeaveClusterConfig",
                                    "metadata": {"name": "us-west"},
                                    "spec": {"zone": "US-WEST-04A"},
                                }
                            ),
                        ),
                    ],
                ),
                "node-class-l40": fnv1.Resources(
                    items=[_node_class(name="l40", instance_type="gd-8xl40-i128", accelerator="nvidia-l40")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-coreweaveclusterconfig-us-west": _usage(
                        kind="CoreWeaveClusterConfig",
                        name="us-west",
                        reason="ProvisionedCluster prod uses this CoreWeaveClusterConfig",
                    ),
                    "usage-coreweavenodeclass-l40": _usage(
                        kind="CoreWeaveNodeClass",
                        name="l40",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "coreweave-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "coreweave.modelplane.ai/v1alpha1",
                                "kind": "CoreWeaveCluster",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "zone": "US-WEST-04A",
                                    "nodePools": [
                                        {
                                            "name": "gpu",
                                            "role": "GPU",
                                            "instanceType": "gd-8xl40-i128",
                                            "gpu": {"acceleratorType": "nvidia-l40"},
                                            "nodeCount": 1,
                                        },
                                    ],
                                },
                            }
                        ),
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-l40": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="l40",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ClusterReady",
        reason=(
            "Once the CoreWeaveCluster is Ready with a kubeconfig, a ProvisionedCluster composes a "
            "CoreWeaveDeviceStack reaching it through that Secret, and reports the Secret and the cache StorageClass."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveClusterConfig", name="us-east"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="turin"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=["clusters.cks.coreweave.m.upbound.io", "vpcs.networking.coreweave.m.upbound.io"],
                    ),
                    "coreweave-cluster": _observed_cluster(ready=True),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[_node_class(name="h100", instance_type="gd-8xh100ib-i128", accelerator="nvidia-h100")],
                ),
                "node-class-turin": fnv1.Resources(
                    items=[_node_class(name="turin", instance_type="turin-gp", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    secrets=[
                        {
                            "type": "Kubeconfig",
                            "name": "prod-kubeconfig-6c1d0",
                            "namespace": "modelplane-system",
                            "key": "kubeconfig",
                        },
                    ],
                    storage_class_name="modelplane-rwx-fs",
                ),
                resources={
                    "usage-coreweaveclusterconfig-us-east": _usage(
                        kind="CoreWeaveClusterConfig",
                        name="us-east",
                        reason="ProvisionedCluster prod uses this CoreWeaveClusterConfig",
                    ),
                    "usage-coreweavenodeclass-h100": _usage(
                        kind="CoreWeaveNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                    "usage-coreweavenodeclass-turin": _usage(
                        kind="CoreWeaveNodeClass",
                        name="turin",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "coreweave-cluster": _cluster(ready=fnv1.READY_TRUE),
                    "device-stack": _device_stack(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveClusterConfig",
                        match_name="us-east",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="h100",
                    ),
                    "node-class-turin": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="turin",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="DeviceStackReady",
        reason="Once the CoreWeaveDeviceStack reports Ready, a ProvisionedCluster marks it ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveClusterConfig", name="us-east"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="turin"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=["clusters.cks.coreweave.m.upbound.io", "vpcs.networking.coreweave.m.upbound.io"],
                    ),
                    "coreweave-cluster": _observed_cluster(ready=True),
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "coreweave.modelplane.ai/v1alpha1",
                                "kind": "CoreWeaveDeviceStack",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "secrets": [
                                        {
                                            "type": "Kubeconfig",
                                            "name": "prod-kubeconfig-6c1d0",
                                            "namespace": "modelplane-system",
                                            "key": "kubeconfig",
                                        },
                                    ],
                                    "draDrivers": ["gpu.nvidia.com"],
                                },
                                "status": {
                                    "conditions": [
                                        {
                                            "type": "Ready",
                                            "status": "True",
                                            "reason": "Available",
                                            "lastTransitionTime": "2024-01-01T00:00:00Z",
                                        },
                                    ],
                                },
                            }
                        ),
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[_node_class(name="h100", instance_type="gd-8xh100ib-i128", accelerator="nvidia-h100")],
                ),
                "node-class-turin": fnv1.Resources(
                    items=[_node_class(name="turin", instance_type="turin-gp", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    secrets=[
                        {
                            "type": "Kubeconfig",
                            "name": "prod-kubeconfig-6c1d0",
                            "namespace": "modelplane-system",
                            "key": "kubeconfig",
                        },
                    ],
                    storage_class_name="modelplane-rwx-fs",
                ),
                resources={
                    "usage-coreweaveclusterconfig-us-east": _usage(
                        kind="CoreWeaveClusterConfig",
                        name="us-east",
                        reason="ProvisionedCluster prod uses this CoreWeaveClusterConfig",
                    ),
                    "usage-coreweavenodeclass-h100": _usage(
                        kind="CoreWeaveNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                    "usage-coreweavenodeclass-turin": _usage(
                        kind="CoreWeaveNodeClass",
                        name="turin",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "coreweave-cluster": _cluster(ready=fnv1.READY_TRUE),
                    "device-stack": _device_stack(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveClusterConfig",
                        match_name="us-east",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="h100",
                    ),
                    "node-class-turin": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="turin",
                    ),
                },
            ),
        ),
    ),
    # The observed device stack is still Ready: only the cluster flapped.
    Case(
        name="ClusterReadyRegressed",
        reason=(
            "With the CoreWeaveCluster's Ready condition regressed to False, a ProvisionedCluster stops reporting its "
            "secrets but keeps the CoreWeaveDeviceStack from the device stack's own observed spec."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveClusterConfig", name="us-east"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="coreweave.modelplane.ai/v1alpha1", kind="CoreWeaveNodeClass", name="turin"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=["clusters.cks.coreweave.m.upbound.io", "vpcs.networking.coreweave.m.upbound.io"],
                    ),
                    "coreweave-cluster": _observed_cluster(ready=False),
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "coreweave.modelplane.ai/v1alpha1",
                                "kind": "CoreWeaveDeviceStack",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "secrets": [
                                        {
                                            "type": "Kubeconfig",
                                            "name": "prod-kubeconfig-6c1d0",
                                            "namespace": "modelplane-system",
                                            "key": "kubeconfig",
                                        },
                                    ],
                                    "draDrivers": ["gpu.nvidia.com"],
                                },
                                "status": {
                                    "conditions": [
                                        {
                                            "type": "Ready",
                                            "status": "True",
                                            "reason": "Available",
                                            "lastTransitionTime": "2024-01-01T00:00:00Z",
                                        },
                                    ],
                                },
                            }
                        ),
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[_node_class(name="h100", instance_type="gd-8xh100ib-i128", accelerator="nvidia-h100")],
                ),
                "node-class-turin": fnv1.Resources(
                    items=[_node_class(name="turin", instance_type="turin-gp", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name="modelplane-rwx-fs"),
                resources={
                    "usage-coreweaveclusterconfig-us-east": _usage(
                        kind="CoreWeaveClusterConfig",
                        name="us-east",
                        reason="ProvisionedCluster prod uses this CoreWeaveClusterConfig",
                    ),
                    "usage-coreweavenodeclass-h100": _usage(
                        kind="CoreWeaveNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                    "usage-coreweavenodeclass-turin": _usage(
                        kind="CoreWeaveNodeClass",
                        name="turin",
                        reason="ProvisionedCluster prod uses this CoreWeaveNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "coreweave-cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                    "device-stack": _device_stack(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveClusterConfig",
                        match_name="us-east",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="h100",
                    ),
                    "node-class-turin": fnv1.ResourceSelector(
                        api_version="coreweave.modelplane.ai/v1alpha1",
                        kind="CoreWeaveNodeClass",
                        match_name="turin",
                    ),
                },
            ),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction provisions a CKS cluster for a ProvisionedCluster."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
