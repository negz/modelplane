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
from models.ai.modelplane.infrastructure.provisionedcluster import v1alpha1
from models.ai.modelplane.nebius.nebiusclusterconfig import v1alpha1 as configv1alpha1
from models.ai.modelplane.nebius.nebiusnodeclass import v1alpha1 as ncv1alpha1
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
    """The desired ProvisionedCluster, reporting secrets and cache unless they're None."""
    status: dict = {}
    if secrets is not None:
        status["secrets"] = secrets
    if storage_class_name is not None:
        status["cache"] = {"storageClassName": storage_class_name}
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}))


def _config(*, infiniband_fabric: str | None) -> fnv1.Resource:
    """The eu-north NebiusClusterConfig, as Crossplane fetches it, naming an InfiniBand fabric unless it's None."""
    spec: dict = {
        "kubernetesVersion": "1.36",
        "credentials": {"type": "ClusterProviderConfig", "name": "nebius"},
    }
    if infiniband_fabric is not None:
        spec["infinibandFabric"] = infiniband_fabric
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "nebius.modelplane.ai/v1alpha1",
                "kind": "NebiusClusterConfig",
                "metadata": {"name": "eu-north"},
                "spec": spec,
            }
        ),
    )


def _node_class(
    *, name: str, platform: str, preset: str, disk_size_gb: int, accelerator: str | None, fabric: str
) -> fnv1.Resource:
    """A NebiusNodeClass as Crossplane fetches it, with its defaults filled in, naming an accelerator unless it's None."""
    spec: dict = {
        "platform": platform,
        "preset": preset,
        "diskSizeGb": disk_size_gb,
        "driversPreset": "cuda13.0",
        "fabric": fabric,
    }
    if accelerator is not None:
        spec["accelerator"] = {"type": accelerator}
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "nebius.modelplane.ai/v1alpha1",
                "kind": "NebiusNodeClass",
                "metadata": {"name": name},
                "spec": spec,
            }
        ),
    )


def _usage(*, kind: str, name: str, reason: str) -> fnv1.Resource:
    """A composed ClusterUsage blocking deletion of a Nebius object the ProvisionedCluster references."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "ClusterUsage",
                "spec": {
                    "of": {
                        "apiVersion": "nebius.modelplane.ai/v1alpha1",
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
    """The composed ManagedResourceActivationPolicy activating the kinds compose-nebius-cluster composes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "apiextensions.crossplane.io/v1alpha1",
                "kind": "ManagedResourceActivationPolicy",
                "spec": {
                    "activate": [
                        "filesystems.compute.nebius.m.upbound.io",
                        "gpuclusters.compute.nebius.m.upbound.io",
                        "clusters.mk8s.nebius.m.upbound.io",
                        "nodegroups.mk8s.nebius.m.upbound.io",
                        "networks.vpc.nebius.m.upbound.io",
                        "subnets.vpc.nebius.m.upbound.io",
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
                        "filesystems.compute.nebius.m.upbound.io",
                        "gpuclusters.compute.nebius.m.upbound.io",
                        "clusters.mk8s.nebius.m.upbound.io",
                        "nodegroups.mk8s.nebius.m.upbound.io",
                        "networks.vpc.nebius.m.upbound.io",
                        "subnets.vpc.nebius.m.upbound.io",
                    ],
                },
                "status": {"activated": activated},
            }
        ),
    )


def _cluster(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed NebiusCluster, with an autoscaled GPU pool on InfiniBand fabric-2 and a fixed CPU pool built from their node classes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "nebius.modelplane.ai/v1alpha1",
                "kind": "NebiusCluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ClusterProviderConfig", "name": "nebius"},
                    "nodePools": [
                        {
                            "name": "gpu",
                            "role": "GPU",
                            "platform": "gpu-h100-sxm",
                            "preset": "8gpu-128vcpu-1600gb",
                            "diskSizeGb": 200,
                            "nodeCount": 2,
                            "maxNodeCount": 4,
                            "gpu": {"acceleratorType": "nvidia-h100", "driversPreset": "cuda13.0"},
                            "fabric": {"type": "InfiniBand", "infiniband": {"fabric": "fabric-2"}},
                        },
                        {
                            "name": "cpu",
                            "role": "CPU",
                            "platform": "cpu-d3",
                            "preset": "16vcpu-64gb",
                            "diskSizeGb": 100,
                            "nodeCount": 2,
                        },
                    ],
                },
            }
        ),
        ready=ready,
    )


def _observed_cluster(*, ready: bool) -> fnv1.Resource:
    """The NebiusCluster as observed, reporting its kubeconfig and Nebius credentials Secrets and cache StorageClass, with a Ready condition that's True if ready and False if not."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "nebius.modelplane.ai/v1alpha1",
                "kind": "NebiusCluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ClusterProviderConfig", "name": "nebius"},
                    "nodePools": [
                        {
                            "name": "gpu",
                            "role": "GPU",
                            "platform": "gpu-h100-sxm",
                            "preset": "8gpu-128vcpu-1600gb",
                            "diskSizeGb": 200,
                            "nodeCount": 2,
                            "maxNodeCount": 4,
                            "gpu": {"acceleratorType": "nvidia-h100", "driversPreset": "cuda13.0"},
                            "fabric": {"type": "InfiniBand", "infiniband": {"fabric": "fabric-2"}},
                        },
                        {
                            "name": "cpu",
                            "role": "CPU",
                            "platform": "cpu-d3",
                            "preset": "16vcpu-64gb",
                            "diskSizeGb": 100,
                            "nodeCount": 2,
                        },
                    ],
                },
                "status": {
                    "secrets": [
                        {"type": "Kubeconfig", "name": "prod-kubeconfig-6c1d0", "key": "kubeconfig"},
                        {
                            "type": "NebiusServiceAccountCredentials",
                            "name": "nebius-credentials",
                            "namespace": "crossplane-system",
                            "key": "credentials.json",
                        },
                    ],
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
    """The composed NebiusDeviceStack, reaching the cluster through its kubeconfig as the Nebius service account."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "nebius.modelplane.ai/v1alpha1",
                "kind": "NebiusDeviceStack",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "secrets": [
                        {
                            "type": "Kubeconfig",
                            "name": "prod-kubeconfig-6c1d0",
                            "namespace": "modelplane-system",
                            "key": "kubeconfig",
                        },
                        {
                            "type": "NebiusServiceAccountCredentials",
                            "name": "nebius-credentials",
                            "namespace": "crossplane-system",
                            "key": "credentials.json",
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
        reason="A NebiusClusterConfig XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        configv1alpha1.NebiusClusterConfig(
                            metadata=metav1.ObjectMeta(name="eu-north"),
                            spec=configv1alpha1.Spec(infinibandFabric="fabric-2"),
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
        reason="A NebiusNodeClass XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        ncv1alpha1.NebiusNodeClass(
                            metadata=metav1.ObjectMeta(name="h100-ib"),
                            spec=ncv1alpha1.Spec(
                                platform="gpu-h100-sxm",
                                preset="8gpu-128vcpu-1600gb",
                                accelerator=ncv1alpha1.Accelerator(type="nvidia-h100"),
                                fabric="InfiniBand",
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
                        apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusClusterConfig", name="eu-north"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="h100-ib"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="cpu-d3"
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
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusClusterConfig",
                        match_name="eu-north",
                    ),
                    "node-class-h100-ib": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="h100-ib",
                    ),
                    "node-class-cpu-d3": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="cpu-d3",
                    ),
                },
            ),
        ),
    ),
    # The cpu pool was added since Crossplane last fetched the required
    # resources, so it hasn't fetched the cpu-d3 node class yet.
    Case(
        name="NodeClassNotFetched",
        reason="A node class Crossplane hasn't fetched yet holds back composing without failing the reconcile.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusClusterConfig", name="eu-north"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="h100-ib"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="cpu-d3"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config(infiniband_fabric="fabric-2")]),
                "node-class-h100-ib": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100-ib",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        ),
                    ],
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
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusClusterConfig",
                        match_name="eu-north",
                    ),
                    "node-class-h100-ib": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="h100-ib",
                    ),
                    "node-class-cpu-d3": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="cpu-d3",
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
                        apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusClusterConfig", name="eu-north"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="h100-ib"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="cpu-d3"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config(infiniband_fabric="fabric-2")]),
                "node-class-h100-ib": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100-ib",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        ),
                    ],
                ),
                "node-class-cpu-d3": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_FATAL, message="Missing NebiusNodeClass cpu-d3"),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusClusterConfig",
                        match_name="eu-north",
                    ),
                    "node-class-h100-ib": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="h100-ib",
                    ),
                    "node-class-cpu-d3": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="cpu-d3",
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
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="cpu-d3"
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
                        "NebiusClusterConfig.nebius.modelplane.ai; node pool gpu's nodeClassRef is a "
                        "GKENodeClass.gke.modelplane.ai, not a NebiusNodeClass.nebius.modelplane.ai"
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
                    "node-class-cpu-d3": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="cpu-d3",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="Reservation",
        reason="A node pool with a reservationRef fails the reconcile, because Nebius has no capacity reservations.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusClusterConfig", name="eu-north"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="h100-ib"
                            ),
                            nodeCount=2,
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
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="cpu-d3"
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
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusClusterConfig",
                        match_name="eu-north",
                    ),
                    "node-class-h100-ib": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="h100-ib",
                    ),
                    "node-class-cpu-d3": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="cpu-d3",
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
                        apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusClusterConfig", name="eu-north"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="cpu-d3"
                            ),
                            nodeCount=2,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="h100-ib"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config(infiniband_fabric="fabric-2")]),
                "node-class-h100-ib": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100-ib",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        ),
                    ],
                ),
                "node-class-cpu-d3": fnv1.Resources(
                    items=[
                        _node_class(
                            name="cpu-d3",
                            platform="cpu-d3",
                            preset="16vcpu-64gb",
                            disk_size_gb=100,
                            accelerator=None,
                            fabric="None",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-nebiusclusterconfig-eu-north": _usage(
                        kind="NebiusClusterConfig",
                        name="eu-north",
                        reason="ProvisionedCluster prod uses this NebiusClusterConfig",
                    ),
                    "usage-nebiusnodeclass-h100-ib": _usage(
                        kind="NebiusNodeClass",
                        name="h100-ib",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "usage-nebiusnodeclass-cpu-d3": _usage(
                        kind="NebiusNodeClass",
                        name="cpu-d3",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message=(
                        "node pool gpu runs accelerators, but NebiusNodeClass cpu-d3 names no accelerator; "
                        "node pool cpu is a CPU pool, but NebiusNodeClass h100-ib names an accelerator"
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusClusterConfig",
                        match_name="eu-north",
                    ),
                    "node-class-h100-ib": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="h100-ib",
                    ),
                    "node-class-cpu-d3": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="cpu-d3",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="InfiniBandWithoutFabric",
        reason=(
            "A pool whose node class wants InfiniBand fails the reconcile, once the ClusterUsages guard its inputs, "
            "when the cluster config names no InfiniBand fabric for it to join."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusClusterConfig", name="eu-north"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="h100-ib"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="cpu-d3"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config(infiniband_fabric=None)]),
                "node-class-h100-ib": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100-ib",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        ),
                    ],
                ),
                "node-class-cpu-d3": fnv1.Resources(
                    items=[
                        _node_class(
                            name="cpu-d3",
                            platform="cpu-d3",
                            preset="16vcpu-64gb",
                            disk_size_gb=100,
                            accelerator=None,
                            fabric="None",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-nebiusclusterconfig-eu-north": _usage(
                        kind="NebiusClusterConfig",
                        name="eu-north",
                        reason="ProvisionedCluster prod uses this NebiusClusterConfig",
                    ),
                    "usage-nebiusnodeclass-h100-ib": _usage(
                        kind="NebiusNodeClass",
                        name="h100-ib",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "usage-nebiusnodeclass-cpu-d3": _usage(
                        kind="NebiusNodeClass",
                        name="cpu-d3",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message=(
                        "node pool gpu's NebiusNodeClass h100-ib wants InfiniBand, "
                        "but NebiusClusterConfig eu-north names no infinibandFabric"
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusClusterConfig",
                        match_name="eu-north",
                    ),
                    "node-class-h100-ib": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="h100-ib",
                    ),
                    "node-class-cpu-d3": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="cpu-d3",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="NotActivated",
        reason=(
            "Until the activation policy reports every kind active, a ProvisionedCluster composes the policy and "
            "ClusterUsages but no NebiusCluster."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusClusterConfig", name="eu-north"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="h100-ib"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="cpu-d3"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(activated=["clusters.mk8s.nebius.m.upbound.io"]),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config(infiniband_fabric="fabric-2")]),
                "node-class-h100-ib": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100-ib",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        ),
                    ],
                ),
                "node-class-cpu-d3": fnv1.Resources(
                    items=[
                        _node_class(
                            name="cpu-d3",
                            platform="cpu-d3",
                            preset="16vcpu-64gb",
                            disk_size_gb=100,
                            accelerator=None,
                            fabric="None",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-nebiusclusterconfig-eu-north": _usage(
                        kind="NebiusClusterConfig",
                        name="eu-north",
                        reason="ProvisionedCluster prod uses this NebiusClusterConfig",
                    ),
                    "usage-nebiusnodeclass-h100-ib": _usage(
                        kind="NebiusNodeClass",
                        name="h100-ib",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "usage-nebiusnodeclass-cpu-d3": _usage(
                        kind="NebiusNodeClass",
                        name="cpu-d3",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusClusterConfig",
                        match_name="eu-north",
                    ),
                    "node-class-h100-ib": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="h100-ib",
                    ),
                    "node-class-cpu-d3": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="cpu-d3",
                    ),
                },
            ),
        ),
    ),
    # The gpu pool leaves minNodeCount unset, so compose-nebius-cluster
    # floors its autoscaling at its node count.
    Case(
        name="Activated",
        reason=(
            "Once the activation policy reports every kind active, a ProvisionedCluster composes a NebiusCluster "
            "with an autoscaled GPU pool on the config's InfiniBand fabric and a CPU pool from their node classes, "
            "and the version and credentials from its config."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusClusterConfig", name="eu-north"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="h100-ib"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="cpu-d3"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "filesystems.compute.nebius.m.upbound.io",
                            "gpuclusters.compute.nebius.m.upbound.io",
                            "clusters.mk8s.nebius.m.upbound.io",
                            "nodegroups.mk8s.nebius.m.upbound.io",
                            "networks.vpc.nebius.m.upbound.io",
                            "subnets.vpc.nebius.m.upbound.io",
                        ],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config(infiniband_fabric="fabric-2")]),
                "node-class-h100-ib": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100-ib",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        ),
                    ],
                ),
                "node-class-cpu-d3": fnv1.Resources(
                    items=[
                        _node_class(
                            name="cpu-d3",
                            platform="cpu-d3",
                            preset="16vcpu-64gb",
                            disk_size_gb=100,
                            accelerator=None,
                            fabric="None",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-nebiusclusterconfig-eu-north": _usage(
                        kind="NebiusClusterConfig",
                        name="eu-north",
                        reason="ProvisionedCluster prod uses this NebiusClusterConfig",
                    ),
                    "usage-nebiusnodeclass-h100-ib": _usage(
                        kind="NebiusNodeClass",
                        name="h100-ib",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "usage-nebiusnodeclass-cpu-d3": _usage(
                        kind="NebiusNodeClass",
                        name="cpu-d3",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "nebius-cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusClusterConfig",
                        match_name="eu-north",
                    ),
                    "node-class-h100-ib": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="h100-ib",
                    ),
                    "node-class-cpu-d3": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="cpu-d3",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ActivatedDefaults",
        reason=(
            "A ProvisionedCluster whose config and pool leave optional fields unset composes a NebiusCluster that "
            "leaves them unset too."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusClusterConfig", name="eu-west"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="l40s"
                            ),
                            nodeCount=1,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "filesystems.compute.nebius.m.upbound.io",
                            "gpuclusters.compute.nebius.m.upbound.io",
                            "clusters.mk8s.nebius.m.upbound.io",
                            "nodegroups.mk8s.nebius.m.upbound.io",
                            "networks.vpc.nebius.m.upbound.io",
                            "subnets.vpc.nebius.m.upbound.io",
                        ],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "nebius.modelplane.ai/v1alpha1",
                                    "kind": "NebiusClusterConfig",
                                    "metadata": {"name": "eu-west"},
                                    "spec": {},
                                }
                            ),
                        ),
                    ],
                ),
                "node-class-l40s": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l40s",
                            platform="gpu-l40s-a",
                            preset="1gpu-8vcpu-32gb",
                            disk_size_gb=200,
                            accelerator="nvidia-l40s",
                            fabric="None",
                        )
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-nebiusclusterconfig-eu-west": _usage(
                        kind="NebiusClusterConfig",
                        name="eu-west",
                        reason="ProvisionedCluster prod uses this NebiusClusterConfig",
                    ),
                    "usage-nebiusnodeclass-l40s": _usage(
                        kind="NebiusNodeClass",
                        name="l40s",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "nebius-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "nebius.modelplane.ai/v1alpha1",
                                "kind": "NebiusCluster",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "nodePools": [
                                        {
                                            "name": "gpu",
                                            "role": "GPU",
                                            "platform": "gpu-l40s-a",
                                            "preset": "1gpu-8vcpu-32gb",
                                            "diskSizeGb": 200,
                                            "gpu": {"acceleratorType": "nvidia-l40s", "driversPreset": "cuda13.0"},
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
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusClusterConfig",
                        match_name="eu-west",
                    ),
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="l40s",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ClusterReady",
        reason=(
            "Once the NebiusCluster is Ready with a kubeconfig, a ProvisionedCluster composes a NebiusDeviceStack "
            "reaching it through that Secret as the Nebius credentials, and reports both Secrets, the credentials in "
            "their own namespace, and the cache StorageClass."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusClusterConfig", name="eu-north"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="h100-ib"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="cpu-d3"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "filesystems.compute.nebius.m.upbound.io",
                            "gpuclusters.compute.nebius.m.upbound.io",
                            "clusters.mk8s.nebius.m.upbound.io",
                            "nodegroups.mk8s.nebius.m.upbound.io",
                            "networks.vpc.nebius.m.upbound.io",
                            "subnets.vpc.nebius.m.upbound.io",
                        ],
                    ),
                    "nebius-cluster": _observed_cluster(ready=True),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config(infiniband_fabric="fabric-2")]),
                "node-class-h100-ib": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100-ib",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        ),
                    ],
                ),
                "node-class-cpu-d3": fnv1.Resources(
                    items=[
                        _node_class(
                            name="cpu-d3",
                            platform="cpu-d3",
                            preset="16vcpu-64gb",
                            disk_size_gb=100,
                            accelerator=None,
                            fabric="None",
                        ),
                    ],
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
                        {
                            "type": "NebiusServiceAccountCredentials",
                            "name": "nebius-credentials",
                            "namespace": "crossplane-system",
                            "key": "credentials.json",
                        },
                    ],
                    storage_class_name="modelplane-rwx-fs",
                ),
                resources={
                    "usage-nebiusclusterconfig-eu-north": _usage(
                        kind="NebiusClusterConfig",
                        name="eu-north",
                        reason="ProvisionedCluster prod uses this NebiusClusterConfig",
                    ),
                    "usage-nebiusnodeclass-h100-ib": _usage(
                        kind="NebiusNodeClass",
                        name="h100-ib",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "usage-nebiusnodeclass-cpu-d3": _usage(
                        kind="NebiusNodeClass",
                        name="cpu-d3",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "nebius-cluster": _cluster(ready=fnv1.READY_TRUE),
                    "device-stack": _device_stack(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusClusterConfig",
                        match_name="eu-north",
                    ),
                    "node-class-h100-ib": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="h100-ib",
                    ),
                    "node-class-cpu-d3": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="cpu-d3",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="DeviceStackReady",
        reason="Once the NebiusDeviceStack reports Ready, a ProvisionedCluster marks it ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusClusterConfig", name="eu-north"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="h100-ib"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="cpu-d3"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "filesystems.compute.nebius.m.upbound.io",
                            "gpuclusters.compute.nebius.m.upbound.io",
                            "clusters.mk8s.nebius.m.upbound.io",
                            "nodegroups.mk8s.nebius.m.upbound.io",
                            "networks.vpc.nebius.m.upbound.io",
                            "subnets.vpc.nebius.m.upbound.io",
                        ],
                    ),
                    "nebius-cluster": _observed_cluster(ready=True),
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "nebius.modelplane.ai/v1alpha1",
                                "kind": "NebiusDeviceStack",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "secrets": [
                                        {
                                            "type": "Kubeconfig",
                                            "name": "prod-kubeconfig-6c1d0",
                                            "namespace": "modelplane-system",
                                            "key": "kubeconfig",
                                        },
                                        {
                                            "type": "NebiusServiceAccountCredentials",
                                            "name": "nebius-credentials",
                                            "namespace": "crossplane-system",
                                            "key": "credentials.json",
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
                "config": fnv1.Resources(items=[_config(infiniband_fabric="fabric-2")]),
                "node-class-h100-ib": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100-ib",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        ),
                    ],
                ),
                "node-class-cpu-d3": fnv1.Resources(
                    items=[
                        _node_class(
                            name="cpu-d3",
                            platform="cpu-d3",
                            preset="16vcpu-64gb",
                            disk_size_gb=100,
                            accelerator=None,
                            fabric="None",
                        ),
                    ],
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
                        {
                            "type": "NebiusServiceAccountCredentials",
                            "name": "nebius-credentials",
                            "namespace": "crossplane-system",
                            "key": "credentials.json",
                        },
                    ],
                    storage_class_name="modelplane-rwx-fs",
                ),
                resources={
                    "usage-nebiusclusterconfig-eu-north": _usage(
                        kind="NebiusClusterConfig",
                        name="eu-north",
                        reason="ProvisionedCluster prod uses this NebiusClusterConfig",
                    ),
                    "usage-nebiusnodeclass-h100-ib": _usage(
                        kind="NebiusNodeClass",
                        name="h100-ib",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "usage-nebiusnodeclass-cpu-d3": _usage(
                        kind="NebiusNodeClass",
                        name="cpu-d3",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "nebius-cluster": _cluster(ready=fnv1.READY_TRUE),
                    "device-stack": _device_stack(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusClusterConfig",
                        match_name="eu-north",
                    ),
                    "node-class-h100-ib": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="h100-ib",
                    ),
                    "node-class-cpu-d3": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="cpu-d3",
                    ),
                },
            ),
        ),
    ),
    # The observed device stack is still Ready: only the cluster flapped.
    Case(
        name="ClusterReadyRegressed",
        reason=(
            "With the NebiusCluster's Ready condition regressed to False, a ProvisionedCluster stops reporting its "
            "secrets but keeps the NebiusDeviceStack from the device stack's own observed spec."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusClusterConfig", name="eu-north"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="h100-ib"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="nebius.modelplane.ai/v1alpha1", kind="NebiusNodeClass", name="cpu-d3"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "filesystems.compute.nebius.m.upbound.io",
                            "gpuclusters.compute.nebius.m.upbound.io",
                            "clusters.mk8s.nebius.m.upbound.io",
                            "nodegroups.mk8s.nebius.m.upbound.io",
                            "networks.vpc.nebius.m.upbound.io",
                            "subnets.vpc.nebius.m.upbound.io",
                        ],
                    ),
                    "nebius-cluster": _observed_cluster(ready=False),
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "nebius.modelplane.ai/v1alpha1",
                                "kind": "NebiusDeviceStack",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "secrets": [
                                        {
                                            "type": "Kubeconfig",
                                            "name": "prod-kubeconfig-6c1d0",
                                            "namespace": "modelplane-system",
                                            "key": "kubeconfig",
                                        },
                                        {
                                            "type": "NebiusServiceAccountCredentials",
                                            "name": "nebius-credentials",
                                            "namespace": "crossplane-system",
                                            "key": "credentials.json",
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
                "config": fnv1.Resources(items=[_config(infiniband_fabric="fabric-2")]),
                "node-class-h100-ib": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100-ib",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        ),
                    ],
                ),
                "node-class-cpu-d3": fnv1.Resources(
                    items=[
                        _node_class(
                            name="cpu-d3",
                            platform="cpu-d3",
                            preset="16vcpu-64gb",
                            disk_size_gb=100,
                            accelerator=None,
                            fabric="None",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name="modelplane-rwx-fs"),
                resources={
                    "usage-nebiusclusterconfig-eu-north": _usage(
                        kind="NebiusClusterConfig",
                        name="eu-north",
                        reason="ProvisionedCluster prod uses this NebiusClusterConfig",
                    ),
                    "usage-nebiusnodeclass-h100-ib": _usage(
                        kind="NebiusNodeClass",
                        name="h100-ib",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "usage-nebiusnodeclass-cpu-d3": _usage(
                        kind="NebiusNodeClass",
                        name="cpu-d3",
                        reason="ProvisionedCluster prod uses this NebiusNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "nebius-cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                    "device-stack": _device_stack(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusClusterConfig",
                        match_name="eu-north",
                    ),
                    "node-class-h100-ib": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="h100-ib",
                    ),
                    "node-class-cpu-d3": fnv1.ResourceSelector(
                        api_version="nebius.modelplane.ai/v1alpha1",
                        kind="NebiusNodeClass",
                        match_name="cpu-d3",
                    ),
                },
            ),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction provisions a Nebius mk8s cluster for a ProvisionedCluster."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
