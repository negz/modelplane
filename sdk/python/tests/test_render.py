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

"""Tests for rendering a stack's components as composed resources.

COMPOSE_CASES covers what compose renders and what its gates withhold, and
COMPOSE_USAGES_CASES the Usages compose_usages derives from depends_on.
"""

import dataclasses
import json

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from google.protobuf import json_format, message
from modelplane_sdk import stacks
from modelplane_sdk.stacks import render


@dataclasses.dataclass
class ComposeCase:
    """A test case for render.compose."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    components: list[stacks.Component]
    provider_config: str
    gate: bool
    management_policies: list[str] | None
    # compose's return value, the keys it rendered. want is what it wrote.
    want_rendered: list[str]
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class ComposeUsagesCase:
    """A test case for render.compose_usages."""

    name: str
    reason: str
    components: list[stacks.Component]
    want: fnv1.RunFunctionResponse


def _operator() -> stacks.Chart:
    """An operator chart other components depend on."""
    return stacks.Chart(
        key="operator",
        release="mp-operator",
        namespace="operator-system",
        chart="operator",
        repository="https://charts.example.com",
        version="1.0.0",
        wait=True,
        values={"replicas": 2},
    )


def _config() -> stacks.Manifests:
    """Two ConfigMaps the operator reads, so the entry renders one Object per doc."""
    return stacks.Manifests(
        key="config",
        depends_on=["operator"],
        manifests=[
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a", "namespace": "operator-system"}},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "b", "namespace": "operator-system"}},
        ],
    )


def _cert_manager() -> stacks.Observed:
    """Another stack's cert-manager, observed until its webhook serves."""
    return stacks.Observed(
        key="cert-manager",
        manifest={
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "cert-manager-webhook", "namespace": "cert-manager"},
        },
        ready="object.status.availableReplicas >= 1",
    )


def _observed(*, ready: bool) -> fnv1.Resource:
    """An observed composed resource, reporting Ready if ready is set."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "status": {
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True" if ready else "False",
                            "lastTransitionTime": "2026-01-01T00:00:00Z",
                        }
                    ]
                }
            }
        )
    )


def _operator_release(*, management_policies: list[str] | None) -> fnv1.Resource:
    """The composed Release installing the operator chart, with management_policies if there are any."""
    spec: dict = {
        "providerConfigRef": {"kind": "ProviderConfig", "name": "cluster"},
        "forProvider": {
            "chart": {"name": "operator", "repository": "https://charts.example.com", "version": "1.0.0"},
            "namespace": "operator-system",
            "wait": True,
            "waitTimeout": "10m",
            "values": {"replicas": 2},
        },
    }
    if management_policies is not None:
        spec["managementPolicies"] = management_policies
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-operator"},
                    "labels": {"modelplane.ai/resource": "operator"},
                },
                "spec": spec,
            }
        )
    )


def _usage(
    *, of_api_version: str, of_kind: str, of_key: str, by_api_version: str, by_kind: str, by_key: str
) -> fnv1.Resource:
    """The composed Usage holding the resource labelled of_key until the one labelled by_key is gone.

    Ready on arrival.
    """
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": of_api_version,
                        "kind": of_kind,
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": of_key},
                        },
                    },
                    "by": {
                        "apiVersion": by_api_version,
                        "kind": by_kind,
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": by_key},
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


COMPOSE_CASES = [
    ComposeCase(
        name="GateClosed",
        reason="Until the cluster can be reached, nothing that doesn't exist yet is rendered.",
        req=fnv1.RunFunctionRequest(),
        components=[_operator(), _config()],
        provider_config="cluster",
        gate=False,
        management_policies=None,
        want_rendered=[],
        want=fnv1.RunFunctionResponse(),
    ),
    # A dependency going unready, or the gate closing, never deletes what
    # already exists.
    ComposeCase(
        name="GateClosedObserved",
        reason="With the gate closed, a component that already exists still renders.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(resources={"operator": _observed(ready=False)}),
        ),
        components=[_operator(), _config()],
        provider_config="cluster",
        gate=False,
        management_policies=None,
        want_rendered=["operator"],
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(resources={"operator": _operator_release(management_policies=None)}),
        ),
    ),
    ComposeCase(
        name="DependencyNotReady",
        reason="A component whose dependency isn't Ready yet isn't first created.",
        req=fnv1.RunFunctionRequest(),
        components=[_operator(), _config()],
        provider_config="cluster",
        gate=True,
        management_policies=None,
        want_rendered=["operator"],
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(resources={"operator": _operator_release(management_policies=None)}),
        ),
    ),
    ComposeCase(
        name="DependencyReady",
        reason="Once its dependency is Ready, a multi-doc Manifests renders one Object per doc, keyed by doc name.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(resources={"operator": _observed(ready=True)}),
        ),
        components=[_operator(), _config()],
        provider_config="cluster",
        gate=True,
        management_policies=None,
        want_rendered=["operator", "config-a", "config-b"],
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(
                resources={
                    "operator": _operator_release(management_policies=None),
                    "config-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "config-a"}},
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "cluster"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "ConfigMap",
                                            "metadata": {"name": "a", "namespace": "operator-system"},
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "config-b": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "config-b"}},
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "cluster"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "ConfigMap",
                                            "metadata": {"name": "b", "namespace": "operator-system"},
                                        }
                                    },
                                },
                            }
                        )
                    ),
                }
            ),
        ),
    ),
    # The Observed component's own Ready condition is what its dependents'
    # install gate waits on.
    ComposeCase(
        name="Observed",
        reason=(
            "An Observed component renders as an observe-only Object, Ready by its query, and its dependent waits "
            "for it like any dependency."
        ),
        req=fnv1.RunFunctionRequest(),
        components=[
            _cert_manager(),
            stacks.Manifests(
                key="issuer",
                depends_on=["cert-manager"],
                manifests=[
                    {
                        "apiVersion": "cert-manager.io/v1",
                        "kind": "ClusterIssuer",
                        "metadata": {"name": "selfsigned"},
                        "spec": {"selfSigned": {}},
                    }
                ],
            ),
        ],
        provider_config="cluster",
        gate=True,
        management_policies=None,
        want_rendered=["cert-manager"],
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(
                resources={
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "cert-manager"}},
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "cluster"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "apps/v1",
                                            "kind": "Deployment",
                                            "metadata": {"name": "cert-manager-webhook", "namespace": "cert-manager"},
                                        }
                                    },
                                    "managementPolicies": ["Observe"],
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": "object.status.availableReplicas >= 1",
                                    },
                                },
                            }
                        )
                    ),
                }
            ),
        ),
    ),
    ComposeCase(
        name="ManagementPolicies",
        reason=(
            "management_policies applies to every Release and Manifests Object, and an Observed one stays observe-only."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(resources={"operator": _observed(ready=True)}),
        ),
        components=[_cert_manager(), _operator(), _config()],
        provider_config="cluster",
        gate=True,
        management_policies=["Observe", "Create", "Update", "LateInitialize"],
        want_rendered=["cert-manager", "operator", "config-a", "config-b"],
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(
                resources={
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "cert-manager"}},
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "cluster"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "apps/v1",
                                            "kind": "Deployment",
                                            "metadata": {"name": "cert-manager-webhook", "namespace": "cert-manager"},
                                        }
                                    },
                                    "managementPolicies": ["Observe"],
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": "object.status.availableReplicas >= 1",
                                    },
                                },
                            }
                        )
                    ),
                    "operator": _operator_release(
                        management_policies=["Observe", "Create", "Update", "LateInitialize"]
                    ),
                    "config-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "config-a"}},
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "cluster"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "ConfigMap",
                                            "metadata": {"name": "a", "namespace": "operator-system"},
                                        }
                                    },
                                    "managementPolicies": ["Observe", "Create", "Update", "LateInitialize"],
                                },
                            }
                        )
                    ),
                    "config-b": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "config-b"}},
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "cluster"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "ConfigMap",
                                            "metadata": {"name": "b", "namespace": "operator-system"},
                                        }
                                    },
                                    "managementPolicies": ["Observe", "Create", "Update", "LateInitialize"],
                                },
                            }
                        )
                    ),
                }
            ),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: ComposeCase) -> None:
    """compose renders the components its gates let through, and returns their keys."""
    rsp = fnv1.RunFunctionResponse()
    got = render.compose(
        case.req,
        rsp,
        case.components,
        provider_config=case.provider_config,
        gate=case.gate,
        management_policies=case.management_policies,
    )
    assert (got, _to_dict(rsp)) == (case.want_rendered, _to_dict(case.want)), case.reason


COMPOSE_USAGES_CASES = [
    ComposeUsagesCase(
        name="MultiDocDependent",
        reason="An edge onto a multi-doc Manifests becomes one Usage per doc, each holding the dependency's Release.",
        components=[_operator(), _config()],
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(
                resources={
                    "usage-operator-by-config-a": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="config-a",
                    ),
                    "usage-operator-by-config-b": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="config-b",
                    ),
                }
            ),
        ),
    ),
    # An Observed dependency belongs to another stack, which deletes it.
    ComposeUsagesCase(
        name="ObservedDependency",
        reason="An edge onto an Observed component gets no Usage, while the dependent's other edges do.",
        components=[
            _cert_manager(),
            _operator(),
            stacks.Manifests(
                key="issuer",
                depends_on=["cert-manager", "operator"],
                manifests=[
                    {
                        "apiVersion": "cert-manager.io/v1",
                        "kind": "ClusterIssuer",
                        "metadata": {"name": "selfsigned"},
                        "spec": {"selfSigned": {}},
                    }
                ],
            ),
        ],
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(
                resources={
                    "usage-operator-by-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="issuer",
                    ),
                }
            ),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_USAGES_CASES, ids=lambda case: case.name)
def test_compose_usages(case: ComposeUsagesCase) -> None:
    """compose_usages derives a Usage per depends_on edge, ready on arrival."""
    rsp = fnv1.RunFunctionResponse()
    render.compose_usages(rsp, case.components)
    assert _to_dict(rsp) == _to_dict(case.want), case.reason
