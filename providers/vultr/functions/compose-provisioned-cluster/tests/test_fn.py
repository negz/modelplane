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
from models.ai.modelplane.vultr.vultrclusterconfig import v1alpha1 as configv1alpha1
from models.ai.modelplane.vultr.vultrnodeclass import v1alpha1 as ncv1alpha1
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


def _desired_xr(*, secrets: list[dict[str, str]] | None) -> fnv1.Resource:
    """The desired ProvisionedCluster, reporting secrets unless they're None."""
    status: dict = {}
    if secrets is not None:
        status["secrets"] = secrets
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}))


def _config() -> fnv1.Resource:
    """The ewr VultrClusterConfig, as Crossplane fetches it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vultr.modelplane.ai/v1alpha1",
                "kind": "VultrClusterConfig",
                "metadata": {"name": "ewr"},
                "spec": {
                    "region": "ewr",
                    "kubernetesVersion": "v1.36.6+1",
                    "credentials": {"type": "ProviderConfig", "name": "my-vultr-account"},
                },
            }
        ),
    )


def _node_class(*, name: str, plan: str, accelerator: str | None) -> fnv1.Resource:
    """A VultrNodeClass as Crossplane fetches it, naming an accelerator unless it's None."""
    spec: dict = {"plan": plan}
    if accelerator is not None:
        spec["accelerator"] = {"type": accelerator}
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vultr.modelplane.ai/v1alpha1",
                "kind": "VultrNodeClass",
                "metadata": {"name": name},
                "spec": spec,
            }
        ),
    )


def _usage(*, kind: str, name: str, reason: str) -> fnv1.Resource:
    """A composed ClusterUsage blocking deletion of a Vultr object the ProvisionedCluster references."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "ClusterUsage",
                "spec": {
                    "of": {
                        "apiVersion": "vultr.modelplane.ai/v1alpha1",
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
    """The composed ManagedResourceActivationPolicy activating the kinds compose-vultr-cluster composes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "apiextensions.crossplane.io/v1alpha1",
                "kind": "ManagedResourceActivationPolicy",
                "spec": {
                    "activate": [
                        "kubernetes.vke.vultr.m.upbound.io",
                        "kubernetesnodepools.vke.vultr.m.upbound.io",
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
                        "kubernetes.vke.vultr.m.upbound.io",
                        "kubernetesnodepools.vke.vultr.m.upbound.io",
                    ],
                },
                "status": {"activated": activated},
            }
        ),
    )


def _cluster(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed VultrCluster, with a GPU pool and a CPU pool built from their node classes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vultr.modelplane.ai/v1alpha1",
                "kind": "VultrCluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "region": "ewr",
                    "kubernetesVersion": "v1.36.6+1",
                    "credentials": {"type": "ProviderConfig", "name": "my-vultr-account"},
                    "nodePools": [
                        {
                            "name": "gpu",
                            "role": "GPU",
                            "plan": "vcg-l40s-16c-180g-48vram",
                            "gpu": {"acceleratorType": "nvidia-l40s"},
                            "nodeCount": 2,
                            "maxNodeCount": 4,
                        },
                        {
                            "name": "cpu",
                            "role": "CPU",
                            "plan": "vc2-6c-16gb",
                            "nodeCount": 2,
                        },
                    ],
                },
            }
        ),
        ready=ready,
    )


def _observed_cluster(*, ready: bool) -> fnv1.Resource:
    """The VultrCluster as observed, reporting its kubeconfig Secret, with a Ready condition that's True if ready and False if not."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vultr.modelplane.ai/v1alpha1",
                "kind": "VultrCluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "region": "ewr",
                    "kubernetesVersion": "v1.36.6+1",
                    "credentials": {"type": "ProviderConfig", "name": "my-vultr-account"},
                    "nodePools": [
                        {
                            "name": "gpu",
                            "role": "GPU",
                            "plan": "vcg-l40s-16c-180g-48vram",
                            "gpu": {"acceleratorType": "nvidia-l40s"},
                            "nodeCount": 2,
                            "maxNodeCount": 4,
                        },
                        {
                            "name": "cpu",
                            "role": "CPU",
                            "plan": "vc2-6c-16gb",
                            "nodeCount": 2,
                        },
                    ],
                },
                "status": {
                    "secrets": [{"type": "Kubeconfig", "name": "prod-kubeconfig-6c1d0", "key": "kubeconfig"}],
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
    """The composed VultrDeviceStack, reaching the cluster through its kubeconfig Secret."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vultr.modelplane.ai/v1alpha1",
                "kind": "VultrDeviceStack",
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
        reason="A VultrClusterConfig XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        configv1alpha1.VultrClusterConfig(
                            metadata=metav1.ObjectMeta(name="ewr"),
                            spec=configv1alpha1.Spec(region="ewr", kubernetesVersion="v1.36.6+1"),
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
        reason="A VultrNodeClass XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        ncv1alpha1.VultrNodeClass(
                            metadata=metav1.ObjectMeta(name="l40s"),
                            spec=ncv1alpha1.Spec(
                                plan="vcg-l40s-16c-180g-48vram",
                                accelerator=ncv1alpha1.Accelerator(type="nvidia-l40s"),
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
                        apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrClusterConfig", name="ewr"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="l40s"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="vc2"
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
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrClusterConfig",
                        match_name="ewr",
                    ),
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="l40s",
                    ),
                    "node-class-vc2": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="vc2",
                    ),
                },
            ),
        ),
    ),
    # The cpu pool was added since Crossplane last fetched the required
    # resources, so it hasn't fetched the vc2 node class yet.
    Case(
        name="NodeClassNotFetched",
        reason="A node class Crossplane hasn't fetched yet holds back composing without failing the reconcile.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrClusterConfig", name="ewr"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="l40s"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="vc2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l40s": fnv1.Resources(
                    items=[_node_class(name="l40s", plan="vcg-l40s-16c-180g-48vram", accelerator="nvidia-l40s")],
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
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrClusterConfig",
                        match_name="ewr",
                    ),
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="l40s",
                    ),
                    "node-class-vc2": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="vc2",
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
                        apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrClusterConfig", name="ewr"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="l40s"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="vc2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l40s": fnv1.Resources(
                    items=[_node_class(name="l40s", plan="vcg-l40s-16c-180g-48vram", accelerator="nvidia-l40s")],
                ),
                "node-class-vc2": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_FATAL, message="Missing VultrNodeClass vc2"),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrClusterConfig",
                        match_name="ewr",
                    ),
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="l40s",
                    ),
                    "node-class-vc2": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="vc2",
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l40s"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="vc2"
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
                        "VultrClusterConfig.vultr.modelplane.ai; node pool gpu's nodeClassRef is a "
                        "GKENodeClass.gke.modelplane.ai, not a VultrNodeClass.vultr.modelplane.ai"
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
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l40s",
                    ),
                    "node-class-vc2": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="vc2",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="Reservation",
        reason="A node pool with a reservationRef fails the reconcile, because Vultr has no capacity reservations.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrClusterConfig", name="ewr"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="l40s"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="gke.modelplane.ai/v1alpha1",
                                kind="GKECapacityReservation",
                                name="l40s-reserved",
                            ),
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="vc2"
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
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrClusterConfig",
                        match_name="ewr",
                    ),
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="l40s",
                    ),
                    "node-class-vc2": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="vc2",
                    ),
                    "reservation-l40s-reserved": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKECapacityReservation",
                        match_name="l40s-reserved",
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
                        apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrClusterConfig", name="ewr"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="vc2"
                            ),
                            nodeCount=1,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="l40s"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l40s": fnv1.Resources(
                    items=[_node_class(name="l40s", plan="vcg-l40s-16c-180g-48vram", accelerator="nvidia-l40s")],
                ),
                "node-class-vc2": fnv1.Resources(
                    items=[_node_class(name="vc2", plan="vc2-6c-16gb", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-vultrclusterconfig-ewr": _usage(
                        kind="VultrClusterConfig",
                        name="ewr",
                        reason="ProvisionedCluster prod uses this VultrClusterConfig",
                    ),
                    "usage-vultrnodeclass-l40s": _usage(
                        kind="VultrNodeClass",
                        name="l40s",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                    "usage-vultrnodeclass-vc2": _usage(
                        kind="VultrNodeClass",
                        name="vc2",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message=(
                        "node pool gpu runs accelerators, but VultrNodeClass vc2 names no accelerator; "
                        "node pool cpu is a CPU pool, but VultrNodeClass l40s names an accelerator"
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrClusterConfig",
                        match_name="ewr",
                    ),
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="l40s",
                    ),
                    "node-class-vc2": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="vc2",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="NotActivated",
        reason=(
            "Until the activation policy reports every kind active, a ProvisionedCluster composes the policy and "
            "ClusterUsages but no VultrCluster."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrClusterConfig", name="ewr"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="l40s"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="vc2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(activated=["kubernetes.vke.vultr.m.upbound.io"]),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l40s": fnv1.Resources(
                    items=[_node_class(name="l40s", plan="vcg-l40s-16c-180g-48vram", accelerator="nvidia-l40s")],
                ),
                "node-class-vc2": fnv1.Resources(
                    items=[_node_class(name="vc2", plan="vc2-6c-16gb", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None),
                resources={
                    "usage-vultrclusterconfig-ewr": _usage(
                        kind="VultrClusterConfig",
                        name="ewr",
                        reason="ProvisionedCluster prod uses this VultrClusterConfig",
                    ),
                    "usage-vultrnodeclass-l40s": _usage(
                        kind="VultrNodeClass",
                        name="l40s",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                    "usage-vultrnodeclass-vc2": _usage(
                        kind="VultrNodeClass",
                        name="vc2",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrClusterConfig",
                        match_name="ewr",
                    ),
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="l40s",
                    ),
                    "node-class-vc2": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="vc2",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="Activated",
        reason=(
            "Once the activation policy reports every kind active, a ProvisionedCluster composes a VultrCluster "
            "with a GPU pool and a CPU pool from their node classes, and the region, version and credentials from its config."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrClusterConfig", name="ewr"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="l40s"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="vc2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=["kubernetes.vke.vultr.m.upbound.io", "kubernetesnodepools.vke.vultr.m.upbound.io"],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l40s": fnv1.Resources(
                    items=[_node_class(name="l40s", plan="vcg-l40s-16c-180g-48vram", accelerator="nvidia-l40s")],
                ),
                "node-class-vc2": fnv1.Resources(
                    items=[_node_class(name="vc2", plan="vc2-6c-16gb", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None),
                resources={
                    "usage-vultrclusterconfig-ewr": _usage(
                        kind="VultrClusterConfig",
                        name="ewr",
                        reason="ProvisionedCluster prod uses this VultrClusterConfig",
                    ),
                    "usage-vultrnodeclass-l40s": _usage(
                        kind="VultrNodeClass",
                        name="l40s",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                    "usage-vultrnodeclass-vc2": _usage(
                        kind="VultrNodeClass",
                        name="vc2",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "vultr-cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrClusterConfig",
                        match_name="ewr",
                    ),
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="l40s",
                    ),
                    "node-class-vc2": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="vc2",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="DefaultCredentials",
        reason=(
            "A VultrClusterConfig without credentials composes a VultrCluster without them, so the VultrCluster's "
            "default ClusterProviderConfig applies."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrClusterConfig", name="ewr"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="l40s"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=["kubernetes.vke.vultr.m.upbound.io", "kubernetesnodepools.vke.vultr.m.upbound.io"],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "vultr.modelplane.ai/v1alpha1",
                                    "kind": "VultrClusterConfig",
                                    "metadata": {"name": "ewr"},
                                    "spec": {"region": "ewr", "kubernetesVersion": "v1.36.6+1"},
                                }
                            ),
                        ),
                    ],
                ),
                "node-class-l40s": fnv1.Resources(
                    items=[_node_class(name="l40s", plan="vcg-l40s-16c-180g-48vram", accelerator="nvidia-l40s")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None),
                resources={
                    "usage-vultrclusterconfig-ewr": _usage(
                        kind="VultrClusterConfig",
                        name="ewr",
                        reason="ProvisionedCluster prod uses this VultrClusterConfig",
                    ),
                    "usage-vultrnodeclass-l40s": _usage(
                        kind="VultrNodeClass",
                        name="l40s",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "vultr-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "vultr.modelplane.ai/v1alpha1",
                                "kind": "VultrCluster",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "region": "ewr",
                                    "kubernetesVersion": "v1.36.6+1",
                                    "nodePools": [
                                        {
                                            "name": "gpu",
                                            "role": "GPU",
                                            "plan": "vcg-l40s-16c-180g-48vram",
                                            "gpu": {"acceleratorType": "nvidia-l40s"},
                                            "nodeCount": 2,
                                            "maxNodeCount": 4,
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
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrClusterConfig",
                        match_name="ewr",
                    ),
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="l40s",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ClusterReady",
        reason=(
            "Once the VultrCluster is Ready with a kubeconfig, a ProvisionedCluster composes a "
            "VultrDeviceStack reaching it through that Secret, and reports the Secret."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrClusterConfig", name="ewr"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="l40s"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="vc2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=["kubernetes.vke.vultr.m.upbound.io", "kubernetesnodepools.vke.vultr.m.upbound.io"],
                    ),
                    "vultr-cluster": _observed_cluster(ready=True),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l40s": fnv1.Resources(
                    items=[_node_class(name="l40s", plan="vcg-l40s-16c-180g-48vram", accelerator="nvidia-l40s")],
                ),
                "node-class-vc2": fnv1.Resources(
                    items=[_node_class(name="vc2", plan="vc2-6c-16gb", accelerator=None)],
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
                ),
                resources={
                    "usage-vultrclusterconfig-ewr": _usage(
                        kind="VultrClusterConfig",
                        name="ewr",
                        reason="ProvisionedCluster prod uses this VultrClusterConfig",
                    ),
                    "usage-vultrnodeclass-l40s": _usage(
                        kind="VultrNodeClass",
                        name="l40s",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                    "usage-vultrnodeclass-vc2": _usage(
                        kind="VultrNodeClass",
                        name="vc2",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "vultr-cluster": _cluster(ready=fnv1.READY_TRUE),
                    "device-stack": _device_stack(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrClusterConfig",
                        match_name="ewr",
                    ),
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="l40s",
                    ),
                    "node-class-vc2": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="vc2",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="DeviceStackReady",
        reason="Once the VultrDeviceStack reports Ready, a ProvisionedCluster marks it ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrClusterConfig", name="ewr"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="l40s"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="vc2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=["kubernetes.vke.vultr.m.upbound.io", "kubernetesnodepools.vke.vultr.m.upbound.io"],
                    ),
                    "vultr-cluster": _observed_cluster(ready=True),
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "vultr.modelplane.ai/v1alpha1",
                                "kind": "VultrDeviceStack",
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
                "node-class-l40s": fnv1.Resources(
                    items=[_node_class(name="l40s", plan="vcg-l40s-16c-180g-48vram", accelerator="nvidia-l40s")],
                ),
                "node-class-vc2": fnv1.Resources(
                    items=[_node_class(name="vc2", plan="vc2-6c-16gb", accelerator=None)],
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
                ),
                resources={
                    "usage-vultrclusterconfig-ewr": _usage(
                        kind="VultrClusterConfig",
                        name="ewr",
                        reason="ProvisionedCluster prod uses this VultrClusterConfig",
                    ),
                    "usage-vultrnodeclass-l40s": _usage(
                        kind="VultrNodeClass",
                        name="l40s",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                    "usage-vultrnodeclass-vc2": _usage(
                        kind="VultrNodeClass",
                        name="vc2",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "vultr-cluster": _cluster(ready=fnv1.READY_TRUE),
                    "device-stack": _device_stack(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrClusterConfig",
                        match_name="ewr",
                    ),
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="l40s",
                    ),
                    "node-class-vc2": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="vc2",
                    ),
                },
            ),
        ),
    ),
    # The observed device stack is still Ready: only the cluster flapped.
    Case(
        name="ClusterReadyRegressed",
        reason=(
            "With the VultrCluster's Ready condition regressed to False, a ProvisionedCluster stops reporting its "
            "secrets but keeps the VultrDeviceStack from the device stack's own observed spec."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrClusterConfig", name="ewr"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="l40s"
                            ),
                            nodeCount=2,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="vultr.modelplane.ai/v1alpha1", kind="VultrNodeClass", name="vc2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=["kubernetes.vke.vultr.m.upbound.io", "kubernetesnodepools.vke.vultr.m.upbound.io"],
                    ),
                    "vultr-cluster": _observed_cluster(ready=False),
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "vultr.modelplane.ai/v1alpha1",
                                "kind": "VultrDeviceStack",
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
                "node-class-l40s": fnv1.Resources(
                    items=[_node_class(name="l40s", plan="vcg-l40s-16c-180g-48vram", accelerator="nvidia-l40s")],
                ),
                "node-class-vc2": fnv1.Resources(
                    items=[_node_class(name="vc2", plan="vc2-6c-16gb", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None),
                resources={
                    "usage-vultrclusterconfig-ewr": _usage(
                        kind="VultrClusterConfig",
                        name="ewr",
                        reason="ProvisionedCluster prod uses this VultrClusterConfig",
                    ),
                    "usage-vultrnodeclass-l40s": _usage(
                        kind="VultrNodeClass",
                        name="l40s",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                    "usage-vultrnodeclass-vc2": _usage(
                        kind="VultrNodeClass",
                        name="vc2",
                        reason="ProvisionedCluster prod uses this VultrNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "vultr-cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                    "device-stack": _device_stack(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrClusterConfig",
                        match_name="ewr",
                    ),
                    "node-class-l40s": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="l40s",
                    ),
                    "node-class-vc2": fnv1.ResourceSelector(
                        api_version="vultr.modelplane.ai/v1alpha1",
                        kind="VultrNodeClass",
                        match_name="vc2",
                    ),
                },
            ),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction provisions a VKE cluster for a ProvisionedCluster."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
