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
from models.ai.modelplane.aks.aksclusterconfig import v1alpha1 as configv1alpha1
from models.ai.modelplane.aks.aksnodeclass import v1alpha1 as ncv1alpha1
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
    """The desired ProvisionedCluster, reporting secrets and cache unless they're None."""
    status: dict = {}
    if secrets is not None:
        status["secrets"] = secrets
    if storage_class_name is not None:
        status["cache"] = {"storageClassName": storage_class_name}
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}))


def _config() -> fnv1.Resource:
    """The westeurope AKSClusterConfig, as Crossplane fetches it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "aks.modelplane.ai/v1alpha1",
                "kind": "AKSClusterConfig",
                "metadata": {"name": "westeurope"},
                "spec": {
                    "location": "westeurope",
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ClusterProviderConfig", "name": "azure"},
                },
            }
        ),
    )


def _node_class(*, name: str, vm_size: str, disk_size_gb: int, accelerator: str | None, fabric: str) -> fnv1.Resource:
    """An AKSNodeClass as Crossplane fetches it, naming an accelerator unless it's None."""
    spec: dict = {"vmSize": vm_size, "diskSizeGb": disk_size_gb, "fabric": fabric}
    if accelerator is not None:
        spec["accelerator"] = {"type": accelerator}
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "aks.modelplane.ai/v1alpha1",
                "kind": "AKSNodeClass",
                "metadata": {"name": name},
                "spec": spec,
            }
        ),
    )


def _usage(*, kind: str, name: str, reason: str) -> fnv1.Resource:
    """A composed ClusterUsage blocking deletion of an AKS object the ProvisionedCluster references."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "ClusterUsage",
                "spec": {
                    "of": {
                        "apiVersion": "aks.modelplane.ai/v1alpha1",
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
    """The composed ManagedResourceActivationPolicy activating the kinds compose-aks-cluster composes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "apiextensions.crossplane.io/v1alpha1",
                "kind": "ManagedResourceActivationPolicy",
                "spec": {
                    "activate": [
                        "kubernetesclusters.containerservice.azure.m.upbound.io",
                        "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
                        "subnets.network.azure.m.upbound.io",
                        "virtualnetworks.network.azure.m.upbound.io",
                        "resourcegroups.azure.m.upbound.io",
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
                        "kubernetesclusters.containerservice.azure.m.upbound.io",
                        "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
                        "subnets.network.azure.m.upbound.io",
                        "virtualnetworks.network.azure.m.upbound.io",
                        "resourcegroups.azure.m.upbound.io",
                    ],
                },
                "status": {"activated": activated},
            }
        ),
    )


def _cluster(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed AKSCluster, with an InfiniBand GPU pool in zone 1 and a CPU pool built from their node classes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "aks.modelplane.ai/v1alpha1",
                "kind": "AKSCluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "location": "westeurope",
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ClusterProviderConfig", "name": "azure"},
                    "nodePools": [
                        {
                            "name": "gpu",
                            "role": "GPU",
                            "vmSize": "Standard_ND96isr_H100_v5",
                            "diskSizeGb": 200,
                            "gpu": {"acceleratorType": "nvidia-h100"},
                            "nodeCount": 1,
                            "minNodeCount": 1,
                            "maxNodeCount": 4,
                            "zones": ["1"],
                            "fabric": "InfiniBand",
                        },
                        {
                            "name": "cpu",
                            "role": "CPU",
                            "vmSize": "Standard_D16s_v5",
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
    """The AKSCluster as observed, reporting its kubeconfig Secret and cache StorageClass, with a Ready condition that's True if ready and False if not."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "aks.modelplane.ai/v1alpha1",
                "kind": "AKSCluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "location": "westeurope",
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ClusterProviderConfig", "name": "azure"},
                    "nodePools": [
                        {
                            "name": "gpu",
                            "role": "GPU",
                            "vmSize": "Standard_ND96isr_H100_v5",
                            "diskSizeGb": 200,
                            "gpu": {"acceleratorType": "nvidia-h100"},
                            "nodeCount": 1,
                            "minNodeCount": 1,
                            "maxNodeCount": 4,
                            "zones": ["1"],
                            "fabric": "InfiniBand",
                        },
                        {
                            "name": "cpu",
                            "role": "CPU",
                            "vmSize": "Standard_D16s_v5",
                            "diskSizeGb": 100,
                            "nodeCount": 2,
                            "minNodeCount": 0,
                            "maxNodeCount": 8,
                            "fabric": "None",
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
    """The composed AKSDeviceStack, reaching the cluster through its kubeconfig Secret."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "aks.modelplane.ai/v1alpha1",
                "kind": "AKSDeviceStack",
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
        reason="An AKSClusterConfig XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        configv1alpha1.AKSClusterConfig(
                            metadata=metav1.ObjectMeta(name="westeurope"),
                            spec=configv1alpha1.Spec(location="westeurope"),
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
        reason="An AKSNodeClass XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        ncv1alpha1.AKSNodeClass(
                            metadata=metav1.ObjectMeta(name="h100"),
                            spec=ncv1alpha1.Spec(
                                vmSize="Standard_ND96isr_H100_v5",
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
                        apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSClusterConfig", name="westeurope"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("1")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="d16"
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
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSClusterConfig",
                        match_name="westeurope",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-d16": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="d16",
                    ),
                },
            ),
        ),
    ),
    # The cpu pool was added since Crossplane last fetched the required
    # resources, so it hasn't fetched the d16 node class yet.
    Case(
        name="NodeClassNotFetched",
        reason="A node class Crossplane hasn't fetched yet holds back composing without failing the reconcile.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSClusterConfig", name="westeurope"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("1")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="d16"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100",
                            vm_size="Standard_ND96isr_H100_v5",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        )
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
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSClusterConfig",
                        match_name="westeurope",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-d16": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="d16",
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
                        apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSClusterConfig", name="westeurope"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("1")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="d16"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100",
                            vm_size="Standard_ND96isr_H100_v5",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        )
                    ],
                ),
                "node-class-d16": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_FATAL, message="Missing AKSNodeClass d16"),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSClusterConfig",
                        match_name="westeurope",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-d16": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="d16",
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
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="d16"
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
                        "AKSClusterConfig.aks.modelplane.ai; node pool gpu's nodeClassRef is a "
                        "GKENodeClass.gke.modelplane.ai, not a AKSNodeClass.aks.modelplane.ai"
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
                    "node-class-d16": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="d16",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="Reservation",
        reason="A node pool with a reservationRef fails the reconcile, because AKS has no capacity reservations.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSClusterConfig", name="westeurope"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("1")],
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
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="d16"
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
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSClusterConfig",
                        match_name="westeurope",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-d16": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="d16",
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
                        apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSClusterConfig", name="westeurope"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="d16"
                            ),
                            nodeCount=1,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="h100"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100",
                            vm_size="Standard_ND96isr_H100_v5",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        )
                    ],
                ),
                "node-class-d16": fnv1.Resources(
                    items=[
                        _node_class(
                            name="d16", vm_size="Standard_D16s_v5", disk_size_gb=100, accelerator=None, fabric="None"
                        )
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-aksclusterconfig-westeurope": _usage(
                        kind="AKSClusterConfig",
                        name="westeurope",
                        reason="ProvisionedCluster prod uses this AKSClusterConfig",
                    ),
                    "usage-aksnodeclass-h100": _usage(
                        kind="AKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                    "usage-aksnodeclass-d16": _usage(
                        kind="AKSNodeClass",
                        name="d16",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message=(
                        "node pool gpu runs accelerators, but AKSNodeClass d16 names no accelerator; "
                        "node pool cpu is a CPU pool, but AKSNodeClass h100 names an accelerator"
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSClusterConfig",
                        match_name="westeurope",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-d16": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="d16",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="NotActivated",
        reason=(
            "Until the activation policy reports every kind active, a ProvisionedCluster composes the policy and "
            "ClusterUsages but no AKSCluster, and reports nothing."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSClusterConfig", name="westeurope"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("1")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="d16"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "kubernetesclusters.containerservice.azure.m.upbound.io",
                            "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
                            "subnets.network.azure.m.upbound.io",
                            "virtualnetworks.network.azure.m.upbound.io",
                        ],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100",
                            vm_size="Standard_ND96isr_H100_v5",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        )
                    ],
                ),
                "node-class-d16": fnv1.Resources(
                    items=[
                        _node_class(
                            name="d16", vm_size="Standard_D16s_v5", disk_size_gb=100, accelerator=None, fabric="None"
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
                    "usage-aksclusterconfig-westeurope": _usage(
                        kind="AKSClusterConfig",
                        name="westeurope",
                        reason="ProvisionedCluster prod uses this AKSClusterConfig",
                    ),
                    "usage-aksnodeclass-h100": _usage(
                        kind="AKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                    "usage-aksnodeclass-d16": _usage(
                        kind="AKSNodeClass",
                        name="d16",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSClusterConfig",
                        match_name="westeurope",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-d16": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="d16",
                    ),
                },
            ),
        ),
    ),
    # The GPU pool's node class puts it on InfiniBand, which reaches the
    # AKSCluster as the pool's fabric, and the pool is restricted to a zone.
    # The CPU pool's class leaves fabric at None, which the AKSCluster pool
    # leaves unset.
    Case(
        name="Activated",
        reason=(
            "Once the activation policy reports every kind active, a ProvisionedCluster composes an AKSCluster with "
            "an InfiniBand GPU pool in its zones and a CPU pool from their node classes, and the version and "
            "credentials from its config."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSClusterConfig", name="westeurope"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("1")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="d16"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "kubernetesclusters.containerservice.azure.m.upbound.io",
                            "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
                            "subnets.network.azure.m.upbound.io",
                            "virtualnetworks.network.azure.m.upbound.io",
                            "resourcegroups.azure.m.upbound.io",
                        ],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100",
                            vm_size="Standard_ND96isr_H100_v5",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        )
                    ],
                ),
                "node-class-d16": fnv1.Resources(
                    items=[
                        _node_class(
                            name="d16", vm_size="Standard_D16s_v5", disk_size_gb=100, accelerator=None, fabric="None"
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
                    "usage-aksclusterconfig-westeurope": _usage(
                        kind="AKSClusterConfig",
                        name="westeurope",
                        reason="ProvisionedCluster prod uses this AKSClusterConfig",
                    ),
                    "usage-aksnodeclass-h100": _usage(
                        kind="AKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                    "usage-aksnodeclass-d16": _usage(
                        kind="AKSNodeClass",
                        name="d16",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "aks-cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSClusterConfig",
                        match_name="westeurope",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-d16": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="d16",
                    ),
                },
            ),
        ),
    ),
    # The config names no version or credentials, and the pool no zones or
    # autoscaling bounds, so the AKSCluster's own defaults apply to each.
    Case(
        name="ActivatedDefaults",
        reason=(
            "A ProvisionedCluster whose config and pool leave optional fields unset composes an AKSCluster that "
            "leaves them unset too."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSClusterConfig", name="eastus2"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="a100"
                            ),
                            nodeCount=1,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "kubernetesclusters.containerservice.azure.m.upbound.io",
                            "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
                            "subnets.network.azure.m.upbound.io",
                            "virtualnetworks.network.azure.m.upbound.io",
                            "resourcegroups.azure.m.upbound.io",
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
                                    "apiVersion": "aks.modelplane.ai/v1alpha1",
                                    "kind": "AKSClusterConfig",
                                    "metadata": {"name": "eastus2"},
                                    "spec": {"location": "eastus2"},
                                }
                            ),
                        ),
                    ],
                ),
                "node-class-a100": fnv1.Resources(
                    items=[
                        _node_class(
                            name="a100",
                            vm_size="Standard_NC24ads_A100_v4",
                            disk_size_gb=100,
                            accelerator="nvidia-a100",
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
                    "usage-aksclusterconfig-eastus2": _usage(
                        kind="AKSClusterConfig",
                        name="eastus2",
                        reason="ProvisionedCluster prod uses this AKSClusterConfig",
                    ),
                    "usage-aksnodeclass-a100": _usage(
                        kind="AKSNodeClass",
                        name="a100",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "aks-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "aks.modelplane.ai/v1alpha1",
                                "kind": "AKSCluster",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "location": "eastus2",
                                    "nodePools": [
                                        {
                                            "name": "gpu",
                                            "role": "GPU",
                                            "vmSize": "Standard_NC24ads_A100_v4",
                                            "diskSizeGb": 100,
                                            "gpu": {"acceleratorType": "nvidia-a100"},
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
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSClusterConfig",
                        match_name="eastus2",
                    ),
                    "node-class-a100": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="a100",
                    ),
                },
            ),
        ),
    ),
    # The kubeconfig embeds a client certificate, so the cluster reports no
    # identity secret.
    Case(
        name="ClusterReady",
        reason=(
            "Once the AKSCluster is Ready with a kubeconfig, a ProvisionedCluster composes an AKSDeviceStack "
            "reaching it through that Secret, and reports the Secret and the cache StorageClass."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSClusterConfig", name="westeurope"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("1")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="d16"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "kubernetesclusters.containerservice.azure.m.upbound.io",
                            "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
                            "subnets.network.azure.m.upbound.io",
                            "virtualnetworks.network.azure.m.upbound.io",
                            "resourcegroups.azure.m.upbound.io",
                        ],
                    ),
                    "aks-cluster": _observed_cluster(ready=True),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100",
                            vm_size="Standard_ND96isr_H100_v5",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        )
                    ],
                ),
                "node-class-d16": fnv1.Resources(
                    items=[
                        _node_class(
                            name="d16", vm_size="Standard_D16s_v5", disk_size_gb=100, accelerator=None, fabric="None"
                        )
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
                    ],
                    storage_class_name="modelplane-rwx-fs",
                ),
                resources={
                    "usage-aksclusterconfig-westeurope": _usage(
                        kind="AKSClusterConfig",
                        name="westeurope",
                        reason="ProvisionedCluster prod uses this AKSClusterConfig",
                    ),
                    "usage-aksnodeclass-h100": _usage(
                        kind="AKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                    "usage-aksnodeclass-d16": _usage(
                        kind="AKSNodeClass",
                        name="d16",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "aks-cluster": _cluster(ready=fnv1.READY_TRUE),
                    "device-stack": _device_stack(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSClusterConfig",
                        match_name="westeurope",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-d16": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="d16",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="DeviceStackReady",
        reason="Once the AKSDeviceStack reports Ready, a ProvisionedCluster marks it ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSClusterConfig", name="westeurope"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("1")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="d16"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "kubernetesclusters.containerservice.azure.m.upbound.io",
                            "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
                            "subnets.network.azure.m.upbound.io",
                            "virtualnetworks.network.azure.m.upbound.io",
                            "resourcegroups.azure.m.upbound.io",
                        ],
                    ),
                    "aks-cluster": _observed_cluster(ready=True),
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "aks.modelplane.ai/v1alpha1",
                                "kind": "AKSDeviceStack",
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
                    items=[
                        _node_class(
                            name="h100",
                            vm_size="Standard_ND96isr_H100_v5",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        )
                    ],
                ),
                "node-class-d16": fnv1.Resources(
                    items=[
                        _node_class(
                            name="d16", vm_size="Standard_D16s_v5", disk_size_gb=100, accelerator=None, fabric="None"
                        )
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
                    ],
                    storage_class_name="modelplane-rwx-fs",
                ),
                resources={
                    "usage-aksclusterconfig-westeurope": _usage(
                        kind="AKSClusterConfig",
                        name="westeurope",
                        reason="ProvisionedCluster prod uses this AKSClusterConfig",
                    ),
                    "usage-aksnodeclass-h100": _usage(
                        kind="AKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                    "usage-aksnodeclass-d16": _usage(
                        kind="AKSNodeClass",
                        name="d16",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "aks-cluster": _cluster(ready=fnv1.READY_TRUE),
                    "device-stack": _device_stack(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSClusterConfig",
                        match_name="westeurope",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-d16": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="d16",
                    ),
                },
            ),
        ),
    ),
    # The observed device stack is still Ready: only the cluster flapped.
    Case(
        name="ClusterReadyRegressed",
        reason=(
            "With the AKSCluster's Ready condition regressed to False, a ProvisionedCluster stops reporting its "
            "secrets but keeps the AKSDeviceStack from the device stack's own observed spec."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSClusterConfig", name="westeurope"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("1")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="aks.modelplane.ai/v1alpha1", kind="AKSNodeClass", name="d16"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "kubernetesclusters.containerservice.azure.m.upbound.io",
                            "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
                            "subnets.network.azure.m.upbound.io",
                            "virtualnetworks.network.azure.m.upbound.io",
                            "resourcegroups.azure.m.upbound.io",
                        ],
                    ),
                    "aks-cluster": _observed_cluster(ready=False),
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "aks.modelplane.ai/v1alpha1",
                                "kind": "AKSDeviceStack",
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
                    items=[
                        _node_class(
                            name="h100",
                            vm_size="Standard_ND96isr_H100_v5",
                            disk_size_gb=200,
                            accelerator="nvidia-h100",
                            fabric="InfiniBand",
                        )
                    ],
                ),
                "node-class-d16": fnv1.Resources(
                    items=[
                        _node_class(
                            name="d16", vm_size="Standard_D16s_v5", disk_size_gb=100, accelerator=None, fabric="None"
                        )
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name="modelplane-rwx-fs"),
                resources={
                    "usage-aksclusterconfig-westeurope": _usage(
                        kind="AKSClusterConfig",
                        name="westeurope",
                        reason="ProvisionedCluster prod uses this AKSClusterConfig",
                    ),
                    "usage-aksnodeclass-h100": _usage(
                        kind="AKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                    "usage-aksnodeclass-d16": _usage(
                        kind="AKSNodeClass",
                        name="d16",
                        reason="ProvisionedCluster prod uses this AKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "aks-cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                    "device-stack": _device_stack(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSClusterConfig",
                        match_name="westeurope",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-d16": fnv1.ResourceSelector(
                        api_version="aks.modelplane.ai/v1alpha1",
                        kind="AKSNodeClass",
                        match_name="d16",
                    ),
                },
            ),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction provisions an AKS cluster for a ProvisionedCluster."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
