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

"""Tests for the helpers every provider's ProvisionedCluster composition shares.

A table each for is_contract, require_inputs, resolve_inputs,
check_roles, compose_input_usages, compose_activation, secrets,
cluster_secrets, compose_device_stack and write_status. The SDK has no generated models, so the
ProvisionedCluster and the provider's objects are written as dicts.
"""

import dataclasses
import json
from typing import Any

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from google.protobuf import json_format, message
from modelplane_sdk import provisioning


@dataclasses.dataclass
class IsContractCase:
    """A test case for provisioning.is_contract."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: bool


@dataclasses.dataclass
class RequireInputsCase:
    """A test case for provisioning.require_inputs."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class ResolveInputsCase:
    """A test case for provisioning.resolve_inputs."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    config: str
    node_class: str
    reservation: str | None
    # resolve_inputs's return value. want is what it wrote.
    want_inputs: provisioning.Inputs | None
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class CheckRolesCase:
    """A test case for provisioning.check_roles."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    accelerated: dict[str, bool]
    # check_roles's return value. want is what it wrote.
    want_suited: bool
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class ComposeInputUsagesCase:
    """A test case for provisioning.compose_input_usages."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    inputs: provisioning.Inputs
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class ComposeActivationCase:
    """A test case for provisioning.compose_activation."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    kinds: list[str]
    # compose_activation's return value. want is what it wrote.
    want_active: bool
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class SecretsCase:
    """A test case for provisioning.secrets."""

    name: str
    reason: str
    observed: list[dict[str, Any]] | None
    want: list[dict[str, Any]] | None


@dataclasses.dataclass
class ClusterSecretsCase:
    """A test case for provisioning.cluster_secrets."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    # cluster_secrets's return value. want is what it wrote.
    want_secrets: list[dict[str, Any]] | None
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class ComposeDeviceStackCase:
    """A test case for provisioning.compose_device_stack."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    cluster_secrets: list[dict[str, Any]] | None
    extra: dict[str, Any] | None
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class WriteStatusCase:
    """A test case for provisioning.write_status."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    cluster_secrets: list[dict[str, Any]] | None
    extra: dict[str, Any] | None
    want: fnv1.RunFunctionResponse


def _cluster(*, node_pools: list[dict[str, Any]]) -> fnv1.Resource:
    """The observed ProvisionedCluster prod, provisioned from AcmeClusterConfig us-east."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "ProvisionedCluster",
                "metadata": {"name": "prod"},
                "spec": {
                    "configRef": {
                        "apiVersion": "acme.example.org/v1alpha1",
                        "kind": "AcmeClusterConfig",
                        "name": "us-east",
                    },
                    "draDrivers": ["gpu.example.com"],
                    "nodePools": node_pools,
                },
            }
        )
    )


def _pool(*, name: str, role: str, node_class: str, reservation: str | None) -> dict[str, Any]:
    """A node pool of the given role and AcmeNodeClass node_class, backed by AcmeCapacityReservation reservation if there is one."""
    pool: dict[str, Any] = {
        "name": name,
        "role": role,
        "nodeClassRef": {"apiVersion": "acme.example.org/v1alpha1", "kind": "AcmeNodeClass", "name": node_class},
    }
    if reservation is not None:
        pool["reservationRef"] = {
            "apiVersion": "acme.example.org/v1alpha1",
            "kind": "AcmeCapacityReservation",
            "name": reservation,
        }
    return pool


def _config() -> dict[str, Any]:
    """The AcmeClusterConfig us-east."""
    return {
        "apiVersion": "acme.example.org/v1alpha1",
        "kind": "AcmeClusterConfig",
        "metadata": {"name": "us-east"},
        "spec": {"region": "us-east"},
    }


def _node_class(*, name: str) -> dict[str, Any]:
    """An AcmeNodeClass, whose machine is named for it."""
    return {
        "apiVersion": "acme.example.org/v1alpha1",
        "kind": "AcmeNodeClass",
        "metadata": {"name": name},
        "spec": {"machineType": name},
    }


def _fetched(*, obj: dict[str, Any] | None) -> fnv1.Resources:
    """A required resource as Crossplane fetched it: obj, or nothing if it doesn't exist."""
    if obj is None:
        return fnv1.Resources()
    return fnv1.Resources(items=[fnv1.Resource(resource=resource.dict_to_struct(obj))])


def _policy(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The desired activation policy for two Acme managed resource kinds."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "apiextensions.crossplane.io/v1alpha1",
                "kind": "ManagedResourceActivationPolicy",
                "spec": {"activate": ["clusters.acme.m.example.org", "nodepools.acme.m.example.org"]},
            }
        ),
        ready=ready,
    )


def _device_stack(*, secrets: list[dict[str, Any]], ready: fnv1.Ready) -> fnv1.Resource:
    """The desired AcmeDeviceStack for prod, carrying secrets."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "acme.example.org/v1alpha1",
                "kind": "AcmeDeviceStack",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "spec": {"secrets": secrets, "draDrivers": ["gpu.example.com"], "nvlinkDisabledPools": ["gpu"]},
            }
        ),
        ready=ready,
    )


def _observed_acme_cluster(*, ready: bool) -> fnv1.Resource:
    """The observed AcmeCluster, reporting its kubeconfig and cache StorageClass, Ready if ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "acme.example.org/v1alpha1",
                "kind": "AcmeCluster",
                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                "status": {
                    "secrets": [{"type": "Kubeconfig", "name": "prod-kubeconfig", "key": "kubeconfig"}],
                    "cache": {"storageClassName": "acme-rwx"},
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True" if ready else "False",
                            "lastTransitionTime": "2026-01-01T00:00:00Z",
                        }
                    ],
                },
            }
        )
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


IS_CONTRACT_CASES = [
    IsContractCase(
        name="ProvisionedCluster",
        reason="A request for a ProvisionedCluster is for the contract.",
        req=fnv1.RunFunctionRequest(observed=fnv1.State(composite=_cluster(node_pools=[]))),
        want=True,
    ),
    # A provider's own kinds need a Composition, which can share the
    # ProvisionedCluster's function.
    IsContractCase(
        name="ProvidersOwnKind",
        reason="A request for one of the provider's own kinds isn't for the contract.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_config())))
        ),
        want=False,
    ),
]


@pytest.mark.parametrize("case", IS_CONTRACT_CASES, ids=lambda case: case.name)
def test_is_contract(case: IsContractCase) -> None:
    """is_contract says whether a request is for a ProvisionedCluster."""
    assert provisioning.is_contract(case.req) == case.want, case.reason


REQUIRE_INPUTS_CASES = [
    RequireInputsCase(
        name="EveryReference",
        reason="The config, each node class once however many pools share it, and each reservation are required.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(
                    node_pools=[
                        _pool(name="gpu", role="Accelerator", node_class="h100", reservation="block"),
                        _pool(name="gpu-spare", role="Accelerator", node_class="h100", reservation=None),
                        _pool(name="cpu", role="CPU", node_class="m6", reservation=None),
                    ]
                )
            )
        ),
        want=fnv1.RunFunctionResponse(
            requirements=fnv1.Requirements(
                resources={
                    "config": fnv1.ResourceSelector(
                        api_version="acme.example.org/v1alpha1", kind="AcmeClusterConfig", match_name="us-east"
                    ),
                    "node-class-h100": fnv1.ResourceSelector(
                        api_version="acme.example.org/v1alpha1", kind="AcmeNodeClass", match_name="h100"
                    ),
                    "node-class-m6": fnv1.ResourceSelector(
                        api_version="acme.example.org/v1alpha1", kind="AcmeNodeClass", match_name="m6"
                    ),
                    "reservation-block": fnv1.ResourceSelector(
                        api_version="acme.example.org/v1alpha1", kind="AcmeCapacityReservation", match_name="block"
                    ),
                }
            )
        ),
    ),
]


@pytest.mark.parametrize("case", REQUIRE_INPUTS_CASES, ids=lambda case: case.name)
def test_require_inputs(case: RequireInputsCase) -> None:
    """require_inputs requires every object the ProvisionedCluster references."""
    rsp = fnv1.RunFunctionResponse()
    provisioning.require_inputs(case.req, rsp)
    assert _to_dict(rsp) == _to_dict(case.want), case.reason


RESOLVE_INPUTS_CASES = [
    ResolveInputsCase(
        name="NotFetched",
        reason="While Crossplane hasn't fetched every reference, it returns None without failing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(
                    node_pools=[_pool(name="gpu", role="Accelerator", node_class="h100", reservation=None)]
                )
            ),
            required_resources={"config": _fetched(obj=_config())},
        ),
        config="AcmeClusterConfig.acme.example.org",
        node_class="AcmeNodeClass.acme.example.org",
        reservation=None,
        want_inputs=None,
        want=fnv1.RunFunctionResponse(),
    ),
    ResolveInputsCase(
        name="Resolved",
        reason="Once every reference is fetched, it returns them, keyed by name.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(
                    node_pools=[_pool(name="gpu", role="Accelerator", node_class="h100", reservation="block")]
                )
            ),
            required_resources={
                "config": _fetched(obj=_config()),
                "node-class-h100": _fetched(obj=_node_class(name="h100")),
                "reservation-block": _fetched(
                    obj={
                        "apiVersion": "acme.example.org/v1alpha1",
                        "kind": "AcmeCapacityReservation",
                        "metadata": {"name": "block"},
                        "spec": {"id": "cr-0123"},
                    }
                ),
            },
        ),
        config="AcmeClusterConfig.acme.example.org",
        node_class="AcmeNodeClass.acme.example.org",
        reservation="AcmeCapacityReservation.acme.example.org",
        want_inputs=provisioning.Inputs(
            config=_config(),
            node_classes={"h100": _node_class(name="h100")},
            reservations={
                "block": {
                    "apiVersion": "acme.example.org/v1alpha1",
                    "kind": "AcmeCapacityReservation",
                    "metadata": {"name": "block"},
                    "spec": {"id": "cr-0123"},
                }
            },
        ),
        want=fnv1.RunFunctionResponse(),
    ),
    # A pool given another provider's class names a kind this provider
    # doesn't own, whether or not Crossplane has fetched it.
    ResolveInputsCase(
        name="OtherProvidersKind",
        reason="A node class reference to a kind the provider doesn't own fails the reconcile.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(
                    node_pools=[
                        {
                            "name": "gpu",
                            "role": "Accelerator",
                            "nodeClassRef": {
                                "apiVersion": "other.example.org/v1alpha1",
                                "kind": "OtherNodeClass",
                                "name": "h100",
                            },
                        }
                    ]
                )
            ),
        ),
        config="AcmeClusterConfig.acme.example.org",
        node_class="AcmeNodeClass.acme.example.org",
        reservation=None,
        want_inputs=None,
        want=fnv1.RunFunctionResponse(
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="node pool gpu's nodeClassRef is a OtherNodeClass.other.example.org, "
                    "not a AcmeNodeClass.acme.example.org",
                )
            ]
        ),
    ),
    ResolveInputsCase(
        name="NoReservations",
        reason="A reservation reference fails the reconcile of a provider that has no reservations.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(
                    node_pools=[_pool(name="gpu", role="Accelerator", node_class="h100", reservation="block")]
                )
            ),
        ),
        config="AcmeClusterConfig.acme.example.org",
        node_class="AcmeNodeClass.acme.example.org",
        reservation=None,
        want_inputs=None,
        want=fnv1.RunFunctionResponse(
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="node pool gpu's reservationRef is a AcmeCapacityReservation.acme.example.org, "
                    "not nothing, because this provider has no reservations",
                )
            ]
        ),
    ),
    # Fatal, so Crossplane keeps what the ProvisionedCluster already composed
    # rather than deleting it with the rest of an empty desired state.
    ResolveInputsCase(
        name="Missing",
        reason="A referenced object that doesn't exist fails the reconcile, naming it as missing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(
                    node_pools=[
                        _pool(name="gpu", role="Accelerator", node_class="h100", reservation=None),
                        _pool(name="cpu", role="CPU", node_class="m6", reservation=None),
                    ]
                )
            ),
            required_resources={
                "config": _fetched(obj=_config()),
                "node-class-h100": _fetched(obj=None),
                "node-class-m6": _fetched(obj=None),
            },
        ),
        config="AcmeClusterConfig.acme.example.org",
        node_class="AcmeNodeClass.acme.example.org",
        reservation=None,
        want_inputs=None,
        want=fnv1.RunFunctionResponse(
            results=[fnv1.Result(severity=fnv1.SEVERITY_FATAL, message="Missing AcmeNodeClass h100, AcmeNodeClass m6")]
        ),
    ),
]


@pytest.mark.parametrize("case", RESOLVE_INPUTS_CASES, ids=lambda case: case.name)
def test_resolve_inputs(case: ResolveInputsCase) -> None:
    """resolve_inputs returns the objects the ProvisionedCluster references, or fails the reconcile."""
    rsp = fnv1.RunFunctionResponse()
    got = provisioning.resolve_inputs(
        case.req, rsp, config=case.config, node_class=case.node_class, reservation=case.reservation
    )
    assert (got, _to_dict(rsp)) == (case.want_inputs, _to_dict(case.want)), case.reason


CHECK_ROLES_CASES = [
    CheckRolesCase(
        name="Suited",
        reason="An Accelerator pool whose class names an accelerator and a CPU pool whose class doesn't suit their roles.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(
                    node_pools=[
                        _pool(name="gpu", role="Accelerator", node_class="h100", reservation=None),
                        _pool(name="cpu", role="CPU", node_class="m6", reservation=None),
                    ]
                )
            )
        ),
        accelerated={"h100": True, "m6": False},
        want_suited=True,
        want=fnv1.RunFunctionResponse(),
    ),
    CheckRolesCase(
        name="Mismatched",
        reason="An Accelerator pool whose class names no accelerator and a CPU pool whose class names one fail the "
        "reconcile, naming both.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(
                    node_pools=[
                        _pool(name="gpu", role="Accelerator", node_class="m6", reservation=None),
                        _pool(name="cpu", role="CPU", node_class="h100", reservation=None),
                    ]
                )
            )
        ),
        accelerated={"h100": True, "m6": False},
        want_suited=False,
        want=fnv1.RunFunctionResponse(
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="node pool gpu runs accelerators, but AcmeNodeClass m6 names no accelerator; "
                    "node pool cpu is a CPU pool, but AcmeNodeClass h100 names an accelerator",
                )
            ]
        ),
    ),
]


@pytest.mark.parametrize("case", CHECK_ROLES_CASES, ids=lambda case: case.name)
def test_check_roles(case: CheckRolesCase) -> None:
    """check_roles says whether every pool's node class suits its role, or fails the reconcile."""
    rsp = fnv1.RunFunctionResponse()
    got = provisioning.check_roles(case.req, rsp, case.accelerated)
    assert (got, _to_dict(rsp)) == (case.want_suited, _to_dict(case.want)), case.reason


COMPOSE_INPUT_USAGES_CASES = [
    ComposeInputUsagesCase(
        name="EveryInput",
        reason="The config, each node class and each reservation get a ClusterUsage naming the cluster, ready on arrival.",
        req=fnv1.RunFunctionRequest(observed=fnv1.State(composite=_cluster(node_pools=[]))),
        inputs=provisioning.Inputs(
            config=_config(),
            node_classes={"h100": _node_class(name="h100")},
            reservations={
                "block": {
                    "apiVersion": "acme.example.org/v1alpha1",
                    "kind": "AcmeCapacityReservation",
                    "metadata": {"name": "block"},
                    "spec": {"id": "cr-0123"},
                }
            },
        ),
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(
                resources={
                    "usage-acmeclusterconfig-us-east": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "ClusterUsage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "acme.example.org/v1alpha1",
                                        "kind": "AcmeClusterConfig",
                                        "resourceRef": {"name": "us-east"},
                                    },
                                    "reason": "ProvisionedCluster prod uses this AcmeClusterConfig",
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-acmenodeclass-h100": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "ClusterUsage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "acme.example.org/v1alpha1",
                                        "kind": "AcmeNodeClass",
                                        "resourceRef": {"name": "h100"},
                                    },
                                    "reason": "ProvisionedCluster prod uses this AcmeNodeClass",
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-acmecapacityreservation-block": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "ClusterUsage",
                                "spec": {
                                    "of": {
                                        "apiVersion": "acme.example.org/v1alpha1",
                                        "kind": "AcmeCapacityReservation",
                                        "resourceRef": {"name": "block"},
                                    },
                                    "reason": "ProvisionedCluster prod uses this AcmeCapacityReservation",
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                }
            )
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_INPUT_USAGES_CASES, ids=lambda case: case.name)
def test_compose_input_usages(case: ComposeInputUsagesCase) -> None:
    """compose_input_usages guards each object the ProvisionedCluster references against deletion."""
    rsp = fnv1.RunFunctionResponse()
    provisioning.compose_input_usages(case.req, rsp, case.inputs)
    assert _to_dict(rsp) == _to_dict(case.want), case.reason


COMPOSE_ACTIVATION_CASES = [
    ComposeActivationCase(
        name="NotObserved",
        reason="Until the policy is observed, it's composed but not ready, and the kinds aren't active.",
        req=fnv1.RunFunctionRequest(),
        kinds=["clusters.acme.m.example.org", "nodepools.acme.m.example.org"],
        want_active=False,
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(resources={"activation": _policy(ready=fnv1.READY_UNSPECIFIED)})
        ),
    ),
    # The policy reports Healthy even when it matched no definitions, so only
    # its status.activated counts.
    ComposeActivationCase(
        name="PartlyActivated",
        reason="While the policy has activated only some of the kinds, they aren't active.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                resources={
                    "activation": fnv1.Resource(
                        resource=resource.dict_to_struct({"status": {"activated": ["clusters.acme.m.example.org"]}})
                    )
                }
            )
        ),
        kinds=["clusters.acme.m.example.org", "nodepools.acme.m.example.org"],
        want_active=False,
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(resources={"activation": _policy(ready=fnv1.READY_UNSPECIFIED)})
        ),
    ),
    ComposeActivationCase(
        name="Activated",
        reason="Once the policy has activated every kind, they're active and the policy is ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                resources={
                    "activation": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "status": {
                                    "activated": [
                                        "clusters.acme.m.example.org",
                                        "nodepools.acme.m.example.org",
                                        "networks.acme.m.example.org",
                                    ]
                                }
                            }
                        )
                    )
                }
            )
        ),
        kinds=["clusters.acme.m.example.org", "nodepools.acme.m.example.org"],
        want_active=True,
        want=fnv1.RunFunctionResponse(desired=fnv1.State(resources={"activation": _policy(ready=fnv1.READY_TRUE)})),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_ACTIVATION_CASES, ids=lambda case: case.name)
def test_compose_activation(case: ComposeActivationCase) -> None:
    """compose_activation composes the activation policy and says whether its kinds are active."""
    rsp = fnv1.RunFunctionResponse()
    got = provisioning.compose_activation(case.req, rsp, case.kinds)
    assert (got, _to_dict(rsp)) == (case.want_active, _to_dict(case.want)), case.reason


SECRETS_CASES = [
    SecretsCase(
        name="NoSecrets",
        reason="A cluster reporting no secrets has none to publish.",
        observed=None,
        want=None,
    ),
    SecretsCase(
        name="NoKubeconfig",
        reason="A cluster reporting secrets but no kubeconfig isn't reachable, so it has none to publish.",
        observed=[{"type": "GoogleApplicationCredentials", "name": "prod-sa-key", "key": "private_key"}],
        want=None,
    ),
    SecretsCase(
        name="Namespaces",
        reason="An entry without a namespace is in modelplane-system, and one with a namespace keeps it.",
        observed=[
            {"type": "Kubeconfig", "name": "prod-kubeconfig", "key": "kubeconfig"},
            {"type": "GoogleApplicationCredentials", "name": "prod-sa-key", "key": "private_key", "namespace": "acme"},
        ],
        want=[
            {"type": "Kubeconfig", "name": "prod-kubeconfig", "key": "kubeconfig", "namespace": "modelplane-system"},
            {"type": "GoogleApplicationCredentials", "name": "prod-sa-key", "key": "private_key", "namespace": "acme"},
        ],
    ),
]


@pytest.mark.parametrize("case", SECRETS_CASES, ids=lambda case: case.name)
def test_secrets(case: SecretsCase) -> None:
    """secrets reports a cluster's secrets once it has a kubeconfig."""
    assert provisioning.secrets(case.observed) == case.want, case.reason


CLUSTER_SECRETS_CASES = [
    ClusterSecretsCase(
        name="NotObserved",
        reason="Before the cluster XR exists, there are no secrets and nothing is marked ready.",
        req=fnv1.RunFunctionRequest(observed=fnv1.State(composite=_cluster(node_pools=[]))),
        want_secrets=None,
        want=fnv1.RunFunctionResponse(),
    ),
    ClusterSecretsCase(
        name="NotReady",
        reason="A cluster XR that isn't Ready has no secrets to publish, even if it reports some.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(node_pools=[]),
                resources={"acme-cluster": _observed_acme_cluster(ready=False)},
            )
        ),
        want_secrets=None,
        want=fnv1.RunFunctionResponse(),
    ),
    ClusterSecretsCase(
        name="Ready",
        reason="A Ready cluster XR is marked ready, and its secrets are returned in modelplane-system.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(node_pools=[]),
                resources={"acme-cluster": _observed_acme_cluster(ready=True)},
            )
        ),
        want_secrets=[
            {"type": "Kubeconfig", "name": "prod-kubeconfig", "key": "kubeconfig", "namespace": "modelplane-system"}
        ],
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(resources={"acme-cluster": fnv1.Resource(ready=fnv1.READY_TRUE)})
        ),
    ),
]


@pytest.mark.parametrize("case", CLUSTER_SECRETS_CASES, ids=lambda case: case.name)
def test_cluster_secrets(case: ClusterSecretsCase) -> None:
    """cluster_secrets marks the cluster XR ready and returns its secrets once it's Ready."""
    rsp = fnv1.RunFunctionResponse()
    got = provisioning.cluster_secrets(case.req, rsp, "acme-cluster")
    assert (got, _to_dict(rsp)) == (case.want_secrets, _to_dict(case.want)), case.reason


COMPOSE_DEVICE_STACK_CASES = [
    ComposeDeviceStackCase(
        name="Unreachable",
        reason="Until the cluster reports secrets, no device stack is composed.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(
                    node_pools=[_pool(name="gpu", role="Accelerator", node_class="h100", reservation=None)]
                )
            )
        ),
        cluster_secrets=None,
        extra={"nvlinkDisabledPools": ["gpu"]},
        want=fnv1.RunFunctionResponse(),
    ),
    ComposeDeviceStackCase(
        name="Reachable",
        reason="Once the cluster reports secrets, the device stack carries them, the drivers and extra, unready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(
                    node_pools=[_pool(name="gpu", role="Accelerator", node_class="h100", reservation=None)]
                )
            )
        ),
        cluster_secrets=[
            {"type": "Kubeconfig", "name": "prod-kubeconfig", "key": "kubeconfig", "namespace": "modelplane-system"}
        ],
        extra={"nvlinkDisabledPools": ["gpu"]},
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(
                resources={
                    "device-stack": _device_stack(
                        secrets=[
                            {
                                "type": "Kubeconfig",
                                "name": "prod-kubeconfig",
                                "key": "kubeconfig",
                                "namespace": "modelplane-system",
                            }
                        ],
                        ready=fnv1.READY_UNSPECIFIED,
                    )
                }
            )
        ),
    ),
    # A cluster briefly losing its secrets mustn't delete its device stack.
    ComposeDeviceStackCase(
        name="SecretsLost",
        reason="A device stack that exists keeps its observed secrets when the cluster reports none, and is ready once it is.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(
                    node_pools=[_pool(name="gpu", role="Accelerator", node_class="h100", reservation=None)]
                ),
                resources={
                    "device-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "acme.example.org/v1alpha1",
                                "kind": "AcmeDeviceStack",
                                "metadata": {"name": "prod", "namespace": "modelplane-system"},
                                "spec": {
                                    "secrets": [
                                        {
                                            "type": "Kubeconfig",
                                            "name": "prod-kubeconfig",
                                            "key": "kubeconfig",
                                            "namespace": "modelplane-system",
                                        }
                                    ],
                                    "draDrivers": ["gpu.example.com"],
                                },
                                "status": {
                                    "conditions": [
                                        {
                                            "type": "Ready",
                                            "status": "True",
                                            "lastTransitionTime": "2026-01-01T00:00:00Z",
                                        }
                                    ]
                                },
                            }
                        )
                    )
                },
            )
        ),
        cluster_secrets=None,
        extra={"nvlinkDisabledPools": ["gpu"]},
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(
                resources={
                    "device-stack": _device_stack(
                        secrets=[
                            {
                                "type": "Kubeconfig",
                                "name": "prod-kubeconfig",
                                "key": "kubeconfig",
                                "namespace": "modelplane-system",
                            }
                        ],
                        ready=fnv1.READY_TRUE,
                    )
                }
            )
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_DEVICE_STACK_CASES, ids=lambda case: case.name)
def test_compose_device_stack(case: ComposeDeviceStackCase) -> None:
    """compose_device_stack composes the provider's device stack once the cluster is reachable."""
    rsp = fnv1.RunFunctionResponse()
    provisioning.compose_device_stack(
        case.req,
        rsp,
        api_version="acme.example.org/v1alpha1",
        kind="AcmeDeviceStack",
        cluster_secrets=case.cluster_secrets,
        extra=case.extra,
    )
    assert _to_dict(rsp) == _to_dict(case.want), case.reason


WRITE_STATUS_CASES = [
    WriteStatusCase(
        name="NothingToReport",
        reason="Before the cluster XR exists, and with no secrets or extra, the status is written empty.",
        req=fnv1.RunFunctionRequest(observed=fnv1.State(composite=_cluster(node_pools=[]))),
        cluster_secrets=None,
        extra=None,
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct({"status": {}})))
        ),
    ),
    WriteStatusCase(
        name="Everything",
        reason="The status carries the secrets, the cache StorageClass the cluster XR reports, and extra.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_cluster(node_pools=[]),
                resources={"acme-cluster": _observed_acme_cluster(ready=True)},
            )
        ),
        cluster_secrets=[
            {"type": "Kubeconfig", "name": "prod-kubeconfig", "key": "kubeconfig", "namespace": "modelplane-system"}
        ],
        extra={"gateway": {"service": {"annotations": {"lb.example.org/type": "public"}}}},
        want=fnv1.RunFunctionResponse(
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "secrets": [
                                    {
                                        "type": "Kubeconfig",
                                        "name": "prod-kubeconfig",
                                        "key": "kubeconfig",
                                        "namespace": "modelplane-system",
                                    }
                                ],
                                "cache": {"storageClassName": "acme-rwx"},
                                "gateway": {"service": {"annotations": {"lb.example.org/type": "public"}}},
                            }
                        }
                    )
                )
            )
        ),
    ),
]


@pytest.mark.parametrize("case", WRITE_STATUS_CASES, ids=lambda case: case.name)
def test_write_status(case: WriteStatusCase) -> None:
    """write_status reports what Modelplane needs to use the cluster."""
    rsp = fnv1.RunFunctionResponse()
    provisioning.write_status(
        case.req, rsp, cluster="acme-cluster", cluster_secrets=case.cluster_secrets, extra=case.extra
    )
    assert _to_dict(rsp) == _to_dict(case.want), case.reason
