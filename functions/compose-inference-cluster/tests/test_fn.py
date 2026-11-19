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

"""Tests for the compose-inference-cluster function."""

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
from models.ai.modelplane.inferencecluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class ComposeCase:
    """A test case for RunFunction."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class GatewayHostnameCase:
    """A test case for _gateway_hostname."""

    name: str
    reason: str
    cluster_name: str
    want: str


def _inference_cluster(*, cluster: v1alpha1.Cluster, node_pools: list[v1alpha1.NodePool] | None) -> fnv1.Resource:
    """The observed InferenceCluster XR, test-cluster, with node_pools unless they're None."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.InferenceCluster(
                metadata=metav1.ObjectMeta(name="test-cluster"),
                spec=v1alpha1.Spec(cluster=cluster, nodePools=node_pools),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        ),
    )


def _desired_inference_cluster(
    *, schedulable_pools: list[dict], cache: dict | None, gateway: dict | None
) -> fnv1.Resource:
    """The desired InferenceCluster XR's status, with its cache and gateway unless they're None."""
    status: dict = {
        "providerConfigRef": {"name": "test-cluster-cluster-kubeconfig-d0f89"},
        "namespace": "modelplane-system",
        "schedulablePools": schedulable_pools,
    }
    if cache is not None:
        status["cache"] = cache
    if gateway is not None:
        status["gateway"] = gateway
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}))


def _inference_class(*, name: str, count: int, memory: str, node_class: str | None) -> fnv1.Resource:
    """An InferenceClass of count DRA-claimed NVIDIA GPUs with memory each, as its class requirement returns it,
    referencing the AcmeNodeClass node_class unless it's None."""
    spec: dict = {
        "devices": [
            {
                "name": "gpu",
                "claim": "DRA",
                "driver": "gpu.nvidia.com",
                "deviceClassName": "gpu.nvidia.com",
                "count": count,
                "capacity": {"memory": {"value": memory}},
            },
        ],
    }
    if node_class is not None:
        spec["nodeClassRef"] = {"apiVersion": "acme.example.org/v1alpha1", "kind": "AcmeNodeClass", "name": node_class}
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {"apiVersion": "modelplane.ai/v1alpha1", "kind": "InferenceClass", "metadata": {"name": name}, "spec": spec}
        )
    )


def _cpu_inference_class(*, name: str, cpus: str, node_class: str | None) -> fnv1.Resource:
    """An InferenceClass of one DRA-claimed CPU device of cpus cores, as its class requirement returns it,
    referencing the AcmeNodeClass node_class unless it's None."""
    spec: dict = {
        "devices": [
            {
                "name": "cpu",
                "claim": "DRA",
                "driver": "dra.cpu",
                "deviceClassName": "dra.cpu",
                "count": 1,
                "capacity": {"cpu": {"value": cpus}},
            },
        ],
    }
    if node_class is not None:
        spec["nodeClassRef"] = {"apiVersion": "acme.example.org/v1alpha1", "kind": "AcmeNodeClass", "name": node_class}
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {"apiVersion": "modelplane.ai/v1alpha1", "kind": "InferenceClass", "metadata": {"name": name}, "spec": spec}
        )
    )


def _provider(*, name: str, annotations: dict, api_version: str) -> fnv1.Resource:
    """The acme provider's Composition for a ProvisionedCluster, as the provider requirement returns it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "apiextensions.crossplane.io/v1",
                "kind": "Composition",
                "metadata": {
                    "name": name,
                    "labels": {
                        "infrastructure.modelplane.ai/config-group": "acme.example.org",
                        "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                    },
                    "annotations": annotations,
                },
                "spec": {
                    "compositeTypeRef": {
                        "apiVersion": api_version,
                        "kind": "ProvisionedCluster",
                    },
                },
            }
        )
    )


def _model_replica(*, namespace: str) -> fnv1.Resource:
    """A ModelReplica scheduled to test-cluster, as the model-replicas requirement returns it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelReplica",
                "metadata": {
                    "name": "deploy-test-cluster-0",
                    "namespace": namespace,
                    "labels": {"modelplane.ai/cluster": "test-cluster"},
                },
            }
        )
    )


def _inference_gateway(*, name: str, cluster: str, status: dict | None) -> fnv1.Resource:
    """An InferenceGateway on cluster, as the gateways requirement returns it, with status unless it's None."""
    gateway: dict = {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "InferenceGateway",
        "metadata": {"name": name},
        "spec": {"clusterName": cluster},
    }
    if status is not None:
        gateway["status"] = status
    return fnv1.Resource(resource=resource.dict_to_struct(gateway))


def _model_cache(*, name: str, namespace: str, cluster: str) -> fnv1.Resource:
    """A ModelCache staged onto cluster, as the model-caches requirement returns it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelCache",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {"source": "HuggingFace"},
                "status": {"clusters": [{"name": cluster, "phase": "Ready"}]},
            }
        )
    )


def _observed_serving_stack(*, gateway: dict) -> fnv1.Resource:
    """The observed ServingStack, Ready, with the gateway status it has published."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "ServingStack",
                "metadata": {"name": "test-cluster-serving-stack-fd00b"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}], "gateway": gateway},
            }
        )
    )


def _observed_provisioned_cluster(*, status: dict) -> fnv1.Resource:
    """The observed ProvisionedCluster for l4-pool, with the status its provider reported."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "ProvisionedCluster",
                "metadata": {"name": "test-cluster"},
                "spec": {
                    "configRef": {
                        "apiVersion": "acme.example.org/v1alpha1",
                        "kind": "AcmeClusterConfig",
                        "name": "default",
                    },
                    "draDrivers": ["gpu.nvidia.com"],
                    "nodePools": [
                        {
                            "name": "l4-pool",
                            "nodeClassRef": {
                                "apiVersion": "acme.example.org/v1alpha1",
                                "kind": "AcmeNodeClass",
                                "name": "l4",
                            },
                            "role": "Accelerator",
                            "nodeCount": 2,
                            "maxNodeCount": 4,
                        },
                    ],
                },
                "status": status,
            }
        )
    )


def _provisioned_cluster(*, node_pools: list[dict], dra_drivers: list[str], ready: fnv1.Ready) -> fnv1.Resource:
    """The composed ProvisionedCluster, provisioned from the default AcmeClusterConfig by the acme provider."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "ProvisionedCluster",
                "metadata": {"name": "test-cluster"},
                "spec": {
                    "crossplane": {
                        "compositionSelector": {
                            "matchLabels": {
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            },
                        },
                    },
                    "configRef": {
                        "apiVersion": "acme.example.org/v1alpha1",
                        "kind": "AcmeClusterConfig",
                        "name": "default",
                    },
                    "draDrivers": dra_drivers,
                    "nodePools": node_pools,
                },
            }
        ),
        ready=ready,
    )


def _cluster_provider_config(*, kubeconfig: str, identity: dict | None) -> fnv1.Resource:
    """The ClusterProviderConfig reaching test-cluster with the kubeconfig Secret, as identity unless it's None."""
    spec: dict = {
        "credentials": {
            "source": "Secret",
            "secretRef": {"namespace": "modelplane-system", "name": kubeconfig, "key": "kubeconfig"},
        },
    }
    if identity is not None:
        spec["identity"] = identity
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "ClusterProviderConfig",
                "metadata": {"name": "test-cluster-cluster-kubeconfig-d0f89"},
                "spec": spec,
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _serving_stack(
    *,
    device_stacks: str,
    secrets: list[dict],
    dra_drivers: list[str],
    client_cas: list[dict] | None,
    service: dict | None,
    telemetry: dict | None,
    ready: fnv1.Ready,
) -> fnv1.Resource:
    """The composed ServingStack, its gateway accepting client_cas and configuring its Service as service, and its
    telemetry set to telemetry, unless they're None."""
    gateway: dict = {"hostname": "gateway-test-cluster-09532.modelplane-system.svc.cluster.local"}
    if client_cas is not None:
        gateway["clientCAs"] = client_cas
    if service is not None:
        gateway["service"] = service
    spec: dict = {
        "deviceStacks": device_stacks,
        "gateway": gateway,
        "stack": "Standard",
        "secrets": secrets,
        "draDrivers": dra_drivers,
    }
    if telemetry is not None:
        spec["telemetry"] = telemetry
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "ServingStack",
                "metadata": {"name": "test-cluster-serving-stack-fd00b", "namespace": "modelplane-system"},
                "spec": spec,
            }
        ),
        ready=ready,
    )


def _provisioned_usage() -> fnv1.Resource:
    """The composed Usage holding the ProvisionedCluster until the ServingStack is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "of": {
                        "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                        "kind": "ProvisionedCluster",
                        "resourceRef": {"name": "test-cluster", "namespace": ""},
                    },
                    "by": {
                        "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                        "kind": "ServingStack",
                        "resourceSelector": {"matchControllerRef": True},
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _guard_cluster_usage(*, reason: str) -> fnv1.Resource:
    """The reason-only ClusterUsage the deletion guard composes for test-cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "ClusterUsage",
                "spec": {
                    "of": {
                        "apiVersion": "modelplane.ai/v1alpha1",
                        "kind": "InferenceCluster",
                        "resourceRef": {"name": "test-cluster"},
                    },
                    "reason": reason,
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _input_usage(*, name: str) -> fnv1.Resource:
    """The ClusterUsage blocking deletion of an InferenceClass test-cluster uses."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "ClusterUsage",
                "spec": {
                    "of": {
                        "apiVersion": "modelplane.ai/v1alpha1",
                        "kind": "InferenceClass",
                        "resourceRef": {"name": name},
                    },
                    "reason": "InferenceCluster test-cluster uses this InferenceClass",
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _namespace_object(*, team: str, name: str) -> fnv1.Resource:
    """The composed Object that mirrors team's namespace onto the cluster as the Namespace name."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "managementPolicies": ["Observe", "Create", "Update"],
                    "providerConfigRef": {
                        "kind": "ClusterProviderConfig",
                        "name": "test-cluster-cluster-kubeconfig-d0f89",
                    },
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "Namespace",
                            "metadata": {"name": name, "labels": {"modelplane.ai/namespace": team}},
                        },
                    },
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _pool_observation(*, pool: str) -> fnv1.Resource:
    """The composed Object that creates a PoolObservation of pool on the cluster, watched and polled, without
    deleting it, and marked ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {
                    "namespace": "modelplane-system",
                    "annotations": {"crossplane.io/poll-interval": "30s"},
                },
                "spec": {
                    "providerConfigRef": {
                        "kind": "ClusterProviderConfig",
                        "name": "test-cluster-cluster-kubeconfig-d0f89",
                    },
                    "readiness": {"policy": "SuccessfulCreate"},
                    "watch": True,
                    "managementPolicies": ["Observe", "Create", "Update"],
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "agent.modelplane.ai/v1alpha1",
                            "kind": "PoolObservation",
                            "metadata": {"name": pool},
                            "spec": {"poolName": pool},
                        },
                    },
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _observed_pool_observation(*, pool: str, status: dict) -> fnv1.Resource:
    """The observed Object for pool's PoolObservation, whose remote manifest carries the status the agent reported."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {
                        "kind": "ClusterProviderConfig",
                        "name": "test-cluster-cluster-kubeconfig-d0f89",
                    },
                    "readiness": {"policy": "SuccessfulCreate"},
                    "watch": True,
                    "managementPolicies": ["Observe", "Create", "Update"],
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "agent.modelplane.ai/v1alpha1",
                            "kind": "PoolObservation",
                            "metadata": {"name": pool},
                            "spec": {"poolName": pool},
                        },
                    },
                },
                "status": {
                    "atProvider": {
                        "manifest": {
                            "apiVersion": "agent.modelplane.ai/v1alpha1",
                            "kind": "PoolObservation",
                            "metadata": {"name": pool},
                            "spec": {"poolName": pool},
                            "status": status,
                        },
                    },
                },
            }
        ),
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


# Every want requires what the function reads, before it composes anything: the
# ModelReplicas and ModelRoutes labelled for test-cluster, across all
# namespaces; every ModelCache, since a cache fans out to many clusters and so
# can't be label-selected to one, leaving the function to filter by
# status.clusters[]; every InferenceGateway, since the cluster gateway accepts
# client certificates from each of their CAs, which is how an InferenceGateway
# proves itself; and the InferenceClass behind each node pool. A Provisioned
# cluster also requires its provider: the Composition for a ProvisionedCluster
# labelled with the group and kind of its cluster config.
#
# The Provisioned cases use a made-up acme provider, whose cluster config is
# an AcmeClusterConfig, so nothing here names a real cloud.
#
# gateway-test-cluster-09532.modelplane-system.svc.cluster.local is the internal
# name Modelplane derives for test-cluster's gateway, which
# compose-inference-gateway resolves.
COMPOSE_CASES = [
    ComposeCase(
        name="ExistingCluster",
        reason="An Existing cluster composes a ClusterProviderConfig and a ServingStack that manages its device stacks from its kubeconfig Secret.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    ComposeCase(
        name="ExistingIdentityAndCache",
        reason="An Existing cluster threads its identity's type into the ClusterProviderConfig and the ServingStack's secrets, and relays its cache StorageClass to status.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(
                            secretRef=v1alpha1.SecretRef(name="my-kubeconfig"),
                            identitySecretRef=v1alpha1.IdentitySecretRef(
                                name="nebius-creds",
                                key="credentials.json",
                                type="NebiusServiceAccountCredentials",
                            ),
                            cache=v1alpha1.Cache(storageClassName="nfs-rwx"),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                    ],
                    cache={"storageClassName": "nfs-rwx"},
                    gateway=None,
                ),
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig",
                        identity={
                            "type": "NebiusServiceAccountCredentials",
                            "source": "Secret",
                            "secretRef": {
                                "namespace": "modelplane-system",
                                "name": "nebius-creds",
                                "key": "credentials.json",
                            },
                        },
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[
                            {"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"},
                            {
                                "type": "NebiusServiceAccountCredentials",
                                "name": "nebius-creds",
                                "key": "credentials.json",
                            },
                        ],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # Nothing on an Existing cluster says which drivers it can serve, so its
    # classes' drivers are never gated. A typo'd driver surfaces when its
    # ResourceClaim can't allocate. The Synthetic device is described for
    # scheduling and never claimed, so its driver isn't a device stack to
    # install, while the GPU's unset claim defaults to DRA.
    ComposeCase(
        name="ExistingAnyDriver",
        reason="An Existing cluster composes a class claiming a driver no provider declares, and stamps only its claimed drivers on the ServingStack.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="byo-pool", className="byo", nodeCount=1),
                    ],
                ),
            ),
            required_resources={
                "class-byo": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "InferenceClass",
                                    "metadata": {"name": "byo"},
                                    "spec": {
                                        "devices": [
                                            {"name": "gpu", "driver": "gpu.example.com", "deviceClassName": "g"},
                                            {"name": "fabric", "claim": "Synthetic", "driver": "fabric.example.com"},
                                        ],
                                    },
                                }
                            )
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "byo-pool",
                            "nodes": 1,
                            # The class's schema defaults each device's claim and count.
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.example.com",
                                    "deviceClassName": "g",
                                    "count": 1,
                                },
                                {"name": "fabric", "claim": "Synthetic", "driver": "fabric.example.com", "count": 1},
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-inferenceclass-byo": _input_usage(name="byo"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.example.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-byo": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="byo"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    ComposeCase(
        name="ExistingBackendReady",
        reason="Once its ServingStack is observed Ready with an address, an Existing cluster relays the address, reports its backend healthy, and composes a PoolObservation for each pool.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
                resources={
                    "serving-stack": _observed_serving_stack(gateway={"address": "34.55.100.10"}),
                },
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway={"address": "34.55.100.10"},
                ),
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "pool-observation-l4-pool": _pool_observation(pool="l4-pool"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    ComposeCase(
        name="ExistingCPUPoolObserved",
        reason="Once its ServingStack is Ready, an Existing cluster composes a PoolObservation for its CPU pool as well as its GPU pool, and publishes the agent's observation of the CPU pool.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                        v1alpha1.NodePool(name="cpu-pool", className="cpu", nodeCount=1, maxNodeCount=2),
                    ],
                ),
                resources={
                    "serving-stack": _observed_serving_stack(gateway={"address": "34.55.100.10"}),
                    # The agent counts the CPU pool's node, tainted
                    # modelplane.ai/cpu, as schedulable.
                    "pool-observation-cpu-pool": _observed_pool_observation(
                        pool="cpu-pool",
                        status={
                            "observedGeneration": 1,
                            "conditions": [
                                {
                                    "type": "Healthy",
                                    "status": "True",
                                    "reason": "PoolHealthy",
                                    "message": "",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                    "observedGeneration": 1,
                                },
                            ],
                            "nodes": {"total": 1, "ready": 1, "schedulable": 1},
                            "unschedulablePods": 0,
                            "devices": [
                                {"deviceClassName": "dra.cpu", "nodes": 1, "allocatable": 1, "allocated": 0},
                            ],
                            "nodesByFreeDevices": [{"freeDevices": {"dra.cpu": 1}, "nodes": 1}],
                        },
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class=None)],
                ),
                "class-cpu": fnv1.Resources(
                    items=[_cpu_inference_class(name="cpu", cpus="6", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                        {
                            "name": "cpu-pool",
                            "nodes": 2,
                            "observedNodes": 1,
                            "nodesByFreeDevices": [{"freeDevices": {"dra.cpu": 1}, "nodes": 1}],
                            "devices": [
                                {
                                    "name": "cpu",
                                    "claim": "DRA",
                                    "driver": "dra.cpu",
                                    "deviceClassName": "dra.cpu",
                                    "count": 1,
                                    "capacity": {"cpu": {"value": "6"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway={"address": "34.55.100.10"},
                ),
                resources={
                    "usage-inferenceclass-cpu": _input_usage(name="cpu"),
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["dra.cpu", "gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "pool-observation-l4-pool": _pool_observation(pool="l4-pool"),
                    "pool-observation-cpu-pool": _pool_observation(pool="cpu-pool"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "class-cpu": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="cpu"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # The PoolObservation status is what the agent reported for a pool of one
    # node with four L4s, three of them allocated.
    ComposeCase(
        name="ExistingPoolObserved",
        reason="Once the agent reports on a pool, the cluster publishes the nodes the pool has, how many devices are free on them, and when the agent last saw a claim allocated on them.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="gpu-l4x4-a", className="gpu-l4x4", nodeCount=1, maxNodeCount=3),
                    ],
                ),
                resources={
                    "serving-stack": _observed_serving_stack(gateway={"address": "34.55.100.10"}),
                    "pool-observation-gpu-l4x4-a": _observed_pool_observation(
                        pool="gpu-l4x4-a",
                        status={
                            "observedGeneration": 1,
                            "conditions": [
                                {
                                    "type": "Healthy",
                                    "status": "True",
                                    "reason": "PoolHealthy",
                                    "message": "",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                    "observedGeneration": 1,
                                },
                                {
                                    "type": "ScaleUpBlocked",
                                    "status": "False",
                                    "reason": "NoUnschedulablePods",
                                    "message": "",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                    "observedGeneration": 1,
                                },
                            ],
                            "nodes": {"total": 1, "ready": 1, "schedulable": 1},
                            "unschedulablePods": 0,
                            "devices": [
                                {"deviceClassName": "gpu.nvidia.com", "nodes": 1, "allocatable": 4, "allocated": 3},
                            ],
                            "nodesByFreeDevices": [{"freeDevices": {"gpu.nvidia.com": 1}, "nodes": 1}],
                            "lastAllocationTime": "2026-10-20T10:05:40.318204Z",
                        },
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4x4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4x4", count=4, memory="23034Mi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "gpu-l4x4-a",
                            "nodes": 3,
                            "observedNodes": 1,
                            "nodesByFreeDevices": [{"freeDevices": {"gpu.nvidia.com": 1}, "nodes": 1}],
                            "lastAllocationTime": "2026-10-20T10:05:40.318204Z",
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 4,
                                    "capacity": {"memory": {"value": "23034Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway={"address": "34.55.100.10"},
                ),
                resources={
                    "usage-inferenceclass-gpu-l4x4": _input_usage(name="gpu-l4x4"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "pool-observation-gpu-l4x4-a": _pool_observation(pool="gpu-l4x4-a"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4x4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4x4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    ComposeCase(
        name="ExistingPoolNotObservable",
        reason="A pool the agent reports it can't observe publishes no observation, rather than an observation of an empty pool.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="gpu-l4x4-a", className="gpu-l4x4", nodeCount=1, maxNodeCount=3),
                    ],
                ),
                resources={
                    "serving-stack": _observed_serving_stack(gateway={"address": "34.55.100.10"}),
                    "pool-observation-gpu-l4x4-a": _observed_pool_observation(
                        pool="gpu-l4x4-a",
                        status={
                            "observedGeneration": 1,
                            "conditions": [
                                {
                                    "type": "Healthy",
                                    "status": "Unknown",
                                    "reason": "ObservationFailed",
                                    "message": "cannot observe pool: boom",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                    "observedGeneration": 1,
                                },
                                {
                                    "type": "ScaleUpBlocked",
                                    "status": "Unknown",
                                    "reason": "ObservationFailed",
                                    "message": "cannot observe pool: boom",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                    "observedGeneration": 1,
                                },
                            ],
                            "nodes": {"total": 0, "ready": 0, "schedulable": 0},
                            "unschedulablePods": 0,
                            "lastAllocationTime": "2026-10-20T10:05:40.318204Z",
                        },
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4x4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4x4", count=4, memory="23034Mi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "gpu-l4x4-a",
                            "nodes": 3,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 4,
                                    "capacity": {"memory": {"value": "23034Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway={"address": "34.55.100.10"},
                ),
                resources={
                    "usage-inferenceclass-gpu-l4x4": _input_usage(name="gpu-l4x4"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "pool-observation-gpu-l4x4-a": _pool_observation(pool="gpu-l4x4-a"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4x4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4x4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # One of the pool's two nodes is cordoned, so the histogram counts only
    # the other.
    ComposeCase(
        name="ExistingPoolUnhealthy",
        reason="A pool the agent observes as unhealthy, but not unobservable, still publishes its observation.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="gpu-l4x4-a", className="gpu-l4x4", nodeCount=2, maxNodeCount=3),
                    ],
                ),
                resources={
                    "serving-stack": _observed_serving_stack(gateway={"address": "34.55.100.10"}),
                    "pool-observation-gpu-l4x4-a": _observed_pool_observation(
                        pool="gpu-l4x4-a",
                        status={
                            "observedGeneration": 1,
                            "conditions": [
                                {
                                    "type": "Healthy",
                                    "status": "False",
                                    "reason": "NodesCordoned",
                                    "message": "1 of 2 nodes cordoned",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                    "observedGeneration": 1,
                                },
                                {
                                    "type": "ScaleUpBlocked",
                                    "status": "False",
                                    "reason": "NoUnschedulablePods",
                                    "message": "",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                    "observedGeneration": 1,
                                },
                            ],
                            "nodes": {"total": 2, "ready": 2, "schedulable": 1},
                            "unschedulablePods": 0,
                            "devices": [
                                {"deviceClassName": "gpu.nvidia.com", "nodes": 2, "allocatable": 4, "allocated": 3},
                            ],
                            "nodesByFreeDevices": [{"freeDevices": {"gpu.nvidia.com": 1}, "nodes": 1}],
                            "lastAllocationTime": "2026-10-20T10:05:40.318204Z",
                        },
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4x4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4x4", count=4, memory="23034Mi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "gpu-l4x4-a",
                            "nodes": 3,
                            "observedNodes": 2,
                            "nodesByFreeDevices": [{"freeDevices": {"gpu.nvidia.com": 1}, "nodes": 1}],
                            "lastAllocationTime": "2026-10-20T10:05:40.318204Z",
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 4,
                                    "capacity": {"memory": {"value": "23034Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway={"address": "34.55.100.10"},
                ),
                resources={
                    "usage-inferenceclass-gpu-l4x4": _input_usage(name="gpu-l4x4"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "pool-observation-gpu-l4x4-a": _pool_observation(pool="gpu-l4x4-a"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4x4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4x4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # The agent's schema requires each bucket to count at least one node.
    ComposeCase(
        name="ExistingPoolObservationInvalid",
        reason="A pool whose observation doesn't match the agent's schema publishes no observation, rather than failing the whole cluster.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="gpu-l4x4-a", className="gpu-l4x4", nodeCount=1, maxNodeCount=3),
                    ],
                ),
                resources={
                    "serving-stack": _observed_serving_stack(gateway={"address": "34.55.100.10"}),
                    "pool-observation-gpu-l4x4-a": _observed_pool_observation(
                        pool="gpu-l4x4-a",
                        status={
                            "observedGeneration": 1,
                            "conditions": [
                                {
                                    "type": "Healthy",
                                    "status": "True",
                                    "reason": "PoolHealthy",
                                    "message": "",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                    "observedGeneration": 1,
                                },
                                {
                                    "type": "ScaleUpBlocked",
                                    "status": "False",
                                    "reason": "NoUnschedulablePods",
                                    "message": "",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                    "observedGeneration": 1,
                                },
                            ],
                            "nodes": {"total": 1, "ready": 1, "schedulable": 1},
                            "unschedulablePods": 0,
                            "devices": [
                                {"deviceClassName": "gpu.nvidia.com", "nodes": 1, "allocatable": 4, "allocated": 3},
                            ],
                            "nodesByFreeDevices": [{"freeDevices": {"gpu.nvidia.com": 1}, "nodes": 0}],
                            "lastAllocationTime": "2026-10-20T10:05:40.318204Z",
                        },
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4x4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4x4", count=4, memory="23034Mi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "gpu-l4x4-a",
                            "nodes": 3,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 4,
                                    "capacity": {"memory": {"value": "23034Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway={"address": "34.55.100.10"},
                ),
                resources={
                    "usage-inferenceclass-gpu-l4x4": _input_usage(name="gpu-l4x4"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "pool-observation-gpu-l4x4-a": _pool_observation(pool="gpu-l4x4-a"),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="Ignoring the cluster agent's observation of pool gpu-l4x4-a: it does not match the "
                    "agent's schema (1 problems), so the pool is counted in whole nodes.",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4x4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4x4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # The agent hasn't seen a claim allocated on the pool, so it reports no
    # allocation time.
    ComposeCase(
        name="ExistingPoolObservationKept",
        reason="A PoolObservation that already exists stays composed when the ServingStack stops being Ready, so the agent's reports keep flowing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="gpu-l4x4-a", className="gpu-l4x4", nodeCount=1, maxNodeCount=3),
                    ],
                ),
                resources={
                    "serving-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "ServingStack",
                                "metadata": {"name": "test-cluster-serving-stack-fd00b"},
                                "status": {
                                    "conditions": [{"type": "Ready", "status": "False"}],
                                    "gateway": {"address": "34.55.100.10"},
                                },
                            }
                        ),
                    ),
                    "pool-observation-gpu-l4x4-a": _observed_pool_observation(
                        pool="gpu-l4x4-a",
                        status={
                            "observedGeneration": 1,
                            "conditions": [
                                {
                                    "type": "Healthy",
                                    "status": "True",
                                    "reason": "PoolHealthy",
                                    "message": "",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                    "observedGeneration": 1,
                                },
                                {
                                    "type": "ScaleUpBlocked",
                                    "status": "False",
                                    "reason": "NoUnschedulablePods",
                                    "message": "",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                    "observedGeneration": 1,
                                },
                            ],
                            "nodes": {"total": 1, "ready": 1, "schedulable": 1},
                            "unschedulablePods": 0,
                            "devices": [
                                {"deviceClassName": "gpu.nvidia.com", "nodes": 1, "allocatable": 4, "allocated": 0},
                            ],
                            "nodesByFreeDevices": [{"freeDevices": {"gpu.nvidia.com": 4}, "nodes": 1}],
                        },
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4x4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4x4", count=4, memory="23034Mi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "gpu-l4x4-a",
                            "nodes": 3,
                            "observedNodes": 1,
                            "nodesByFreeDevices": [{"freeDevices": {"gpu.nvidia.com": 4}, "nodes": 1}],
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 4,
                                    "capacity": {"memory": {"value": "23034Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway={"address": "34.55.100.10"},
                ),
                resources={
                    "usage-inferenceclass-gpu-l4x4": _input_usage(name="gpu-l4x4"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "pool-observation-gpu-l4x4-a": _pool_observation(pool="gpu-l4x4-a"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4x4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4x4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    ComposeCase(
        name="ReplicasRoutesAndCaches",
        reason="ModelReplicas, ModelRoutes and ModelCaches across namespaces compose one ClusterUsage naming each kind, and mirror the namespaces that land here.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "model-replicas": fnv1.Resources(
                    items=[_model_replica(namespace="team-a"), _model_replica(namespace="team-b")],
                ),
                "model-routes": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "ModelRoute",
                                    "metadata": {
                                        "name": "svc-eu",
                                        "namespace": "team-b",
                                        "labels": {"modelplane.ai/cluster": "test-cluster"},
                                    },
                                }
                            )
                        ),
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "ModelRoute",
                                    "metadata": {
                                        "name": "svc-eu",
                                        "namespace": "team-c",
                                        "labels": {"modelplane.ai/cluster": "test-cluster"},
                                    },
                                }
                            )
                        ),
                    ],
                ),
                "model-caches": fnv1.Resources(
                    items=[
                        _model_cache(name="qwen", namespace="team-d", cluster="test-cluster"),
                        _model_cache(name="kimi", namespace="team-e", cluster="other-cluster"),
                    ],
                ),
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "usage-replicas": _guard_cluster_usage(
                        reason="ModelReplicas, ModelRoutes and ModelCaches use this InferenceCluster"
                    ),
                    "namespace-team-a": _namespace_object(team="team-a", name="mp-team-a-bd964"),
                    "namespace-team-b": _namespace_object(team="team-b", name="mp-team-b-6bd62"),
                    "namespace-team-c": _namespace_object(team="team-c", name="mp-team-c-d79d9"),
                    "namespace-team-d": _namespace_object(team="team-d", name="mp-team-d-c2383"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # A ModelRoute composes its routing Objects through the cluster's
    # ClusterProviderConfig, so it needs the cluster as much as a replica does.
    ComposeCase(
        name="RouteOnCluster",
        reason="A ModelRoute on the cluster, with no replica there, composes the guard and mirrors its team's namespace.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "model-routes": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "ModelRoute",
                                    "metadata": {
                                        "name": "svc-eu",
                                        "namespace": "team-c",
                                        "labels": {"modelplane.ai/cluster": "test-cluster"},
                                    },
                                }
                            )
                        )
                    ]
                ),
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "usage-replicas": _guard_cluster_usage(reason="ModelRoutes use this InferenceCluster"),
                    "namespace-team-c": _namespace_object(team="team-c", name="mp-team-c-d79d9"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # A ModelCache composes its PVC through the cluster's ClusterProviderConfig,
    # so it needs the cluster as much as a replica does.
    ComposeCase(
        name="CacheOnCluster",
        reason="A ModelCache staging onto the cluster, with no replica there, composes the guard and mirrors its team's namespace.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "model-caches": fnv1.Resources(
                    items=[_model_cache(name="qwen", namespace="team-d", cluster="test-cluster")]
                ),
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "usage-replicas": _guard_cluster_usage(reason="ModelCaches use this InferenceCluster"),
                    "namespace-team-d": _namespace_object(team="team-d", name="mp-team-d-c2383"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # An InferenceGateway composes its Gateway and routing Objects through the
    # cluster's ClusterProviderConfig just as a replica does. It is cluster
    # scoped, so it has no namespace to mirror.
    ComposeCase(
        name="GatewayOnCluster",
        reason="An InferenceGateway on the cluster composes the guard but mirrors no namespace.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[_inference_gateway(name="public", cluster="test-cluster", status=None)]
                ),
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "usage-replicas": _guard_cluster_usage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # This is the teardown transition - the last user is gone, so the function
    # stops composing the guard. The replica and route requirements are empty
    # but present, as Crossplane returns them when a selector matches nothing.
    ComposeCase(
        name="NothingOnCluster",
        reason="With only a ModelCache and an InferenceGateway on another cluster, the cluster composes no ClusterUsage and is deletable.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "model-replicas": fnv1.Resources(),
                "model-routes": fnv1.Resources(),
                "model-caches": fnv1.Resources(
                    items=[_model_cache(name="kimi", namespace="team-e", cluster="other-cluster")]
                ),
                "gateways": fnv1.Resources(
                    items=[_inference_gateway(name="elsewhere", cluster="other-cluster", status=None)]
                ),
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # A missing InferenceClass fails the reconcile, so Crossplane applies none
    # of its desired state and a running cluster isn't deleted for being
    # missing from it.
    ComposeCase(
        name="ClassUnresolved",
        reason="With its class unresolved, the function fails the reconcile, reporting the missing class on ClusterReady.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "model-replicas": fnv1.Resources(items=[_model_replica(namespace="team-a")]),
                # Crossplane fetched the class, and it doesn't exist.
                "class-gpu-l4": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="ModelReplicas use this InferenceCluster"),
                    "namespace-team-a": _namespace_object(team="team-a", name="mp-team-a-bd964"),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_FATAL, message="Waiting for InferenceClasses: gpu-l4")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForClasses",
                    message="Waiting for InferenceClasses: gpu-l4",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="ClassNotYetFetched",
        reason="Before Crossplane has fetched its class, the function asks for it and waits, without failing the reconcile.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "model-replicas": fnv1.Resources(items=[_model_replica(namespace="team-a")]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="ModelReplicas use this InferenceCluster"),
                    "namespace-team-a": _namespace_object(team="team-a", name="mp-team-a-bd964"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
        ),
    ),
    # The hostname gate, which is what keeps a cluster off the schedule until
    # traffic to it is mutually authenticated in both directions. These cases
    # observe an existing cluster's ServingStack ready, with whatever it and the
    # fleet's InferenceGateways have published so far. The gateway name is
    # Modelplane's own, so nothing configures it. An InferenceGateway running on
    # test-cluster also composes the deletion guard.
    #
    # This cluster's CA lets an InferenceGateway tell it reached the right
    # cluster, and an InferenceGateway CA makes the cluster gateway demand a
    # client certificate.
    ComposeCase(
        name="MutuallyAuthenticated",
        reason="With an address, this cluster's CA and an InferenceGateway CA all published, the cluster publishes its gateway hostname.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=None,
                ),
                resources={
                    "serving-stack": _observed_serving_stack(
                        gateway={"address": "34.55.100.10", "caCertificate": "cluster-ca"}
                    ),
                },
            ),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="fleet-0", cluster="test-cluster", status={"clientCACertificate": "fleet-ca"}
                        )
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[],
                    cache=None,
                    gateway={
                        "address": "34.55.100.10",
                        "caCertificate": "cluster-ca",
                        "hostname": "gateway-test-cluster-09532.modelplane-system.svc.cluster.local",
                    },
                ),
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=[],
                        client_cas=[{"name": "fleet-0", "certificate": "fleet-ca"}],
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # The case that matters: the cluster gateway only demands a client
    # certificate when it has a CA to check against, and with none it serves no
    # Gateway at all. Publishing the hostname anyway would make the cluster
    # schedulable when nothing is listening on it, so every request routed there
    # would be stranded.
    ComposeCase(
        name="NoGatewayCA",
        reason="With no InferenceGateway CA published, the cluster publishes its address and CA but no hostname.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=None,
                ),
                resources={
                    "serving-stack": _observed_serving_stack(
                        gateway={"address": "34.55.100.10", "caCertificate": "cluster-ca"}
                    ),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[], cache=None, gateway={"address": "34.55.100.10", "caCertificate": "cluster-ca"}
                ),
                resources={
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=[],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # Without this cluster's CA an InferenceGateway can't validate the cluster
    # gateway it reaches, so it would have to fall back to the public trust
    # store.
    ComposeCase(
        name="NoClusterCA",
        reason="Without this cluster's CA, the cluster publishes its address but no hostname.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=None,
                ),
                resources={
                    "serving-stack": _observed_serving_stack(gateway={"address": "34.55.100.10"}),
                },
            ),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="fleet-0", cluster="test-cluster", status={"clientCACertificate": "fleet-ca"}
                        )
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[], cache=None, gateway={"address": "34.55.100.10"}
                ),
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=[],
                        client_cas=[{"name": "fleet-0", "certificate": "fleet-ca"}],
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # A hostname that resolves to nothing strands every request routed to it,
    # and the CA is republished from the same status.
    ComposeCase(
        name="NoAddress",
        reason="Before its ServingStack publishes an address, the cluster publishes no gateway status, not even its CA.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=None,
                ),
                resources={
                    "serving-stack": _observed_serving_stack(gateway={"caCertificate": "cluster-ca"}),
                },
            ),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="fleet-0", cluster="test-cluster", status={"clientCACertificate": "fleet-ca"}
                        )
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(schedulable_pools=[], cache=None, gateway=None),
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=[],
                        client_cas=[{"name": "fleet-0", "certificate": "fleet-ca"}],
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # Any InferenceGateway may forward to this cluster, and these CAs are what
    # switches the cluster gateway's mTLS listener on. One that hasn't published
    # a CA yet is left out rather than holding the others back, and the list is
    # sorted so it doesn't churn.
    ComposeCase(
        name="ManyGatewayCAs",
        reason="The ServingStack accepts the CA of every InferenceGateway that has published one, on any cluster, sorted by name.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=None,
                ),
                resources={
                    "serving-stack": _observed_serving_stack(
                        gateway={"address": "34.55.100.10", "caCertificate": "cluster-ca"}
                    ),
                },
            ),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="fleet-0", cluster="test-cluster", status={"clientCACertificate": "fleet-0-ca"}
                        ),
                        _inference_gateway(
                            name="fleet-1", cluster="test-cluster", status={"clientCACertificate": "fleet-1-ca"}
                        ),
                        _inference_gateway(name="aaa", cluster="elsewhere", status={"clientCACertificate": "aaa-ca"}),
                        _inference_gateway(name="pending", cluster="elsewhere", status={}),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[],
                    cache=None,
                    gateway={
                        "address": "34.55.100.10",
                        "caCertificate": "cluster-ca",
                        "hostname": "gateway-test-cluster-09532.modelplane-system.svc.cluster.local",
                    },
                ),
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="Managed",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        dra_drivers=[],
                        client_cas=[
                            {"name": "aaa", "certificate": "aaa-ca"},
                            {"name": "fleet-0", "certificate": "fleet-0-ca"},
                            {"name": "fleet-1", "certificate": "fleet-1-ca"},
                        ],
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # On the first call nothing required has been fetched, so the function
    # composes nothing for the cluster yet: Crossplane would discard it anyway.
    ComposeCase(
        name="ProvisionedRequirements",
        reason="On its first call, a Provisioned cluster requires its classes and its provider's Composition by the config's group and kind, and waits.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
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
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
        ),
    ),
    ComposeCase(
        name="ProviderNotYetFetched",
        reason="Before Crossplane has fetched its provider's Composition, a Provisioned cluster guards its classes and waits, without failing the reconcile.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class="l4")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
        ),
    ),
    ComposeCase(
        name="NoProvider",
        reason="With no installed provider provisioning from its config's kind, a Provisioned cluster fails the reconcile with NoProvider.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class="l4")],
                ),
                # Crossplane fetched the provider, and found none.
                "provider": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="No installed cluster provider provisions clusters from "
                    "AcmeClusterConfig.acme.example.org; install the provider that owns it",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoProvider",
                    message="No installed cluster provider provisions clusters from "
                    "AcmeClusterConfig.acme.example.org; install the provider that owns it",
                ),
            ],
        ),
    ),
    # Crossplane picks one of several matching Compositions at random and
    # never reselects, so the function refuses to choose.
    ComposeCase(
        name="ManyProviders",
        reason="With two Compositions provisioning from its config's kind, a Provisioned cluster fails the reconcile with ConflictingProviders, naming both.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class="l4")],
                ),
                "provider": fnv1.Resources(
                    items=[
                        _provider(
                            name="provisioned-acme-fork",
                            annotations={"infrastructure.modelplane.ai/dra-drivers": "gpu.nvidia.com"},
                            api_version="infrastructure.modelplane.ai/v1alpha1",
                        ),
                        _provider(
                            name="provisioned-acme",
                            annotations={"infrastructure.modelplane.ai/dra-drivers": "gpu.nvidia.com"},
                            api_version="infrastructure.modelplane.ai/v1alpha1",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="Several Compositions provision clusters from AcmeClusterConfig.acme.example.org "
                    "(provisioned-acme, provisioned-acme-fork); uninstall all but one provider",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ConflictingProviders",
                    message="Several Compositions provision clusters from AcmeClusterConfig.acme.example.org "
                    "(provisioned-acme, provisioned-acme-fork); uninstall all but one provider",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="OtherContractVersion",
        reason="A Composition labelled for its config's kind but for another version of the contract isn't one Crossplane would select, so a Provisioned cluster fails the reconcile with NoProvider.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class="l4")],
                ),
                "provider": fnv1.Resources(
                    items=[
                        _provider(
                            name="provisioned-acme",
                            annotations={"infrastructure.modelplane.ai/dra-drivers": "gpu.nvidia.com"},
                            api_version="infrastructure.modelplane.ai/v1beta1",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="No installed cluster provider provisions clusters from "
                    "AcmeClusterConfig.acme.example.org; install the provider that owns it",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoProvider",
                    message="No installed cluster provider provisions clusters from "
                    "AcmeClusterConfig.acme.example.org; install the provider that owns it",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="ClassWithoutNodeClass",
        reason="A Provisioned cluster's pool whose class names no node class fails the reconcile with InvalidNodePool.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class=None)],
                ),
                "provider": fnv1.Resources(
                    items=[
                        _provider(
                            name="provisioned-acme",
                            annotations={"infrastructure.modelplane.ai/dra-drivers": "gpu.nvidia.com"},
                            api_version="infrastructure.modelplane.ai/v1alpha1",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="Can't provision node pools: node pool l4-pool's InferenceClass gpu-l4 has no nodeClassRef",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="InvalidNodePool",
                    message="Can't provision node pools: node pool l4-pool's InferenceClass gpu-l4 has no nodeClassRef",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="UnsupportedByProvider",
        reason="Pools asking for zones or a reservation from a provider that declares neither capability fail the reconcile with UnsupportedByProvider, naming every pool.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-a", className="gpu-l4", nodeCount=2, zones=["acme-east-1a"]),
                        v1alpha1.NodePool(
                            name="l4-b",
                            className="gpu-l4",
                            nodeCount=2,
                            zones=["acme-east-1b"],
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="acme.example.org/v1alpha1",
                                kind="AcmeCapacityReservation",
                                name="l4-reserved",
                            ),
                        ),
                    ],
                ),
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class="l4")],
                ),
                "provider": fnv1.Resources(
                    items=[
                        _provider(
                            name="provisioned-acme",
                            annotations={"infrastructure.modelplane.ai/dra-drivers": "gpu.nvidia.com"},
                            api_version="infrastructure.modelplane.ai/v1alpha1",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="node pool l4-a needs Zones; node pool l4-b needs Zones, CapacityReservations, which the "
                    "provider for AcmeClusterConfig.acme.example.org doesn't support (it supports none)",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="UnsupportedByProvider",
                    message="node pool l4-a needs Zones; node pool l4-b needs Zones, CapacityReservations, which the "
                    "provider for AcmeClusterConfig.acme.example.org doesn't support (it supports none)",
                ),
            ],
        ),
    ),
    # The devices gate can fire on a cluster that is already running - a shared
    # class edited to claim a driver the provider can't serve. Everything after
    # the gate composes the cluster, so a bare return would empty the desired
    # state and Crossplane would garbage-collect the ProvisionedCluster, the
    # ServingStack and their Usages. The fatal result is what stops that:
    # Crossplane returns before applying desired state or collecting anything,
    # and still applies the condition.
    ComposeCase(
        name="UnsupportedDevices",
        reason="Classes claiming DRA drivers the provider doesn't declare fail the reconcile with UnsupportedDevices, naming every class and the drivers the provider supports, dra.cpu among them.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="mi300x-pool", className="mi300x", nodeCount=1),
                        v1alpha1.NodePool(name="gaudi-pool", className="gaudi3", nodeCount=1),
                        v1alpha1.NodePool(name="cpu-pool", className="cpu", nodeCount=1),
                    ],
                ),
            ),
            required_resources={
                "class-mi300x": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "InferenceClass",
                                    "metadata": {"name": "mi300x"},
                                    "spec": {
                                        "devices": [
                                            {
                                                "name": "gpu",
                                                "claim": "DRA",
                                                "driver": "gpu.amd.com",
                                                "deviceClassName": "gpu.amd.com",
                                            },
                                        ],
                                        "nodeClassRef": {
                                            "apiVersion": "acme.example.org/v1alpha1",
                                            "kind": "AcmeNodeClass",
                                            "name": "mi300x",
                                        },
                                    },
                                }
                            )
                        ),
                    ],
                ),
                "class-gaudi3": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "InferenceClass",
                                    "metadata": {"name": "gaudi3"},
                                    "spec": {
                                        "devices": [
                                            {
                                                "name": "gpu",
                                                "claim": "DRA",
                                                "driver": "gpu.intel.com",
                                                "deviceClassName": "gpu.intel.com",
                                            },
                                        ],
                                        "nodeClassRef": {
                                            "apiVersion": "acme.example.org/v1alpha1",
                                            "kind": "AcmeNodeClass",
                                            "name": "gaudi3",
                                        },
                                    },
                                }
                            )
                        ),
                    ],
                ),
                "class-cpu": fnv1.Resources(
                    items=[_cpu_inference_class(name="cpu", cpus="6", node_class="cpu")],
                ),
                "provider": fnv1.Resources(
                    items=[
                        _provider(
                            name="provisioned-acme",
                            annotations={"infrastructure.modelplane.ai/dra-drivers": "gpu.nvidia.com"},
                            api_version="infrastructure.modelplane.ai/v1alpha1",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "usage-inferenceclass-cpu": _input_usage(name="cpu"),
                    "usage-inferenceclass-gaudi3": _input_usage(name="gaudi3"),
                    "usage-inferenceclass-mi300x": _input_usage(name="mi300x"),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="class gaudi3 claims gpu.intel.com; class mi300x claims gpu.amd.com; clusters from "
                    "AcmeClusterConfig.acme.example.org support dra.cpu, gpu.nvidia.com",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-cpu": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="cpu"
                    ),
                    "class-gaudi3": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gaudi3"
                    ),
                    "class-mi300x": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="mi300x"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="UnsupportedDevices",
                    message="class gaudi3 claims gpu.intel.com; class mi300x claims gpu.amd.com; clusters from "
                    "AcmeClusterConfig.acme.example.org support dra.cpu, gpu.nvidia.com",
                ),
            ],
        ),
    ),
    # The CPU class claims only dra.cpu, which every provider supports though
    # this one doesn't declare it, and which makes its pool a CPU pool.
    ComposeCase(
        name="ProvisionedComposed",
        reason="A Provisioned cluster composes a ProvisionedCluster selecting its provider, with each pool's node class, role, sizes, zones and reservation, and waits for the provider to report how to reach it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="l4-pool",
                            className="gpu-l4",
                            nodeCount=2,
                            minNodeCount=1,
                            maxNodeCount=4,
                            zones=["acme-east-1a", "acme-east-1b"],
                            reservationRef=v1alpha1.ReservationRef(
                                apiVersion="acme.example.org/v1alpha1",
                                kind="AcmeCapacityReservation",
                                name="l4-reserved",
                            ),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", className="cpu", nodeCount=1),
                    ],
                ),
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class="l4")],
                ),
                "class-cpu": fnv1.Resources(
                    items=[_cpu_inference_class(name="cpu", cpus="6", node_class="cpu")],
                ),
                "provider": fnv1.Resources(
                    items=[
                        _provider(
                            name="provisioned-acme",
                            annotations={
                                "infrastructure.modelplane.ai/capabilities": "Zones, CapacityReservations",
                                "infrastructure.modelplane.ai/dra-drivers": "gpu.nvidia.com",
                            },
                            api_version="infrastructure.modelplane.ai/v1alpha1",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                        {
                            "name": "cpu-pool",
                            "nodes": 1,
                            "devices": [
                                {
                                    "name": "cpu",
                                    "claim": "DRA",
                                    "driver": "dra.cpu",
                                    "deviceClassName": "dra.cpu",
                                    "count": 1,
                                    "capacity": {"cpu": {"value": "6"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-inferenceclass-cpu": _input_usage(name="cpu"),
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "provisioned-cluster": _provisioned_cluster(
                        node_pools=[
                            {
                                "name": "l4-pool",
                                "nodeClassRef": {
                                    "apiVersion": "acme.example.org/v1alpha1",
                                    "kind": "AcmeNodeClass",
                                    "name": "l4",
                                },
                                "role": "Accelerator",
                                "nodeCount": 2,
                                "minNodeCount": 1,
                                "maxNodeCount": 4,
                                "zones": ["acme-east-1a", "acme-east-1b"],
                                "reservationRef": {
                                    "apiVersion": "acme.example.org/v1alpha1",
                                    "kind": "AcmeCapacityReservation",
                                    "name": "l4-reserved",
                                },
                            },
                            {
                                "name": "cpu-pool",
                                "nodeClassRef": {
                                    "apiVersion": "acme.example.org/v1alpha1",
                                    "kind": "AcmeNodeClass",
                                    "name": "cpu",
                                },
                                "role": "CPU",
                                "nodeCount": 1,
                            },
                        ],
                        dra_drivers=["dra.cpu", "gpu.nvidia.com"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-cpu": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="cpu"
                    ),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    # The ServingStack doesn't wait for the ProvisionedCluster to be Ready,
    # because the provider's device stack depends on the serving stack's
    # cert-manager. The identity secret is in the namespace the provider says,
    # not modelplane-system.
    ComposeCase(
        name="ProvisionedReachable",
        reason="Once the provider publishes a kubeconfig, a Provisioned cluster composes a ClusterProviderConfig with its identity, a ServingStack relaying the provider's gateway and telemetry needs, and a Usage holding the ProvisionedCluster.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
                resources={
                    "provisioned-cluster": _observed_provisioned_cluster(
                        status={
                            "conditions": [
                                {
                                    "type": "Ready",
                                    "status": "False",
                                    "reason": "Creating",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                },
                            ],
                            "secrets": [
                                {
                                    "type": "Kubeconfig",
                                    "name": "test-cluster-kubeconfig",
                                    "namespace": "modelplane-system",
                                    "key": "kubeconfig",
                                },
                                {
                                    "type": "GoogleApplicationCredentials",
                                    "name": "acme-credentials",
                                    "namespace": "crossplane-system",
                                    "key": "credentials.json",
                                },
                            ],
                            "gateway": {"service": {"annotations": {"acme.example.org/load-balancer-type": "public"}}},
                            "telemetry": {"ignoredNamespaces": ["acme-exporters"]},
                        },
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class="l4")],
                ),
                "provider": fnv1.Resources(
                    items=[
                        _provider(
                            name="provisioned-acme",
                            annotations={"infrastructure.modelplane.ai/dra-drivers": "gpu.nvidia.com"},
                            api_version="infrastructure.modelplane.ai/v1alpha1",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "provisioned-cluster": _provisioned_cluster(
                        node_pools=[
                            {
                                "name": "l4-pool",
                                "nodeClassRef": {
                                    "apiVersion": "acme.example.org/v1alpha1",
                                    "kind": "AcmeNodeClass",
                                    "name": "l4",
                                },
                                "role": "Accelerator",
                                "nodeCount": 2,
                                "maxNodeCount": 4,
                            },
                        ],
                        dra_drivers=["gpu.nvidia.com"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="test-cluster-kubeconfig",
                        identity={
                            "type": "GoogleApplicationCredentials",
                            "source": "Secret",
                            "secretRef": {
                                "namespace": "crossplane-system",
                                "name": "acme-credentials",
                                "key": "credentials.json",
                            },
                        },
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="External",
                        secrets=[
                            {
                                "type": "Kubeconfig",
                                "name": "test-cluster-kubeconfig",
                                "namespace": "modelplane-system",
                                "key": "kubeconfig",
                            },
                            {
                                "type": "GoogleApplicationCredentials",
                                "name": "acme-credentials",
                                "namespace": "crossplane-system",
                                "key": "credentials.json",
                            },
                        ],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service={"annotations": {"acme.example.org/load-balancer-type": "public"}},
                        telemetry={"ignoredNamespaces": ["acme-exporters"]},
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-provisioned-cluster-by-backend": _provisioned_usage(),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Cluster is reachable, composing its serving stack"),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    # Dropping the ServingStack from desired state would have Crossplane delete
    # it, uninstalling the serving stack from a running cluster, and dropping
    # what the provider relayed would change its gateways' Service and its
    # collector.
    ComposeCase(
        name="ProvisionedSecretsLost",
        reason="When the provider's status loses what it relayed, a Provisioned cluster keeps composing its ServingStack and ClusterProviderConfig from the observed ServingStack's secrets, gateway Service annotations and ignored namespaces.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
                resources={
                    "provisioned-cluster": _observed_provisioned_cluster(status={}),
                    "serving-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "ServingStack",
                                "metadata": {
                                    "name": "test-cluster-serving-stack-fd00b",
                                    "namespace": "modelplane-system",
                                },
                                "spec": {
                                    "deviceStacks": "External",
                                    "gateway": {
                                        "hostname": "gateway-test-cluster-09532.modelplane-system.svc.cluster.local",
                                        "service": {
                                            "annotations": {"acme.example.org/load-balancer-type": "public"},
                                        },
                                    },
                                    "telemetry": {"ignoredNamespaces": ["acme-exporters"]},
                                    "stack": "Standard",
                                    "secrets": [
                                        {
                                            "type": "Kubeconfig",
                                            "name": "test-cluster-kubeconfig",
                                            "namespace": "modelplane-system",
                                            "key": "kubeconfig",
                                        },
                                    ],
                                    "draDrivers": ["gpu.nvidia.com"],
                                },
                            }
                        ),
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class="l4")],
                ),
                "provider": fnv1.Resources(
                    items=[
                        _provider(
                            name="provisioned-acme",
                            annotations={"infrastructure.modelplane.ai/dra-drivers": "gpu.nvidia.com"},
                            api_version="infrastructure.modelplane.ai/v1alpha1",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "provisioned-cluster": _provisioned_cluster(
                        node_pools=[
                            {
                                "name": "l4-pool",
                                "nodeClassRef": {
                                    "apiVersion": "acme.example.org/v1alpha1",
                                    "kind": "AcmeNodeClass",
                                    "name": "l4",
                                },
                                "role": "Accelerator",
                                "nodeCount": 2,
                                "maxNodeCount": 4,
                            },
                        ],
                        dra_drivers=["gpu.nvidia.com"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="test-cluster-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="External",
                        secrets=[
                            {
                                "type": "Kubeconfig",
                                "name": "test-cluster-kubeconfig",
                                "namespace": "modelplane-system",
                                "key": "kubeconfig",
                            },
                        ],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service={"annotations": {"acme.example.org/load-balancer-type": "public"}},
                        telemetry={"ignoredNamespaces": ["acme-exporters"]},
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-provisioned-cluster-by-backend": _provisioned_usage(),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    ComposeCase(
        name="ProvisionedReady",
        reason="Once the ProvisionedCluster is Ready, a Provisioned cluster marks it ready, reports ClusterReady, and relays the provider's cache StorageClass.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
                resources={
                    "provisioned-cluster": _observed_provisioned_cluster(
                        status={
                            "conditions": [
                                {
                                    "type": "Synced",
                                    "status": "True",
                                    "reason": "ReconcileSuccess",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                },
                                {
                                    "type": "Ready",
                                    "status": "True",
                                    "reason": "Available",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                },
                            ],
                            "secrets": [
                                {
                                    "type": "Kubeconfig",
                                    "name": "test-cluster-kubeconfig",
                                    "namespace": "modelplane-system",
                                    "key": "kubeconfig",
                                },
                            ],
                            "cache": {"storageClassName": "acme-rwx"},
                        },
                    ),
                    "serving-stack": _observed_serving_stack(gateway={"address": "34.55.100.10"}),
                },
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class="l4")],
                ),
                "provider": fnv1.Resources(
                    items=[
                        _provider(
                            name="provisioned-acme",
                            annotations={"infrastructure.modelplane.ai/dra-drivers": "gpu.nvidia.com"},
                            api_version="infrastructure.modelplane.ai/v1alpha1",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                    ],
                    cache={"storageClassName": "acme-rwx"},
                    gateway={"address": "34.55.100.10"},
                ),
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "provisioned-cluster": _provisioned_cluster(
                        node_pools=[
                            {
                                "name": "l4-pool",
                                "nodeClassRef": {
                                    "apiVersion": "acme.example.org/v1alpha1",
                                    "kind": "AcmeNodeClass",
                                    "name": "l4",
                                },
                                "role": "Accelerator",
                                "nodeCount": 2,
                                "maxNodeCount": 4,
                            },
                        ],
                        dra_drivers=["gpu.nvidia.com"],
                        ready=fnv1.READY_TRUE,
                    ),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="test-cluster-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        device_stacks="External",
                        secrets=[
                            {
                                "type": "Kubeconfig",
                                "name": "test-cluster-kubeconfig",
                                "namespace": "modelplane-system",
                                "key": "kubeconfig",
                            },
                        ],
                        dra_drivers=["gpu.nvidia.com"],
                        client_cas=None,
                        service=None,
                        telemetry=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-provisioned-cluster-by-backend": _provisioned_usage(),
                    "pool-observation-l4-pool": _pool_observation(pool="l4-pool"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # A provider that can't provision what it was asked for fails its
    # ProvisionedCluster's reconcile, which Crossplane reports on Synced.
    ComposeCase(
        name="ProviderError",
        reason="When the ProvisionedCluster isn't Synced, a Provisioned cluster reports ClusterReady False with reason ProviderError and the provider's message.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Provisioned",
                        provisioned=v1alpha1.Provisioned(
                            configRef=v1alpha1.ConfigRef(
                                apiVersion="acme.example.org/v1alpha1", kind="AcmeClusterConfig", name="default"
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
                resources={
                    "provisioned-cluster": _observed_provisioned_cluster(
                        status={
                            "conditions": [
                                {
                                    "type": "Synced",
                                    "status": "False",
                                    "reason": "ReconcileError",
                                    "message": "AcmeNodeClass l4 offers no L4 GPUs in region acme-east-1",
                                    "lastTransitionTime": "2026-10-20T10:00:00Z",
                                },
                            ],
                        },
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[_inference_class(name="gpu-l4", count=1, memory="24Gi", node_class="l4")],
                ),
                "provider": fnv1.Resources(
                    items=[
                        _provider(
                            name="provisioned-acme",
                            annotations={"infrastructure.modelplane.ai/dra-drivers": "gpu.nvidia.com"},
                            api_version="infrastructure.modelplane.ai/v1alpha1",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    schedulable_pools=[
                        {
                            "name": "l4-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "24Gi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-inferenceclass-gpu-l4": _input_usage(name="gpu-l4"),
                    "provisioned-cluster": _provisioned_cluster(
                        node_pools=[
                            {
                                "name": "l4-pool",
                                "nodeClassRef": {
                                    "apiVersion": "acme.example.org/v1alpha1",
                                    "kind": "AcmeNodeClass",
                                    "name": "l4",
                                },
                                "role": "Accelerator",
                                "nodeCount": 2,
                                "maxNodeCount": 4,
                            },
                        ],
                        dra_drivers=["gpu.nvidia.com"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                    "provider": fnv1.ResourceSelector(
                        api_version="apiextensions.crossplane.io/v1",
                        kind="Composition",
                        match_labels=fnv1.MatchLabels(
                            labels={
                                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                                "infrastructure.modelplane.ai/config-kind": "AcmeClusterConfig",
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ProviderError",
                    message="AcmeNodeClass l4 offers no L4 GPUs in region acme-east-1",
                ),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: ComposeCase) -> None:
    """RunFunction composes the resources an InferenceCluster needs."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason


# The derived gateway hostname doubles as an SNI and a certificate SAN, so two
# clusters must never derive the same one. These cases call _gateway_hostname
# directly, because through RunFunction each cluster name would need a whole
# compose case, renaming every resource the function names after the cluster.
GATEWAY_HOSTNAME_CASES = [
    # A dotted cluster name is a DNS-1123 subdomain, but the first segment of
    # the hostname has to be one DNS-1035 label.
    #
    # With DashedTwin, this pins that eu.example and eu-example hash
    # differently: the hash covers the raw cluster name, before dots become
    # dashes. Sharing one hostname, a cluster's Service would shadow the other's
    # under a certificate it accepts.
    GatewayHostnameCase(
        name="DottedName",
        reason="A dotted cluster name becomes a single DNS label, hashed before its dots become dashes.",
        cluster_name="eu.example",
        want="gateway-eu-example-ad4f6.modelplane-system.svc.cluster.local",
    ),
    GatewayHostnameCase(
        name="DashedTwin",
        reason="A dashed cluster name hashes to a gateway hostname distinct from its dotted twin's.",
        cluster_name="eu-example",
        want="gateway-eu-example-1a2b0.modelplane-system.svc.cluster.local",
    ),
]


@pytest.mark.parametrize("case", GATEWAY_HOSTNAME_CASES, ids=lambda case: case.name)
def test_gateway_hostname(case: GatewayHostnameCase) -> None:
    """_gateway_hostname derives a cluster's gateway hostname."""
    assert fn._gateway_hostname(case.cluster_name) == case.want, case.reason
