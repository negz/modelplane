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
from models.ai.modelplane.gke.gkecapacityreservation import v1alpha1 as crv1alpha1
from models.ai.modelplane.gke.gkeclusterconfig import v1alpha1 as configv1alpha1
from models.ai.modelplane.gke.gkenodeclass import v1alpha1 as ncv1alpha1
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
    """The us-central GKEClusterConfig, as Crossplane fetches it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "gke.modelplane.ai/v1alpha1",
                "kind": "GKEClusterConfig",
                "metadata": {"name": "us-central"},
                "spec": {
                    "region": "us-central1",
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ProviderConfig", "name": "my-gcp-account"},
                },
            }
        ),
    )


def _node_class(*, name: str, machine_type: str, accelerator: dict | None) -> fnv1.Resource:
    """A GKENodeClass as Crossplane fetches it, attaching an accelerator unless it's None."""
    spec: dict = {"machineType": machine_type, "diskSizeGb": 100}
    if accelerator is not None:
        spec["accelerator"] = accelerator
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "gke.modelplane.ai/v1alpha1",
                "kind": "GKENodeClass",
                "metadata": {"name": name},
                "spec": spec,
            }
        ),
    )


def _reservation(*, project: str | None) -> fnv1.Resource:
    """The l4-calendar GKECapacityReservation in us-central1-a, as Crossplane fetches it, shared from project unless it's None."""
    spec: dict = {"zone": "us-central1-a", "name": "l4-calendar"}
    if project is not None:
        spec["project"] = project
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "gke.modelplane.ai/v1alpha1",
                "kind": "GKECapacityReservation",
                "metadata": {"name": "l4-calendar"},
                "spec": spec,
            }
        ),
    )


def _usage(*, kind: str, name: str, reason: str) -> fnv1.Resource:
    """A composed ClusterUsage blocking deletion of a GKE object the ProvisionedCluster references."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "ClusterUsage",
                "spec": {
                    "of": {
                        "apiVersion": "gke.modelplane.ai/v1alpha1",
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
    """The composed ManagedResourceActivationPolicy activating the kinds compose-gke-cluster composes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "apiextensions.crossplane.io/v1alpha1",
                "kind": "ManagedResourceActivationPolicy",
                "spec": {
                    "activate": [
                        "projectiammembers.cloudplatform.gcp.m.upbound.io",
                        "projectservices.cloudplatform.gcp.m.upbound.io",
                        "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                        "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                        "networks.compute.gcp.m.upbound.io",
                        "subnetworks.compute.gcp.m.upbound.io",
                        "clusters.container.gcp.m.upbound.io",
                        "nodepools.container.gcp.m.upbound.io",
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
                "status": {"activated": activated},
            }
        ),
    )


def _cluster(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed GKECluster, with a GPU pool and a CPU pool built from their node classes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "gke.modelplane.ai/v1alpha1",
                "kind": "GKECluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "region": "us-central1",
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ProviderConfig", "name": "my-gcp-account"},
                    "nodePools": [
                        {
                            "name": "gpu",
                            "role": "GPU",
                            "machineType": "g2-standard-8",
                            "diskSizeGb": 100,
                            "gpu": {"acceleratorType": "nvidia-l4", "acceleratorCount": 1},
                            "nodeCount": 1,
                            "minNodeCount": 1,
                            "maxNodeCount": 4,
                            "zones": ["us-central1-a"],
                        },
                        {
                            "name": "cpu",
                            "role": "CPU",
                            "machineType": "e2-standard-8",
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
    """The GKECluster as observed, reporting its kubeconfig and service account key Secrets and cache StorageClass, with a Ready condition that's True if ready and False if not."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "gke.modelplane.ai/v1alpha1",
                "kind": "GKECluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "region": "us-central1",
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ProviderConfig", "name": "my-gcp-account"},
                    "nodePools": [
                        {
                            "name": "gpu",
                            "role": "GPU",
                            "machineType": "g2-standard-8",
                            "diskSizeGb": 100,
                            "gpu": {"acceleratorType": "nvidia-l4", "acceleratorCount": 1},
                            "nodeCount": 1,
                            "minNodeCount": 1,
                            "maxNodeCount": 4,
                            "zones": ["us-central1-a"],
                        },
                        {
                            "name": "cpu",
                            "role": "CPU",
                            "machineType": "e2-standard-8",
                            "diskSizeGb": 100,
                            "nodeCount": 2,
                        },
                    ],
                },
                "status": {
                    "secrets": [
                        {"type": "Kubeconfig", "name": "prod-kubeconfig-6c1d0", "key": "kubeconfig"},
                        {"type": "GoogleApplicationCredentials", "name": "prod-sa-key-7e3a1", "key": "private_key"},
                    ],
                    "cache": {"storageClassName": "modelplane-rwx"},
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
    """The composed GKEDeviceStack, reaching the cluster through its kubeconfig as its service account."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "gke.modelplane.ai/v1alpha1",
                "kind": "GKEDeviceStack",
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
                            "type": "GoogleApplicationCredentials",
                            "name": "prod-sa-key-7e3a1",
                            "namespace": "modelplane-system",
                            "key": "private_key",
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
        reason="A GKEClusterConfig XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        configv1alpha1.GKEClusterConfig(
                            metadata=metav1.ObjectMeta(name="us-central"),
                            spec=configv1alpha1.Spec(region="us-central1"),
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
        reason="A GKENodeClass XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        ncv1alpha1.GKENodeClass(
                            metadata=metav1.ObjectMeta(name="l4"),
                            spec=ncv1alpha1.Spec(
                                machineType="g2-standard-8",
                                accelerator=ncv1alpha1.Accelerator(type="nvidia-l4", count=1),
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
        name="CapacityReservation",
        reason="A GKECapacityReservation XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        crv1alpha1.GKECapacityReservation(
                            metadata=metav1.ObjectMeta(name="l4-calendar"),
                            spec=crv1alpha1.Spec(zone="us-central1-a", name="l4-calendar"),
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
        reason=(
            "With nothing fetched yet, a ProvisionedCluster requires its config, node classes and reservation, and "
            "composes nothing."
        ),
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-central1-a")],
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="gke.modelplane.ai/v1alpha1",
                                kind="GKECapacityReservation",
                                name="l4-calendar",
                            ),
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
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
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-central",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                    "reservation-l4-calendar": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKECapacityReservation",
                        match_name="l4-calendar",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ReservationMissing",
        reason="A reservation that doesn't exist fails the reconcile, naming it as missing.",
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-central1-a")],
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="gke.modelplane.ai/v1alpha1",
                                kind="GKECapacityReservation",
                                name="l4-calendar",
                            ),
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
                        )
                    ],
                ),
                "node-class-e2": fnv1.Resources(
                    items=[_node_class(name="e2", machine_type="e2-standard-8", accelerator=None)],
                ),
                "reservation-l4-calendar": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_FATAL, message="Missing GKECapacityReservation l4-calendar"),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-central",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                    "reservation-l4-calendar": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKECapacityReservation",
                        match_name="l4-calendar",
                    ),
                },
            ),
        ),
    ),
    # The cpu pool was added since Crossplane last fetched the required
    # resources, so it hasn't fetched the e2 node class yet.
    Case(
        name="NodeClassNotFetched",
        reason="A node class Crossplane hasn't fetched yet holds back composing without failing the reconcile.",
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            zones=[v1alpha1.Zone("us-central1-a")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
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
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-central",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
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
                        apiVersion="gke.modelplane.ai/v1alpha1", kind="GKEClusterConfig", name="us-central"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            zones=[v1alpha1.Zone("us-central1-a")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
                        )
                    ],
                ),
                "node-class-e2": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_FATAL, message="Missing GKENodeClass e2"),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-central",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                },
            ),
        ),
    ),
    # What was the CapacityReservation's provider check: another provider's
    # reservation kind is a reservation for another cloud.
    Case(
        name="ForeignKinds",
        reason=(
            "A configRef, a nodeClassRef and a reservationRef to another provider's kinds fail the reconcile, naming "
            "all three."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSClusterConfig", name="us-west"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="l4"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-central1-a")],
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="eks.modelplane.ai/v1alpha1",
                                kind="EKSCapacityReservation",
                                name="l4-block",
                            ),
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
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
                        "configRef is a EKSClusterConfig.eks.modelplane.ai, not a "
                        "GKEClusterConfig.gke.modelplane.ai; node pool gpu's nodeClassRef is a "
                        "EKSNodeClass.eks.modelplane.ai, not a GKENodeClass.gke.modelplane.ai; node pool gpu's "
                        "reservationRef is a EKSCapacityReservation.eks.modelplane.ai, not a "
                        "GKECapacityReservation.gke.modelplane.ai"
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                    "reservation-l4-block": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSCapacityReservation",
                        match_name="l4-block",
                    ),
                },
            ),
        ),
    ),
    # Each pool names the other's node class.
    Case(
        name="RoleMismatch",
        reason=(
            "An Accelerator pool whose node class attaches no accelerator and a CPU pool whose node class attaches one "
            "fail the reconcile, naming both, once the ClusterUsages guard the classes."
        ),
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
                            ),
                            nodeCount=1,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
                        )
                    ],
                ),
                "node-class-e2": fnv1.Resources(
                    items=[_node_class(name="e2", machine_type="e2-standard-8", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-gkeclusterconfig-us-central": _usage(
                        kind="GKEClusterConfig",
                        name="us-central",
                        reason="ProvisionedCluster prod uses this GKEClusterConfig",
                    ),
                    "usage-gkenodeclass-l4": _usage(
                        kind="GKENodeClass",
                        name="l4",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "usage-gkenodeclass-e2": _usage(
                        kind="GKENodeClass",
                        name="e2",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message=(
                        "node pool gpu runs accelerators, but GKENodeClass e2 names no accelerator; "
                        "node pool cpu is a CPU pool, but GKENodeClass l4 names an accelerator"
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
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                },
            ),
        ),
    ),
    # A specific reservation only has capacity in its own zone, so a pool
    # that may create nodes in any other zone can't use it.
    Case(
        name="ReservationZoneMismatch",
        reason=(
            "A pool backed by a GKECapacityReservation that isn't restricted to exactly the reservation's zone fails "
            "the reconcile, once the ClusterUsages guard what the cluster references."
        ),
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-central1-a"), v1alpha1.Zone("us-central1-b")],
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="gke.modelplane.ai/v1alpha1",
                                kind="GKECapacityReservation",
                                name="l4-calendar",
                            ),
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
                        )
                    ],
                ),
                "node-class-e2": fnv1.Resources(
                    items=[_node_class(name="e2", machine_type="e2-standard-8", accelerator=None)],
                ),
                "reservation-l4-calendar": fnv1.Resources(items=[_reservation(project=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-gkeclusterconfig-us-central": _usage(
                        kind="GKEClusterConfig",
                        name="us-central",
                        reason="ProvisionedCluster prod uses this GKEClusterConfig",
                    ),
                    "usage-gkenodeclass-l4": _usage(
                        kind="GKENodeClass",
                        name="l4",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "usage-gkenodeclass-e2": _usage(
                        kind="GKENodeClass",
                        name="e2",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "usage-gkecapacityreservation-l4-calendar": _usage(
                        kind="GKECapacityReservation",
                        name="l4-calendar",
                        reason="ProvisionedCluster prod uses this GKECapacityReservation",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message=(
                        "node pool gpu must set zones to [us-central1-a] to use GKECapacityReservation l4-calendar"
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
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                    "reservation-l4-calendar": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKECapacityReservation",
                        match_name="l4-calendar",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="GPUPoolWithoutZones",
        reason=(
            "An Accelerator pool that names no zones fails the reconcile, once the ClusterUsages guard what the "
            "cluster references."
        ),
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
                        )
                    ],
                ),
                "node-class-e2": fnv1.Resources(
                    items=[_node_class(name="e2", machine_type="e2-standard-8", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-gkeclusterconfig-us-central": _usage(
                        kind="GKEClusterConfig",
                        name="us-central",
                        reason="ProvisionedCluster prod uses this GKEClusterConfig",
                    ),
                    "usage-gkenodeclass-l4": _usage(
                        kind="GKENodeClass",
                        name="l4",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "usage-gkenodeclass-e2": _usage(
                        kind="GKENodeClass",
                        name="e2",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="node pool gpu must name its zones, because GKE GPU pools need them",
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
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="NotActivated",
        reason=(
            "Until the activation policy reports every kind active, a ProvisionedCluster composes the policy and "
            "ClusterUsages but no GKECluster."
        ),
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-central1-a")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=["clusters.container.gcp.m.upbound.io", "nodepools.container.gcp.m.upbound.io"]
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
                        )
                    ],
                ),
                "node-class-e2": fnv1.Resources(
                    items=[_node_class(name="e2", machine_type="e2-standard-8", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-gkeclusterconfig-us-central": _usage(
                        kind="GKEClusterConfig",
                        name="us-central",
                        reason="ProvisionedCluster prod uses this GKEClusterConfig",
                    ),
                    "usage-gkenodeclass-l4": _usage(
                        kind="GKENodeClass",
                        name="l4",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "usage-gkenodeclass-e2": _usage(
                        kind="GKENodeClass",
                        name="e2",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-central",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="Activated",
        reason=(
            "Once the activation policy reports every kind active, a ProvisionedCluster composes a GKECluster with a "
            "GPU pool and a CPU pool from their node classes, and the region, version and credentials from its config."
        ),
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-central1-a")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
                        )
                    ],
                ),
                "node-class-e2": fnv1.Resources(
                    items=[_node_class(name="e2", machine_type="e2-standard-8", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-gkeclusterconfig-us-central": _usage(
                        kind="GKEClusterConfig",
                        name="us-central",
                        reason="ProvisionedCluster prod uses this GKEClusterConfig",
                    ),
                    "usage-gkenodeclass-l4": _usage(
                        kind="GKENodeClass",
                        name="l4",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "usage-gkenodeclass-e2": _usage(
                        kind="GKENodeClass",
                        name="e2",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "gke-cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-central",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                },
            ),
        ),
    ),
    # compose-gke-cluster turns the pool's reservation into a specific
    # reservation affinity, so GKE creates its nodes only from it.
    Case(
        name="Reservation",
        reason=(
            "A pool restricted to exactly its GKECapacityReservation's zone passes the reservation's name through to "
            "the GKECluster pool."
        ),
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-central1-a")],
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="gke.modelplane.ai/v1alpha1",
                                kind="GKECapacityReservation",
                                name="l4-calendar",
                            ),
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
                        )
                    ],
                ),
                "reservation-l4-calendar": fnv1.Resources(items=[_reservation(project=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-gkeclusterconfig-us-central": _usage(
                        kind="GKEClusterConfig",
                        name="us-central",
                        reason="ProvisionedCluster prod uses this GKEClusterConfig",
                    ),
                    "usage-gkenodeclass-l4": _usage(
                        kind="GKENodeClass",
                        name="l4",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "usage-gkecapacityreservation-l4-calendar": _usage(
                        kind="GKECapacityReservation",
                        name="l4-calendar",
                        reason="ProvisionedCluster prod uses this GKECapacityReservation",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "gke-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "gke.modelplane.ai/v1alpha1",
                                "kind": "GKECluster",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "region": "us-central1",
                                    "kubernetesVersion": "1.36",
                                    "credentials": {"type": "ProviderConfig", "name": "my-gcp-account"},
                                    "nodePools": [
                                        {
                                            "name": "gpu",
                                            "role": "GPU",
                                            "machineType": "g2-standard-8",
                                            "diskSizeGb": 100,
                                            "gpu": {"acceleratorType": "nvidia-l4", "acceleratorCount": 1},
                                            "nodeCount": 1,
                                            "minNodeCount": 1,
                                            "maxNodeCount": 4,
                                            "zones": ["us-central1-a"],
                                            "reservation": {"name": "l4-calendar"},
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
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-central",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "reservation-l4-calendar": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKECapacityReservation",
                        match_name="l4-calendar",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="SharedReservation",
        reason=(
            "A pool backed by a GKECapacityReservation shared from another project passes that project through to "
            "the GKECluster pool's reservation."
        ),
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-central1-a")],
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="gke.modelplane.ai/v1alpha1",
                                kind="GKECapacityReservation",
                                name="l4-calendar",
                            ),
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
                        )
                    ],
                ),
                "reservation-l4-calendar": fnv1.Resources(items=[_reservation(project="shared-reservations")]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-gkeclusterconfig-us-central": _usage(
                        kind="GKEClusterConfig",
                        name="us-central",
                        reason="ProvisionedCluster prod uses this GKEClusterConfig",
                    ),
                    "usage-gkenodeclass-l4": _usage(
                        kind="GKENodeClass",
                        name="l4",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "usage-gkecapacityreservation-l4-calendar": _usage(
                        kind="GKECapacityReservation",
                        name="l4-calendar",
                        reason="ProvisionedCluster prod uses this GKECapacityReservation",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "gke-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "gke.modelplane.ai/v1alpha1",
                                "kind": "GKECluster",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "region": "us-central1",
                                    "kubernetesVersion": "1.36",
                                    "credentials": {"type": "ProviderConfig", "name": "my-gcp-account"},
                                    "nodePools": [
                                        {
                                            "name": "gpu",
                                            "role": "GPU",
                                            "machineType": "g2-standard-8",
                                            "diskSizeGb": 100,
                                            "gpu": {"acceleratorType": "nvidia-l4", "acceleratorCount": 1},
                                            "nodeCount": 1,
                                            "minNodeCount": 1,
                                            "maxNodeCount": 4,
                                            "zones": ["us-central1-a"],
                                            "reservation": {
                                                "name": "l4-calendar",
                                                "project": "shared-reservations",
                                            },
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
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-central",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "reservation-l4-calendar": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKECapacityReservation",
                        match_name="l4-calendar",
                    ),
                },
            ),
        ),
    ),
    # A CPU pool, because the GKECluster requires zones for a GPU pool.
    Case(
        name="ActivatedDefaults",
        reason=(
            "A ProvisionedCluster whose config and pool leave optional fields unset composes a GKECluster that "
            "leaves them unset too."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="gke.modelplane.ai/v1alpha1", kind="GKEClusterConfig", name="us-east"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
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
                                    "apiVersion": "gke.modelplane.ai/v1alpha1",
                                    "kind": "GKEClusterConfig",
                                    "metadata": {"name": "us-east"},
                                    "spec": {"region": "us-east1"},
                                }
                            ),
                        ),
                    ],
                ),
                "node-class-e2": fnv1.Resources(
                    items=[_node_class(name="e2", machine_type="e2-standard-8", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-gkeclusterconfig-us-east": _usage(
                        kind="GKEClusterConfig",
                        name="us-east",
                        reason="ProvisionedCluster prod uses this GKEClusterConfig",
                    ),
                    "usage-gkenodeclass-e2": _usage(
                        kind="GKENodeClass",
                        name="e2",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "gke-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "gke.modelplane.ai/v1alpha1",
                                "kind": "GKECluster",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "region": "us-east1",
                                    "nodePools": [
                                        {
                                            "name": "cpu",
                                            "role": "CPU",
                                            "machineType": "e2-standard-8",
                                            "diskSizeGb": 100,
                                            "nodeCount": 2,
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
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-east",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                },
            ),
        ),
    ),
    # The service account key is the identity provider-kubernetes and
    # provider-helm authenticate to the cluster as, on top of the kubeconfig.
    Case(
        name="ClusterReady",
        reason=(
            "Once the GKECluster is Ready with a kubeconfig, a ProvisionedCluster composes a GKEDeviceStack reaching "
            "it through that Secret as the cluster's service account, and reports both Secrets and the cache "
            "StorageClass."
        ),
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-central1-a")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ],
                    ),
                    "gke-cluster": _observed_cluster(ready=True),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
                        )
                    ],
                ),
                "node-class-e2": fnv1.Resources(
                    items=[_node_class(name="e2", machine_type="e2-standard-8", accelerator=None)],
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
                            "type": "GoogleApplicationCredentials",
                            "name": "prod-sa-key-7e3a1",
                            "namespace": "modelplane-system",
                            "key": "private_key",
                        },
                    ],
                    storage_class_name="modelplane-rwx",
                ),
                resources={
                    "usage-gkeclusterconfig-us-central": _usage(
                        kind="GKEClusterConfig",
                        name="us-central",
                        reason="ProvisionedCluster prod uses this GKEClusterConfig",
                    ),
                    "usage-gkenodeclass-l4": _usage(
                        kind="GKENodeClass",
                        name="l4",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "usage-gkenodeclass-e2": _usage(
                        kind="GKENodeClass",
                        name="e2",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "gke-cluster": _cluster(ready=fnv1.READY_TRUE),
                    "device-stack": _device_stack(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-central",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="DeviceStackReady",
        reason="Once the GKEDeviceStack reports Ready, a ProvisionedCluster marks it ready.",
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-central1-a")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ],
                    ),
                    "gke-cluster": _observed_cluster(ready=True),
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "gke.modelplane.ai/v1alpha1",
                                "kind": "GKEDeviceStack",
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
                                            "type": "GoogleApplicationCredentials",
                                            "name": "prod-sa-key-7e3a1",
                                            "namespace": "modelplane-system",
                                            "key": "private_key",
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
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
                        )
                    ],
                ),
                "node-class-e2": fnv1.Resources(
                    items=[_node_class(name="e2", machine_type="e2-standard-8", accelerator=None)],
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
                            "type": "GoogleApplicationCredentials",
                            "name": "prod-sa-key-7e3a1",
                            "namespace": "modelplane-system",
                            "key": "private_key",
                        },
                    ],
                    storage_class_name="modelplane-rwx",
                ),
                resources={
                    "usage-gkeclusterconfig-us-central": _usage(
                        kind="GKEClusterConfig",
                        name="us-central",
                        reason="ProvisionedCluster prod uses this GKEClusterConfig",
                    ),
                    "usage-gkenodeclass-l4": _usage(
                        kind="GKENodeClass",
                        name="l4",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "usage-gkenodeclass-e2": _usage(
                        kind="GKENodeClass",
                        name="e2",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "gke-cluster": _cluster(ready=fnv1.READY_TRUE),
                    "device-stack": _device_stack(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-central",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                },
            ),
        ),
    ),
    # The observed device stack is still Ready: only the cluster flapped.
    Case(
        name="ClusterReadyRegressed",
        reason=(
            "With the GKECluster's Ready condition regressed to False, a ProvisionedCluster stops reporting its "
            "secrets but keeps the GKEDeviceStack from the device stack's own observed spec."
        ),
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
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="l4"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-central1-a")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="gke.modelplane.ai/v1alpha1", kind="GKENodeClass", name="e2"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ],
                    ),
                    "gke-cluster": _observed_cluster(ready=False),
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "gke.modelplane.ai/v1alpha1",
                                "kind": "GKEDeviceStack",
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
                                            "type": "GoogleApplicationCredentials",
                                            "name": "prod-sa-key-7e3a1",
                                            "namespace": "modelplane-system",
                                            "key": "private_key",
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
                "node-class-l4": fnv1.Resources(
                    items=[
                        _node_class(
                            name="l4", machine_type="g2-standard-8", accelerator={"type": "nvidia-l4", "count": 1}
                        )
                    ],
                ),
                "node-class-e2": fnv1.Resources(
                    items=[_node_class(name="e2", machine_type="e2-standard-8", accelerator=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name="modelplane-rwx"),
                resources={
                    "usage-gkeclusterconfig-us-central": _usage(
                        kind="GKEClusterConfig",
                        name="us-central",
                        reason="ProvisionedCluster prod uses this GKEClusterConfig",
                    ),
                    "usage-gkenodeclass-l4": _usage(
                        kind="GKENodeClass",
                        name="l4",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "usage-gkenodeclass-e2": _usage(
                        kind="GKENodeClass",
                        name="e2",
                        reason="ProvisionedCluster prod uses this GKENodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "gke-cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                    "device-stack": _device_stack(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKEClusterConfig",
                        match_name="us-central",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="l4",
                    ),
                    "node-class-e2": fnv1.ResourceSelector(
                        api_version="gke.modelplane.ai/v1alpha1",
                        kind="GKENodeClass",
                        match_name="e2",
                    ),
                },
            ),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction provisions a GKE cluster for a ProvisionedCluster."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
