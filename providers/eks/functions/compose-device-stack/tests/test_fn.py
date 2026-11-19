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

"""Tests for the compose-device-stack function."""

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
from models.ai.modelplane.eks.eksdevicestack import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-device-stack."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(*, dra_drivers: list[v1alpha1.DraDriver]) -> fnv1.Resource:
    """The prod EKSDeviceStack XR, reaching its cluster through the prod kubeconfig Secret, claiming the given DRA drivers."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.EKSDeviceStack(
                metadata=metav1.ObjectMeta(name="prod", namespace="modelplane-system"),
                spec=v1alpha1.Spec(
                    secrets=[
                        v1alpha1.Secret(
                            type="Kubeconfig",
                            name="prod-kubeconfig-6c1d0",
                            namespace="modelplane-system",
                            key="kubeconfig",
                        ),
                    ],
                    draDrivers=dra_drivers,
                ),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        ),
    )


def _provider_config_kubernetes(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed provider-kubernetes ProviderConfig, reaching the cluster through its kubeconfig Secret."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "ProviderConfig",
                "metadata": {"name": "prod-cluster-0fab1"},
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {
                            "name": "prod-kubeconfig-6c1d0",
                            "namespace": "modelplane-system",
                            "key": "kubeconfig",
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _provider_config_helm(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed provider-helm ProviderConfig, reaching the cluster through its kubeconfig Secret."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "ProviderConfig",
                "metadata": {"name": "prod-cluster-0fab1"},
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {
                            "name": "prod-kubeconfig-6c1d0",
                            "namespace": "modelplane-system",
                            "key": "kubeconfig",
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _observed_provider_config(*, api_version: str) -> fnv1.Resource:
    """A ProviderConfig of the given apiVersion as observed, which has no status to report."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": api_version,
                "kind": "ProviderConfig",
                "metadata": {"name": "prod-cluster-0fab1", "namespace": "modelplane-system"},
            }
        ),
    )


def _observed_ready() -> fnv1.Resource:
    """A composed resource as observed, reporting Ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
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
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    Case(
        name="FirstPass",
        reason=(
            "With nothing observed, an EKSDeviceStack claiming gpu.nvidia.com composes its ProviderConfigs, not yet "
            "ready, and the Usages ordering its components' teardown, but no components until both are observed."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")]),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_UNSPECIFIED),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_UNSPECIFIED),
                    "usage-node-feature-discovery-by-gpu-operator": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "node-feature-discovery"},
                                        },
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "gpu-operator"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrole": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "gpu-operator"},
                                        },
                                    },
                                    "by": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "Object",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "dcgm-exporter-clusterrole"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrolebinding": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "gpu-operator"},
                                        },
                                    },
                                    "by": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "Object",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {
                                                "modelplane.ai/resource": "dcgm-exporter-clusterrolebinding"
                                            },
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "gpu-operator"},
                                        },
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "nvidia-dra-driver-gpu"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gpu-operator-by-nvsentinel": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "gpu-operator"},
                                        },
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "nvsentinel"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The GPU Operator waits on both NFD and cert-manager, and the rest of
    # the NVIDIA stack on the GPU Operator, so only the components that
    # depend on nothing are created yet. cert-manager is the serving
    # stack's, which the device stack only observes.
    Case(
        name="ProviderConfigsObserved",
        reason=(
            "Once both ProviderConfigs are observed, an EKSDeviceStack claiming gpu.nvidia.com observes the serving "
            "stack's cert-manager and composes the NFD and nodewright Releases without Delete, each held by a Usage "
            "of its ProviderConfig."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")]),
                resources={
                    "provider-config-kubernetes": _observed_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1"
                    ),
                    "provider-config-helm": _observed_provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_TRUE),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_TRUE),
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "cert-manager",
                                        "modelplane.ai/usage-consumer": "cert-manager",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe"],
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": (
                                            "has(object.status) && has(object.status.availableReplicas) && "
                                            "object.status.availableReplicas >= 1"
                                        ),
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "apps/v1",
                                            "kind": "Deployment",
                                            "metadata": {"name": "cert-manager-webhook", "namespace": "cert-manager"},
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "node-feature-discovery": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-node-feature-discovery"},
                                    "labels": {
                                        "modelplane.ai/resource": "node-feature-discovery",
                                        "modelplane.ai/usage-consumer": "node-feature-discovery",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "chart": {
                                            "name": "node-feature-discovery",
                                            "repository": "https://kubernetes-sigs.github.io/node-feature-discovery/charts",
                                            "version": "0.19.0",
                                        },
                                        "namespace": "node-feature-discovery",
                                        "wait": True,
                                        "waitTimeout": "10m",
                                        "values": {
                                            "gc": {"enable": True, "tolerations": []},
                                            "master": {"enable": True, "tolerations": []},
                                            "topologyUpdater": {
                                                "createCRDs": True,
                                                "enable": False,
                                                "kubeletStateDir": "",
                                                "resources": {
                                                    "limits": {"memory": "256Mi"},
                                                    "requests": {"cpu": "50m", "memory": "128Mi"},
                                                },
                                                "tolerations": [
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Exists",
                                                        "effect": "NoSchedule",
                                                    },
                                                ],
                                            },
                                            "worker": {
                                                "enable": True,
                                                "tolerations": [
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Exists",
                                                        "effect": "NoSchedule",
                                                    },
                                                ],
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "nodewright-operator": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-nodewright"},
                                    "labels": {
                                        "modelplane.ai/resource": "nodewright-operator",
                                        "modelplane.ai/usage-consumer": "nodewright-operator",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "chart": {
                                            "name": "nodewright",
                                            "repository": "oci://ghcr.io/nvidia/nodewright/charts",
                                            "version": "v0.19.0",
                                        },
                                        "namespace": "skyhook",
                                        "values": {
                                            "controllerManager": {
                                                "manager": {
                                                    "resources": {
                                                        "limits": {"cpu": "1000m", "memory": "4000Mi"},
                                                        "requests": {"cpu": "500m", "memory": "2000Mi"},
                                                    },
                                                },
                                                "tolerations": [],
                                            },
                                            "fullnameOverride": "skyhook-operator",
                                            "limitRange": {
                                                "default": {"cpu": "1", "memory": "1Gi"},
                                                "defaultRequest": {"cpu": "500m", "memory": "512Mi"},
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "usage-node-feature-discovery-by-gpu-operator": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "node-feature-discovery"},
                                        },
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "gpu-operator"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrole": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "gpu-operator"},
                                        },
                                    },
                                    "by": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "Object",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "dcgm-exporter-clusterrole"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrolebinding": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "gpu-operator"},
                                        },
                                    },
                                    "by": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "Object",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {
                                                "modelplane.ai/resource": "dcgm-exporter-clusterrolebinding"
                                            },
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "gpu-operator"},
                                        },
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "nvidia-dra-driver-gpu"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gpu-operator-by-nvsentinel": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "gpu-operator"},
                                        },
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/resource": "nvsentinel"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "Object",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "cert-manager"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-node-feature-discovery": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "node-feature-discovery"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-nodewright-operator": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "nodewright-operator"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # compose-eks-cluster installs the EFA DRA driver when a pool's node
    # class asks for the EFA fabric, so the device stack has nothing for it.
    Case(
        name="ExternalDriverOnly",
        reason="An EKSDeviceStack claiming only dra.net composes its ProviderConfigs and no components.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=[v1alpha1.DraDriver("dra.net")]),
                resources={
                    "provider-config-kubernetes": _observed_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1"
                    ),
                    "provider-config-helm": _observed_provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_TRUE),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction installs the device stacks an EKS cluster's InferenceClasses claim."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason


@dataclasses.dataclass
class ComposedResourceKeysCase:
    """A test case for the keys compose-device-stack composes its resources under."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: set[str]


# With every component observed Ready, each dependency gate is open and the
# whole stack renders. The cases compare keys rather than whole responses
# because the stack is generated from AICR recipes, and restating every
# Release's values would restate the generated file. COMPOSE_CASES covers
# how components render.
COMPOSED_RESOURCE_KEYS_CASES = [
    ComposedResourceKeysCase(
        name="AllReady",
        reason=(
            "With every component observed Ready, an EKSDeviceStack claiming gpu.nvidia.com composes the whole NVIDIA "
            "stack, the Usages ordering its teardown, and a Usage of its ProviderConfig by each component."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")]),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nodewright-operator": _observed_ready(),
                    "gpu-operator": _observed_ready(),
                    "dcgm-exporter-clusterrole": _observed_ready(),
                    "dcgm-exporter-clusterrolebinding": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "nvsentinel": _observed_ready(),
                },
            ),
        ),
        want={
            "provider-config-kubernetes",
            "provider-config-helm",
            # The serving stack's cert-manager, observed.
            "cert-manager",
            # The EKS stack, generated from AICR.
            "node-feature-discovery",
            "nodewright-operator",
            "gpu-operator",
            "dcgm-exporter-clusterrole",
            "dcgm-exporter-clusterrolebinding",
            "nvidia-dra-driver-gpu",
            "nvsentinel",
            # Teardown ordering. None holds cert-manager, which isn't the
            # device stack's to delete.
            "usage-node-feature-discovery-by-gpu-operator",
            "usage-gpu-operator-by-dcgm-exporter-clusterrole",
            "usage-gpu-operator-by-dcgm-exporter-clusterrolebinding",
            "usage-gpu-operator-by-nvidia-dra-driver-gpu",
            "usage-gpu-operator-by-nvsentinel",
            # Each component holds its ProviderConfig.
            "usage-pc-cert-manager",
            "usage-pc-node-feature-discovery",
            "usage-pc-nodewright-operator",
            "usage-pc-gpu-operator",
            "usage-pc-dcgm-exporter-clusterrole",
            "usage-pc-dcgm-exporter-clusterrolebinding",
            "usage-pc-nvidia-dra-driver-gpu",
            "usage-pc-nvsentinel",
        },
    ),
]


@pytest.mark.parametrize("case", COMPOSED_RESOURCE_KEYS_CASES, ids=lambda case: case.name)
def test_composed_resource_keys(case: ComposedResourceKeysCase) -> None:
    """RunFunction composes the EKS device stack under its pinned keys."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert set(got.desired.resources) == case.want, case.reason
