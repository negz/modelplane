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

"""Tests for composing a provider's device stack onto its cluster.

PROVIDER_CONFIGS_CASES covers the ProviderConfigs built from a cluster's
secrets, and COMPOSE_CASES what compose renders over a device stack XR's
reconciles. The SDK has no generated models, so the XR is written as a
dict.
"""

import dataclasses
import json
from typing import Any

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from google.protobuf import json_format, message
from modelplane_sdk import devicestack, stacks


@dataclasses.dataclass
class ProviderConfigsCase:
    """A test case for devicestack.provider_configs."""

    name: str
    reason: str
    secrets: list[dict[str, Any]]
    want: dict[str, dict[str, Any]]


@dataclasses.dataclass
class ComposeCase:
    """A test case for devicestack.compose."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    components: list[stacks.Component]
    want: fnv1.RunFunctionResponse


def _device_stack() -> fnv1.Resource:
    """The observed AcmeDeviceStack prod, for a cluster whose classes claim gpu.example.com."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "acme.example.org/v1alpha1",
                "kind": "AcmeDeviceStack",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {
                    "secrets": [{"type": "Kubeconfig", "name": "prod-kubeconfig", "key": "kubeconfig"}],
                    "draDrivers": ["gpu.example.com"],
                },
            }
        )
    )


def _driver() -> stacks.Chart:
    """The device stack's DRA driver chart, for gpu.example.com."""
    return stacks.Chart(
        key="dra-driver",
        release="mp-dra-driver",
        namespace="dra-driver",
        chart="dra-driver",
        repository="https://charts.example.com",
        version="1.0.0",
        dra_drivers=["gpu.example.com"],
    )


def _fabric() -> stacks.Chart:
    """The device stack's fabric chart, for net.example.com, which no class claims."""
    return stacks.Chart(
        key="fabric",
        release="mp-fabric",
        namespace="fabric",
        chart="fabric",
        repository="https://charts.example.com",
        version="1.0.0",
        dra_drivers=["net.example.com"],
    )


def _provider_config(*, api_version: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The desired ProviderConfig of api_version, reaching the cluster through its kubeconfig."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": api_version,
                "kind": "ProviderConfig",
                "metadata": {"name": "prod-cluster-0fab1"},
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {"name": "prod-kubeconfig", "namespace": "modelplane-system", "key": "kubeconfig"},
                    },
                },
            }
        ),
        ready=ready,
    )


def _driver_release(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The desired Release installing the DRA driver chart, without Delete."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-dra-driver"},
                    "labels": {
                        "modelplane.ai/resource": "dra-driver",
                        "modelplane.ai/usage-consumer": "dra-driver",
                    },
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                    "forProvider": {
                        "chart": {
                            "name": "dra-driver",
                            "repository": "https://charts.example.com",
                            "version": "1.0.0",
                        },
                        "namespace": "dra-driver",
                    },
                    "managementPolicies": ["Observe", "Create", "Update"],
                },
            }
        ),
        ready=ready,
    )


def _driver_usage() -> fnv1.Resource:
    """The Usage holding the helm ProviderConfig until the DRA driver's Release is gone, ready on arrival."""
    return fnv1.Resource(
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
                            "matchLabels": {"modelplane.ai/usage-consumer": "dra-driver"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


PROVIDER_CONFIGS_CASES = [
    ProviderConfigsCase(
        name="KubeconfigOnly",
        reason="With only a kubeconfig, both ProviderConfigs authenticate with it, in the XR's namespace.",
        secrets=[{"type": "Kubeconfig", "name": "prod-kubeconfig", "key": "kubeconfig"}],
        want={
            "provider-config-kubernetes": {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "ProviderConfig",
                "metadata": {"name": "prod-cluster"},
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {"name": "prod-kubeconfig", "namespace": "modelplane-system", "key": "kubeconfig"},
                    },
                },
            },
            "provider-config-helm": {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "ProviderConfig",
                "metadata": {"name": "prod-cluster"},
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {"name": "prod-kubeconfig", "namespace": "modelplane-system", "key": "kubeconfig"},
                    },
                },
            },
        },
    ),
    ProviderConfigsCase(
        name="Identity",
        reason="An identity secret becomes both ProviderConfigs' identity, of its type, in its own namespace.",
        secrets=[
            {"type": "Kubeconfig", "name": "prod-kubeconfig", "key": "kubeconfig"},
            {"type": "GoogleApplicationCredentials", "name": "prod-sa-key", "key": "private_key", "namespace": "acme"},
        ],
        want={
            "provider-config-kubernetes": {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "ProviderConfig",
                "metadata": {"name": "prod-cluster"},
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {"name": "prod-kubeconfig", "namespace": "modelplane-system", "key": "kubeconfig"},
                    },
                    "identity": {
                        "type": "GoogleApplicationCredentials",
                        "source": "Secret",
                        "secretRef": {"name": "prod-sa-key", "namespace": "acme", "key": "private_key"},
                    },
                },
            },
            "provider-config-helm": {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "ProviderConfig",
                "metadata": {"name": "prod-cluster"},
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {"name": "prod-kubeconfig", "namespace": "modelplane-system", "key": "kubeconfig"},
                    },
                    "identity": {
                        "type": "GoogleApplicationCredentials",
                        "source": "Secret",
                        "secretRef": {"name": "prod-sa-key", "namespace": "acme", "key": "private_key"},
                    },
                },
            },
        },
    ),
]


@pytest.mark.parametrize("case", PROVIDER_CONFIGS_CASES, ids=lambda case: case.name)
def test_provider_configs(case: ProviderConfigsCase) -> None:
    """provider_configs builds a provider-helm and a provider-kubernetes ProviderConfig from a cluster's secrets."""
    assert devicestack.provider_configs("prod-cluster", "modelplane-system", case.secrets) == case.want, case.reason


COMPOSE_CASES = [
    # Teardown Usages for depends_on edges reference nothing on the cluster,
    # but this stack has none.
    ComposeCase(
        name="FirstReconcile",
        reason="Before the ProviderConfigs are observed, only they are composed, unready.",
        req=fnv1.RunFunctionRequest(observed=fnv1.State(composite=_device_stack())),
        components=[_driver(), _fabric()],
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "provider-config-helm": _provider_config(
                        api_version="helm.m.crossplane.io/v1beta1", ready=fnv1.READY_UNSPECIFIED
                    ),
                }
            )
        ),
    ),
    ComposeCase(
        name="ProviderConfigsObserved",
        reason=(
            "Once the ProviderConfigs are observed they're ready, and the components the claimed drivers need are "
            "composed without Delete, each with a Usage of its ProviderConfig."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_device_stack(),
                resources={
                    "provider-config-kubernetes": fnv1.Resource(),
                    "provider-config-helm": fnv1.Resource(),
                },
            )
        ),
        components=[_driver(), _fabric()],
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1", ready=fnv1.READY_TRUE
                    ),
                    "provider-config-helm": _provider_config(
                        api_version="helm.m.crossplane.io/v1beta1", ready=fnv1.READY_TRUE
                    ),
                    "dra-driver": _driver_release(ready=fnv1.READY_UNSPECIFIED),
                    "usage-pc-dra-driver": _driver_usage(),
                }
            )
        ),
    ),
    ComposeCase(
        name="ComponentReady",
        reason="A component whose Release reports Ready is marked ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_device_stack(),
                resources={
                    "provider-config-kubernetes": fnv1.Resource(),
                    "provider-config-helm": fnv1.Resource(),
                    "dra-driver": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "status": {
                                    "conditions": [
                                        {
                                            "type": "Ready",
                                            "status": "True",
                                            "lastTransitionTime": "2026-01-01T00:00:00Z",
                                        }
                                    ]
                                }
                            }
                        )
                    ),
                },
            )
        ),
        components=[_driver(), _fabric()],
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1", ready=fnv1.READY_TRUE
                    ),
                    "provider-config-helm": _provider_config(
                        api_version="helm.m.crossplane.io/v1beta1", ready=fnv1.READY_TRUE
                    ),
                    "dra-driver": _driver_release(ready=fnv1.READY_TRUE),
                    "usage-pc-dra-driver": _driver_usage(),
                }
            )
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: ComposeCase) -> None:
    """compose renders a device stack XR's components onto its cluster."""
    rsp = fnv1.RunFunctionResponse()
    devicestack.compose(case.req, rsp, case.components)
    assert _to_dict(rsp) == _to_dict(case.want), case.reason


def test_compose_checks() -> None:
    """compose fails closed on a component list modelplane_sdk.stacks.check rejects."""
    with pytest.raises(ValueError, match=r"^AcmeDeviceStack: charts \['dra-driver'\] aren't released as mp-<chart>$"):
        devicestack.compose(
            fnv1.RunFunctionRequest(observed=fnv1.State(composite=_device_stack())),
            fnv1.RunFunctionResponse(),
            [
                stacks.Chart(
                    key="dra-driver",
                    release="dra-driver",
                    namespace="dra-driver",
                    chart="dra-driver",
                    repository="https://charts.example.com",
                    version="1.0.0",
                    dra_drivers=["gpu.example.com"],
                )
            ],
        )
