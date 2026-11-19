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
from models.ai.modelplane.eks.ekscapacityreservation import v1alpha1 as crv1alpha1
from models.ai.modelplane.eks.eksclusterconfig import v1alpha1 as configv1alpha1
from models.ai.modelplane.eks.eksnodeclass import v1alpha1 as ncv1alpha1
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
    """The us-west EKSClusterConfig, as Crossplane fetches it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.modelplane.ai/v1alpha1",
                "kind": "EKSClusterConfig",
                "metadata": {"name": "us-west"},
                "spec": {
                    "region": "us-west-2",
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ClusterProviderConfig", "name": "aws"},
                },
            }
        ),
    )


def _node_class(*, name: str, instance_type: str, accelerator: str | None, fabric: str) -> fnv1.Resource:
    """An EKSNodeClass as Crossplane fetches it, with the given fabric, naming an accelerator unless it's None."""
    spec: dict = {"instanceType": instance_type, "diskSizeGb": 100, "fabric": fabric}
    if accelerator is not None:
        spec["accelerator"] = {"type": accelerator}
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.modelplane.ai/v1alpha1",
                "kind": "EKSNodeClass",
                "metadata": {"name": name},
                "spec": spec,
            }
        ),
    )


def _usage(*, kind: str, name: str, reason: str) -> fnv1.Resource:
    """A composed ClusterUsage blocking deletion of an EKS object the ProvisionedCluster references."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "ClusterUsage",
                "spec": {
                    "of": {
                        "apiVersion": "eks.modelplane.ai/v1alpha1",
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
    """The composed ManagedResourceActivationPolicy activating the kinds compose-eks-cluster composes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "apiextensions.crossplane.io/v1alpha1",
                "kind": "ManagedResourceActivationPolicy",
                "spec": {
                    "activate": [
                        "eips.ec2.aws.m.upbound.io",
                        "internetgateways.ec2.aws.m.upbound.io",
                        "launchtemplates.ec2.aws.m.upbound.io",
                        "natgateways.ec2.aws.m.upbound.io",
                        "routes.ec2.aws.m.upbound.io",
                        "routetables.ec2.aws.m.upbound.io",
                        "routetableassociations.ec2.aws.m.upbound.io",
                        "securitygroups.ec2.aws.m.upbound.io",
                        "securitygroupegressrules.ec2.aws.m.upbound.io",
                        "securitygroupingressrules.ec2.aws.m.upbound.io",
                        "subnets.ec2.aws.m.upbound.io",
                        "vpcs.ec2.aws.m.upbound.io",
                        "filesystems.efs.aws.m.upbound.io",
                        "mounttargets.efs.aws.m.upbound.io",
                        "addons.eks.aws.m.upbound.io",
                        "clusters.eks.aws.m.upbound.io",
                        "clusterauths.eks.aws.m.upbound.io",
                        "nodegroups.eks.aws.m.upbound.io",
                        "podidentityassociations.eks.aws.m.upbound.io",
                        "policies.iam.aws.m.upbound.io",
                        "roles.iam.aws.m.upbound.io",
                        "rolepolicyattachments.iam.aws.m.upbound.io",
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
    """The composed EKSCluster, with a GPU pool and a CPU pool built from their node classes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.modelplane.ai/v1alpha1",
                "kind": "EKSCluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "region": "us-west-2",
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ClusterProviderConfig", "name": "aws"},
                    "nodePools": [
                        {
                            "name": "gpu",
                            "role": "GPU",
                            "instanceType": "p5.48xlarge",
                            "diskSizeGb": 100,
                            "gpu": {"acceleratorType": "nvidia-h100"},
                            "nodeCount": 1,
                            "minNodeCount": 1,
                            "maxNodeCount": 4,
                            "zones": ["us-west-2a"],
                        },
                        {
                            "name": "cpu",
                            "role": "CPU",
                            "instanceType": "m6i.4xlarge",
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
    """The EKSCluster as observed, reporting its kubeconfig Secret and cache StorageClass, with a Ready condition that's True if ready and False if not."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.modelplane.ai/v1alpha1",
                "kind": "EKSCluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "region": "us-west-2",
                    "kubernetesVersion": "1.36",
                    "credentials": {"type": "ClusterProviderConfig", "name": "aws"},
                    "nodePools": [
                        {
                            "name": "gpu",
                            "role": "GPU",
                            "instanceType": "p5.48xlarge",
                            "diskSizeGb": 100,
                            "gpu": {"acceleratorType": "nvidia-h100"},
                            "nodeCount": 1,
                            "minNodeCount": 1,
                            "maxNodeCount": 4,
                            "zones": ["us-west-2a"],
                        },
                        {
                            "name": "cpu",
                            "role": "CPU",
                            "instanceType": "m6i.4xlarge",
                            "diskSizeGb": 100,
                            "nodeCount": 2,
                        },
                    ],
                },
                "status": {
                    "secrets": [{"type": "Kubeconfig", "name": "prod-kubeconfig-6c1d0", "key": "kubeconfig"}],
                    "cache": {"storageClassName": "modelplane-rwx-efs"},
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
    """The composed EKSDeviceStack, reaching the cluster through its kubeconfig Secret."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.modelplane.ai/v1alpha1",
                "kind": "EKSDeviceStack",
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
        reason="An EKSClusterConfig XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        configv1alpha1.EKSClusterConfig(
                            metadata=metav1.ObjectMeta(name="us-west"),
                            spec=configv1alpha1.Spec(region="us-west-2"),
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
        reason="An EKSNodeClass XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        ncv1alpha1.EKSNodeClass(
                            metadata=metav1.ObjectMeta(name="h100"),
                            spec=ncv1alpha1.Spec(
                                instanceType="p5.48xlarge",
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
        name="CapacityReservation",
        reason="An EKSCapacityReservation XR shares this function's Composition but composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        crv1alpha1.EKSCapacityReservation(
                            metadata=metav1.ObjectMeta(name="h100-block"),
                            spec=crv1alpha1.Spec(region="us-west-2", type="CapacityBlock", id="cr-0123456789abcdef0"),
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
                        apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSClusterConfig", name="us-west"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-west-2a")],
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="eks.modelplane.ai/v1alpha1",
                                kind="EKSCapacityReservation",
                                name="h100-block",
                            ),
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
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
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
                    ),
                    "reservation-h100-block": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSCapacityReservation",
                        match_name="h100-block",
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
                        apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSClusterConfig", name="us-west"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-west-2a")],
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="eks.modelplane.ai/v1alpha1",
                                kind="EKSCapacityReservation",
                                name="h100-block",
                            ),
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
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
                        _node_class(name="h100", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="None")
                    ],
                ),
                "node-class-m6i": fnv1.Resources(
                    items=[_node_class(name="m6i", instance_type="m6i.4xlarge", accelerator=None, fabric="None")],
                ),
                "reservation-h100-block": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_FATAL, message="Missing EKSCapacityReservation h100-block"),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
                    ),
                    "reservation-h100-block": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSCapacityReservation",
                        match_name="h100-block",
                    ),
                },
            ),
        ),
    ),
    # The cpu pool was added since Crossplane last fetched the required
    # resources, so it hasn't fetched the m6i node class yet.
    Case(
        name="NodeClassNotFetched",
        reason="A node class Crossplane hasn't fetched yet holds back composing without failing the reconcile.",
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
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
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
                        _node_class(name="h100", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="None")
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
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
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
                        apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSClusterConfig", name="us-west"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
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
                        _node_class(name="h100", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="None")
                    ],
                ),
                "node-class-m6i": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_FATAL, message="Missing EKSNodeClass m6i"),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
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
                            zones=[v1alpha1.Zone("us-west-2a")],
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
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
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
                        "EKSClusterConfig.eks.modelplane.ai; node pool gpu's nodeClassRef is a "
                        "GKENodeClass.gke.modelplane.ai, not a EKSNodeClass.eks.modelplane.ai; node pool gpu's "
                        "reservationRef is a GKECapacityReservation.gke.modelplane.ai, not a "
                        "EKSCapacityReservation.eks.modelplane.ai"
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
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
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
                        apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSClusterConfig", name="us-west"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
                            ),
                            nodeCount=1,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
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
                        _node_class(name="h100", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="None")
                    ],
                ),
                "node-class-m6i": fnv1.Resources(
                    items=[_node_class(name="m6i", instance_type="m6i.4xlarge", accelerator=None, fabric="None")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-eksclusterconfig-us-west": _usage(
                        kind="EKSClusterConfig",
                        name="us-west",
                        reason="ProvisionedCluster prod uses this EKSClusterConfig",
                    ),
                    "usage-eksnodeclass-h100": _usage(
                        kind="EKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "usage-eksnodeclass-m6i": _usage(
                        kind="EKSNodeClass",
                        name="m6i",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message=(
                        "node pool gpu runs accelerators, but EKSNodeClass m6i names no accelerator; "
                        "node pool cpu is a CPU pool, but EKSNodeClass h100 names an accelerator"
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
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ReservationWrongRegion",
        reason=(
            "A pool backed by an EKSCapacityReservation in another region than the cluster's fails the reconcile, "
            "once the ClusterUsages guard what the cluster references."
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
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-west-2a")],
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="eks.modelplane.ai/v1alpha1",
                                kind="EKSCapacityReservation",
                                name="h100-block",
                            ),
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
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
                        _node_class(name="h100", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="None")
                    ],
                ),
                "node-class-m6i": fnv1.Resources(
                    items=[_node_class(name="m6i", instance_type="m6i.4xlarge", accelerator=None, fabric="None")],
                ),
                "reservation-h100-block": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "eks.modelplane.ai/v1alpha1",
                                    "kind": "EKSCapacityReservation",
                                    "metadata": {"name": "h100-block"},
                                    "spec": {
                                        "region": "us-east-2",
                                        "type": "CapacityBlock",
                                        "id": "cr-0123456789abcdef0",
                                    },
                                }
                            ),
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-eksclusterconfig-us-west": _usage(
                        kind="EKSClusterConfig",
                        name="us-west",
                        reason="ProvisionedCluster prod uses this EKSClusterConfig",
                    ),
                    "usage-eksnodeclass-h100": _usage(
                        kind="EKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "usage-eksnodeclass-m6i": _usage(
                        kind="EKSNodeClass",
                        name="m6i",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "usage-ekscapacityreservation-h100-block": _usage(
                        kind="EKSCapacityReservation",
                        name="h100-block",
                        reason="ProvisionedCluster prod uses this EKSCapacityReservation",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="EKSCapacityReservation h100-block is in region us-east-2, but the cluster is in us-west-2",
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
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
                    ),
                    "reservation-h100-block": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSCapacityReservation",
                        match_name="h100-block",
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
                        apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSClusterConfig", name="us-west"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
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
                        _node_class(name="h100", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="None")
                    ],
                ),
                "node-class-m6i": fnv1.Resources(
                    items=[_node_class(name="m6i", instance_type="m6i.4xlarge", accelerator=None, fabric="None")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-eksclusterconfig-us-west": _usage(
                        kind="EKSClusterConfig",
                        name="us-west",
                        reason="ProvisionedCluster prod uses this EKSClusterConfig",
                    ),
                    "usage-eksnodeclass-h100": _usage(
                        kind="EKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "usage-eksnodeclass-m6i": _usage(
                        kind="EKSNodeClass",
                        name="m6i",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="node pool gpu must name its zones, because EKS GPU pools need them",
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
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="NotActivated",
        reason=(
            "Until the activation policy reports every kind active, a ProvisionedCluster composes the policy and "
            "ClusterUsages but no EKSCluster."
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
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-west-2a")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=["clusters.eks.aws.m.upbound.io", "nodegroups.eks.aws.m.upbound.io"]
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[
                        _node_class(name="h100", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="None")
                    ],
                ),
                "node-class-m6i": fnv1.Resources(
                    items=[_node_class(name="m6i", instance_type="m6i.4xlarge", accelerator=None, fabric="None")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-eksclusterconfig-us-west": _usage(
                        kind="EKSClusterConfig",
                        name="us-west",
                        reason="ProvisionedCluster prod uses this EKSClusterConfig",
                    ),
                    "usage-eksnodeclass-h100": _usage(
                        kind="EKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "usage-eksnodeclass-m6i": _usage(
                        kind="EKSNodeClass",
                        name="m6i",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="Activated",
        reason=(
            "Once the activation policy reports every kind active, a ProvisionedCluster composes an EKSCluster with a "
            "GPU pool and a CPU pool from their node classes, and the region, version and credentials from its config."
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
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-west-2a")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[
                        _node_class(name="h100", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="None")
                    ],
                ),
                "node-class-m6i": fnv1.Resources(
                    items=[_node_class(name="m6i", instance_type="m6i.4xlarge", accelerator=None, fabric="None")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-eksclusterconfig-us-west": _usage(
                        kind="EKSClusterConfig",
                        name="us-west",
                        reason="ProvisionedCluster prod uses this EKSClusterConfig",
                    ),
                    "usage-eksnodeclass-h100": _usage(
                        kind="EKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "usage-eksnodeclass-m6i": _usage(
                        kind="EKSNodeClass",
                        name="m6i",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "eks-cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
                    ),
                },
            ),
        ),
    ),
    # compose-eks-cluster turns the pool's capacityReservation into a launch
    # template that launches only into the reservation.
    Case(
        name="CapacityBlock",
        reason=(
            "A pool backed by a Capacity Block EKSCapacityReservation in the cluster's region passes the "
            "reservation's type and ID through to the EKSCluster pool."
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
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-west-2a")],
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="eks.modelplane.ai/v1alpha1",
                                kind="EKSCapacityReservation",
                                name="h100-block",
                            ),
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[
                        _node_class(name="h100", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="None")
                    ],
                ),
                "reservation-h100-block": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "eks.modelplane.ai/v1alpha1",
                                    "kind": "EKSCapacityReservation",
                                    "metadata": {"name": "h100-block"},
                                    "spec": {
                                        "region": "us-west-2",
                                        "type": "CapacityBlock",
                                        "id": "cr-0123456789abcdef0",
                                    },
                                }
                            ),
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
                    "usage-eksclusterconfig-us-west": _usage(
                        kind="EKSClusterConfig",
                        name="us-west",
                        reason="ProvisionedCluster prod uses this EKSClusterConfig",
                    ),
                    "usage-eksnodeclass-h100": _usage(
                        kind="EKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "usage-ekscapacityreservation-h100-block": _usage(
                        kind="EKSCapacityReservation",
                        name="h100-block",
                        reason="ProvisionedCluster prod uses this EKSCapacityReservation",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "eks-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "eks.modelplane.ai/v1alpha1",
                                "kind": "EKSCluster",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "region": "us-west-2",
                                    "kubernetesVersion": "1.36",
                                    "credentials": {"type": "ClusterProviderConfig", "name": "aws"},
                                    "nodePools": [
                                        {
                                            "name": "gpu",
                                            "role": "GPU",
                                            "instanceType": "p5.48xlarge",
                                            "diskSizeGb": 100,
                                            "gpu": {"acceleratorType": "nvidia-h100"},
                                            "nodeCount": 1,
                                            "minNodeCount": 1,
                                            "maxNodeCount": 4,
                                            "zones": ["us-west-2a"],
                                            "capacityReservation": {
                                                "type": "CapacityBlock",
                                                "id": "cr-0123456789abcdef0",
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
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "reservation-h100-block": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSCapacityReservation",
                        match_name="h100-block",
                    ),
                },
            ),
        ),
    ),
    # compose-eks-cluster turns the pool's fabric into EFA launch template
    # interfaces, and installs the EFA DRA driver.
    Case(
        name="FabricEFA",
        reason="A pool whose node class asks for the EFA fabric passes it through to the EKSCluster pool.",
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
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100-efa"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-west-2a")],
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ],
                    ),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100-efa": fnv1.Resources(
                    items=[
                        _node_class(
                            name="h100-efa", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="EFA"
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
                    "usage-eksclusterconfig-us-west": _usage(
                        kind="EKSClusterConfig",
                        name="us-west",
                        reason="ProvisionedCluster prod uses this EKSClusterConfig",
                    ),
                    "usage-eksnodeclass-h100-efa": _usage(
                        kind="EKSNodeClass",
                        name="h100-efa",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "eks-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "eks.modelplane.ai/v1alpha1",
                                "kind": "EKSCluster",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "region": "us-west-2",
                                    "kubernetesVersion": "1.36",
                                    "credentials": {"type": "ClusterProviderConfig", "name": "aws"},
                                    "nodePools": [
                                        {
                                            "name": "gpu",
                                            "role": "GPU",
                                            "instanceType": "p5.48xlarge",
                                            "diskSizeGb": 100,
                                            "gpu": {"acceleratorType": "nvidia-h100"},
                                            "nodeCount": 1,
                                            "minNodeCount": 1,
                                            "maxNodeCount": 4,
                                            "zones": ["us-west-2a"],
                                            "fabric": "EFA",
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
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-h100-efa": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100-efa",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ActivatedDefaults",
        reason=(
            "A ProvisionedCluster whose config and pool leave optional fields unset composes an EKSCluster that "
            "leaves them unset too."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    config_ref=v1alpha1.ConfigRef(
                        apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSClusterConfig", name="us-east"
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu",
                            role="Accelerator",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="l4"
                            ),
                            nodeCount=1,
                            zones=[v1alpha1.Zone("us-east-1a")],
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
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
                                    "apiVersion": "eks.modelplane.ai/v1alpha1",
                                    "kind": "EKSClusterConfig",
                                    "metadata": {"name": "us-east"},
                                    "spec": {"region": "us-east-1"},
                                }
                            ),
                        ),
                    ],
                ),
                "node-class-l4": fnv1.Resources(
                    items=[_node_class(name="l4", instance_type="g6.xlarge", accelerator="nvidia-l4", fabric="None")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name=None),
                resources={
                    "usage-eksclusterconfig-us-east": _usage(
                        kind="EKSClusterConfig",
                        name="us-east",
                        reason="ProvisionedCluster prod uses this EKSClusterConfig",
                    ),
                    "usage-eksnodeclass-l4": _usage(
                        kind="EKSNodeClass",
                        name="l4",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "eks-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "eks.modelplane.ai/v1alpha1",
                                "kind": "EKSCluster",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "region": "us-east-1",
                                    "nodePools": [
                                        {
                                            "name": "gpu",
                                            "role": "GPU",
                                            "instanceType": "g6.xlarge",
                                            "diskSizeGb": 100,
                                            "gpu": {"acceleratorType": "nvidia-l4"},
                                            "nodeCount": 1,
                                            "zones": ["us-east-1a"],
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
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-east",
                    ),
                    "node-class-l4": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="l4",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ClusterReady",
        reason=(
            "Once the EKSCluster is Ready with a kubeconfig, a ProvisionedCluster composes an EKSDeviceStack reaching "
            "it through that Secret, and reports the Secret and the cache StorageClass."
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
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-west-2a")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ],
                    ),
                    "eks-cluster": _observed_cluster(ready=True),
                },
            ),
            required_resources={
                "config": fnv1.Resources(items=[_config()]),
                "node-class-h100": fnv1.Resources(
                    items=[
                        _node_class(name="h100", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="None")
                    ],
                ),
                "node-class-m6i": fnv1.Resources(
                    items=[_node_class(name="m6i", instance_type="m6i.4xlarge", accelerator=None, fabric="None")],
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
                    storage_class_name="modelplane-rwx-efs",
                ),
                resources={
                    "usage-eksclusterconfig-us-west": _usage(
                        kind="EKSClusterConfig",
                        name="us-west",
                        reason="ProvisionedCluster prod uses this EKSClusterConfig",
                    ),
                    "usage-eksnodeclass-h100": _usage(
                        kind="EKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "usage-eksnodeclass-m6i": _usage(
                        kind="EKSNodeClass",
                        name="m6i",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "eks-cluster": _cluster(ready=fnv1.READY_TRUE),
                    "device-stack": _device_stack(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="DeviceStackReady",
        reason="Once the EKSDeviceStack reports Ready, a ProvisionedCluster marks it ready.",
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
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-west-2a")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ],
                    ),
                    "eks-cluster": _observed_cluster(ready=True),
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "eks.modelplane.ai/v1alpha1",
                                "kind": "EKSDeviceStack",
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
                        _node_class(name="h100", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="None")
                    ],
                ),
                "node-class-m6i": fnv1.Resources(
                    items=[_node_class(name="m6i", instance_type="m6i.4xlarge", accelerator=None, fabric="None")],
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
                    storage_class_name="modelplane-rwx-efs",
                ),
                resources={
                    "usage-eksclusterconfig-us-west": _usage(
                        kind="EKSClusterConfig",
                        name="us-west",
                        reason="ProvisionedCluster prod uses this EKSClusterConfig",
                    ),
                    "usage-eksnodeclass-h100": _usage(
                        kind="EKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "usage-eksnodeclass-m6i": _usage(
                        kind="EKSNodeClass",
                        name="m6i",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "eks-cluster": _cluster(ready=fnv1.READY_TRUE),
                    "device-stack": _device_stack(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
                    ),
                },
            ),
        ),
    ),
    # The observed device stack is still Ready: only the cluster flapped.
    Case(
        name="ClusterReadyRegressed",
        reason=(
            "With the EKSCluster's Ready condition regressed to False, a ProvisionedCluster stops reporting its "
            "secrets but keeps the EKSDeviceStack from the device stack's own observed spec."
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
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="h100"
                            ),
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=[v1alpha1.Zone("us-west-2a")],
                        ),
                        v1alpha1.NodePool(
                            name="cpu",
                            role="CPU",
                            nodeClassRef=v1alpha1.NodeClassRef(
                                apiVersion="eks.modelplane.ai/v1alpha1", kind="EKSNodeClass", name="m6i"
                            ),
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation(
                        activated=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ],
                    ),
                    "eks-cluster": _observed_cluster(ready=False),
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "eks.modelplane.ai/v1alpha1",
                                "kind": "EKSDeviceStack",
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
                        _node_class(name="h100", instance_type="p5.48xlarge", accelerator="nvidia-h100", fabric="None")
                    ],
                ),
                "node-class-m6i": fnv1.Resources(
                    items=[_node_class(name="m6i", instance_type="m6i.4xlarge", accelerator=None, fabric="None")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(secrets=None, storage_class_name="modelplane-rwx-efs"),
                resources={
                    "usage-eksclusterconfig-us-west": _usage(
                        kind="EKSClusterConfig",
                        name="us-west",
                        reason="ProvisionedCluster prod uses this EKSClusterConfig",
                    ),
                    "usage-eksnodeclass-h100": _usage(
                        kind="EKSNodeClass",
                        name="h100",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "usage-eksnodeclass-m6i": _usage(
                        kind="EKSNodeClass",
                        name="m6i",
                        reason="ProvisionedCluster prod uses this EKSNodeClass",
                    ),
                    "activation": _activation(ready=fnv1.READY_TRUE),
                    "eks-cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                    "device-stack": _device_stack(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSClusterConfig",
                        match_name="us-west",
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="h100",
                    ),
                    "node-class-m6i": fnv1.ResourceSelector(
                        api_version="eks.modelplane.ai/v1alpha1",
                        kind="EKSNodeClass",
                        match_name="m6i",
                    ),
                },
            ),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction provisions an EKS cluster for a ProvisionedCluster."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
