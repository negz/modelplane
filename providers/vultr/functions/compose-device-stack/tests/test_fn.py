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
from models.ai.modelplane.vultr.vultrdevicestack import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-device-stack."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(*, secrets: list[v1alpha1.Secret], dra_drivers: list[v1alpha1.DraDriver] | None) -> fnv1.Resource:
    """The prod VultrDeviceStack XR, with the given secrets, and DRA drivers unless they're None."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.VultrDeviceStack(
                metadata=metav1.ObjectMeta(name="prod", namespace="modelplane-system"),
                spec=v1alpha1.Spec(secrets=secrets, draDrivers=dra_drivers),
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


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    Case(
        name="FirstPass",
        reason=(
            "With nothing observed, a VultrDeviceStack composes only its ProviderConfigs, not yet ready, and no "
            "components until both are observed."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    secrets=[
                        v1alpha1.Secret(
                            type="Kubeconfig",
                            name="prod-kubeconfig-6c1d0",
                            namespace="modelplane-system",
                            key="kubeconfig",
                        ),
                    ],
                    dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_UNSPECIFIED),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="ProviderConfigsObserved",
        reason=(
            "Once both ProviderConfigs are observed, a VultrDeviceStack claiming gpu.nvidia.com composes the "
            "NVIDIA DRA driver Release without Delete, and a Usage of the ProviderConfig by it."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    secrets=[
                        v1alpha1.Secret(
                            type="Kubeconfig",
                            name="prod-kubeconfig-6c1d0",
                            namespace="modelplane-system",
                            key="kubeconfig",
                        ),
                    ],
                    dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")],
                ),
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
                    "nvidia-dra-driver-gpu": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-dra-driver-nvidia-gpu"},
                                    "labels": {
                                        "modelplane.ai/resource": "nvidia-dra-driver-gpu",
                                        "modelplane.ai/usage-consumer": "nvidia-dra-driver-gpu",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "chart": {
                                            "name": "dra-driver-nvidia-gpu",
                                            "repository": "oci://registry.k8s.io/dra-driver-nvidia/charts",
                                            "version": "0.5.0",
                                        },
                                        "namespace": "nvidia-dra-driver",
                                        "values": {
                                            "gpuResourcesEnabledOverride": True,
                                            "resources": {"computeDomains": {"enabled": False}},
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "usage-pc-nvidia-dra-driver-gpu": fnv1.Resource(
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
                                            "matchLabels": {"modelplane.ai/usage-consumer": "nvidia-dra-driver-gpu"},
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
    Case(
        name="NoGPUDriver",
        reason="A VultrDeviceStack claiming no DRA driver composes its ProviderConfigs and no components.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    secrets=[
                        v1alpha1.Secret(
                            type="Kubeconfig",
                            name="prod-kubeconfig-6c1d0",
                            namespace="modelplane-system",
                            key="kubeconfig",
                        ),
                    ],
                    dra_drivers=None,
                ),
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
    # The kubeconfig names no namespace, so it's read from the XR's.
    Case(
        name="IdentitySecret",
        reason=(
            "An identity secret in a namespace of its own is layered on both ProviderConfigs as their identity, "
            "beside the kubeconfig from the XR's namespace."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="prod-kubeconfig-6c1d0", key="kubeconfig"),
                        v1alpha1.Secret(
                            type="GoogleApplicationCredentials",
                            name="prod-identity",
                            namespace="crossplane-system",
                            key="credentials.json",
                        ),
                    ],
                    dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": fnv1.Resource(
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
                                    "identity": {
                                        "type": "GoogleApplicationCredentials",
                                        "source": "Secret",
                                        "secretRef": {
                                            "name": "prod-identity",
                                            "namespace": "crossplane-system",
                                            "key": "credentials.json",
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "provider-config-helm": fnv1.Resource(
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
                                    "identity": {
                                        "type": "GoogleApplicationCredentials",
                                        "source": "Secret",
                                        "secretRef": {
                                            "name": "prod-identity",
                                            "namespace": "crossplane-system",
                                            "key": "credentials.json",
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="AllReady",
        reason="With the NVIDIA DRA driver Release observed Ready, a VultrDeviceStack marks it and its ProviderConfigs ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    secrets=[
                        v1alpha1.Secret(
                            type="Kubeconfig",
                            name="prod-kubeconfig-6c1d0",
                            namespace="modelplane-system",
                            key="kubeconfig",
                        ),
                    ],
                    dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")],
                ),
                resources={
                    "provider-config-kubernetes": _observed_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1"
                    ),
                    "provider-config-helm": _observed_provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                    "nvidia-dra-driver-gpu": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
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
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_TRUE),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_TRUE),
                    "nvidia-dra-driver-gpu": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-dra-driver-nvidia-gpu"},
                                    "labels": {
                                        "modelplane.ai/resource": "nvidia-dra-driver-gpu",
                                        "modelplane.ai/usage-consumer": "nvidia-dra-driver-gpu",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "chart": {
                                            "name": "dra-driver-nvidia-gpu",
                                            "repository": "oci://registry.k8s.io/dra-driver-nvidia/charts",
                                            "version": "0.5.0",
                                        },
                                        "namespace": "nvidia-dra-driver",
                                        "values": {
                                            "gpuResourcesEnabledOverride": True,
                                            "resources": {"computeDomains": {"enabled": False}},
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-nvidia-dra-driver-gpu": fnv1.Resource(
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
                                            "matchLabels": {"modelplane.ai/usage-consumer": "nvidia-dra-driver-gpu"},
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
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction installs the device stacks a VKE cluster's InferenceClasses claim."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
