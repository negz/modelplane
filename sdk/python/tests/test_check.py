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

"""Tests for checking what a provider declares against what it does."""

import dataclasses
from typing import Any

import pytest
from modelplane_sdk import check, stacks


@dataclasses.dataclass
class ProblemsCase:
    """A test case for check.problems."""

    name: str
    reason: str
    provider: str
    objects: list[dict[str, Any]]
    conformance: bool
    components: list[stacks.Component]
    external: list[str]
    want: list[str]


def _composition(*, name: str, config_kind: str, capabilities: str, dra_drivers: str) -> dict[str, Any]:
    """A Composition for a ProvisionedCluster, selected by an acme.example.org config_kind."""
    return {
        "apiVersion": "apiextensions.crossplane.io/v1",
        "kind": "Composition",
        "metadata": {
            "name": name,
            "labels": {
                "infrastructure.modelplane.ai/config-group": "acme.example.org",
                "infrastructure.modelplane.ai/config-kind": config_kind,
            },
            "annotations": {
                "infrastructure.modelplane.ai/capabilities": capabilities,
                "infrastructure.modelplane.ai/dra-drivers": dra_drivers,
            },
        },
        "spec": {
            "compositeTypeRef": {"apiVersion": "infrastructure.modelplane.ai/v1alpha1", "kind": "ProvisionedCluster"},
            "mode": "Pipeline",
        },
    }


def _xrd() -> dict[str, Any]:
    """The XRD defining the provider's cluster config kind, AcmeClusterConfig."""
    return {
        "apiVersion": "apiextensions.crossplane.io/v2",
        "kind": "CompositeResourceDefinition",
        "metadata": {"name": "acmeclusterconfigs.acme.example.org"},
        "spec": {"group": "acme.example.org", "names": {"kind": "AcmeClusterConfig", "plural": "acmeclusterconfigs"}},
    }


def _policy(*, name: str) -> dict[str, Any]:
    """An activation policy for the kinds the provider's device stack composes."""
    return {
        "apiVersion": "apiextensions.crossplane.io/v1alpha1",
        "kind": "ManagedResourceActivationPolicy",
        "metadata": {"name": name},
        "spec": {"activate": ["releases.helm.m.crossplane.io", "objects.kubernetes.m.crossplane.io"]},
    }


def _driver() -> stacks.Chart:
    """A device stack's DRA driver chart, for gpu.example.com."""
    return stacks.Chart(
        key="dra-driver",
        release="mp-dra-driver",
        namespace="dra-driver",
        chart="dra-driver",
        repository="https://charts.example.com",
        version="1.0.0",
        dra_drivers=["gpu.example.com"],
    )


PROBLEMS_CASES = [
    ProblemsCase(
        name="Valid",
        reason="A provider declaring what its device stack and EXTERNAL_DRIVERS carry, under its own names, passes.",
        provider="acme",
        objects=[
            _composition(
                name="acme.provisionedclusters.infrastructure.modelplane.ai",
                config_kind="AcmeClusterConfig",
                capabilities="Zones, CapacityReservations",
                dra_drivers="gpu.example.com,net.example.com",
            ),
            _xrd(),
            _policy(name="modelplane-acme"),
        ],
        conformance=True,
        components=[_driver()],
        external=["net.example.com"],
        want=[],
    ),
    ProblemsCase(
        name="NoConformance",
        reason="A provider with no conformance.yaml fails.",
        provider="acme",
        objects=[
            _composition(
                name="acme.provisionedclusters.infrastructure.modelplane.ai",
                config_kind="AcmeClusterConfig",
                capabilities="",
                dra_drivers="gpu.example.com",
            ),
            _xrd(),
            _policy(name="modelplane-acme"),
        ],
        conformance=False,
        components=[_driver()],
        external=[],
        want=["acme has no conformance.yaml, so the conformance suite can't run it"],
    ),
    # Package object names are global, and Modelplane's own policy is named
    # modelplane.
    ProblemsCase(
        name="PolicyNotNamed",
        reason="An activation policy not named modelplane-<provider> fails.",
        provider="acme",
        objects=[
            _composition(
                name="acme.provisionedclusters.infrastructure.modelplane.ai",
                config_kind="AcmeClusterConfig",
                capabilities="",
                dra_drivers="gpu.example.com",
            ),
            _xrd(),
            _policy(name="modelplane"),
        ],
        conformance=True,
        components=[_driver()],
        external=[],
        want=["activation policy modelplane should be named modelplane-acme"],
    ),
    ProblemsCase(
        name="CompositionNotNamed",
        reason="A Composition for a ProvisionedCluster not named for the provider fails.",
        provider="acme",
        objects=[
            _composition(
                name="provisionedclusters.infrastructure.modelplane.ai",
                config_kind="AcmeClusterConfig",
                capabilities="",
                dra_drivers="gpu.example.com",
            ),
            _xrd(),
            _policy(name="modelplane-acme"),
        ],
        conformance=True,
        components=[_driver()],
        external=[],
        want=[
            "Composition provisionedclusters.infrastructure.modelplane.ai should be named "
            "acme.provisionedclusters.infrastructure.modelplane.ai"
        ],
    ),
    ProblemsCase(
        name="NoComposition",
        reason="A provider with no Composition for a ProvisionedCluster fails, after the problems found before it.",
        provider="acme",
        objects=[_xrd(), _policy(name="modelplane")],
        conformance=True,
        components=[_driver()],
        external=[],
        want=[
            "activation policy modelplane should be named modelplane-acme",
            "want one Composition for a ProvisionedCluster, found 0",
        ],
    ),
    ProblemsCase(
        name="UndefinedConfigKind",
        reason="A Composition selecting on a config kind none of the provider's XRDs define fails.",
        provider="acme",
        objects=[
            _composition(
                name="acme.provisionedclusters.infrastructure.modelplane.ai",
                config_kind="AcmeConfig",
                capabilities="",
                dra_drivers="gpu.example.com",
            ),
            _xrd(),
            _policy(name="modelplane-acme"),
        ],
        conformance=True,
        components=[_driver()],
        external=[],
        want=[
            "Composition acme.provisionedclusters.infrastructure.modelplane.ai selects on "
            "AcmeConfig.acme.example.org, which none of the provider's XRDs define"
        ],
    ),
    ProblemsCase(
        name="UnknownCapability",
        reason="A Composition declaring a capability the contract doesn't define fails.",
        provider="acme",
        objects=[
            _composition(
                name="acme.provisionedclusters.infrastructure.modelplane.ai",
                config_kind="AcmeClusterConfig",
                capabilities="Zones,Spot",
                dra_drivers="gpu.example.com",
            ),
            _xrd(),
            _policy(name="modelplane-acme"),
        ],
        conformance=True,
        components=[_driver()],
        external=[],
        want=[
            "Composition acme.provisionedclusters.infrastructure.modelplane.ai declares unknown capabilities ['Spot']"
        ],
    ),
    ProblemsCase(
        name="DriversDiffer",
        reason="A Composition declaring DRA drivers other than the device stack and EXTERNAL_DRIVERS carry fails.",
        provider="acme",
        objects=[
            _composition(
                name="acme.provisionedclusters.infrastructure.modelplane.ai",
                config_kind="AcmeClusterConfig",
                capabilities="",
                dra_drivers="gpu.example.com",
            ),
            _xrd(),
            _policy(name="modelplane-acme"),
        ],
        conformance=True,
        components=[_driver()],
        external=["net.example.com"],
        want=[
            "Composition acme.provisionedclusters.infrastructure.modelplane.ai declares DRA drivers "
            "['gpu.example.com'], but the device stack and EXTERNAL_DRIVERS carry "
            "['gpu.example.com', 'net.example.com']"
        ],
    ),
    ProblemsCase(
        name="InvalidStack",
        reason="A device stack modelplane_sdk.stacks.check rejects fails.",
        provider="acme",
        objects=[
            _composition(
                name="acme.provisionedclusters.infrastructure.modelplane.ai",
                config_kind="AcmeClusterConfig",
                capabilities="",
                dra_drivers="gpu.example.com",
            ),
            _xrd(),
            _policy(name="modelplane-acme"),
        ],
        conformance=True,
        components=[
            stacks.Chart(
                key="dra-driver",
                release="dra-driver",
                namespace="dra-driver",
                chart="dra-driver",
                repository="https://charts.example.com",
                version="1.0.0",
                dra_drivers=["gpu.example.com"],
            ),
        ],
        external=[],
        want=["acme: charts ['dra-driver'] aren't released as mp-<chart>"],
    ),
    ProblemsCase(
        name="OtherNVIDIAPin",
        reason="A device stack pinning one of NVIDIA's charts at another version than the SDK's fails.",
        provider="acme",
        objects=[
            _composition(
                name="acme.provisionedclusters.infrastructure.modelplane.ai",
                config_kind="AcmeClusterConfig",
                capabilities="",
                dra_drivers="gpu.nvidia.com",
            ),
            _xrd(),
            _policy(name="modelplane-acme"),
        ],
        conformance=True,
        components=[
            stacks.Chart(
                key="nvidia-dra-driver-gpu",
                release="mp-dra-driver-nvidia-gpu",
                namespace="nvidia-dra-driver",
                chart="dra-driver-nvidia-gpu",
                repository="oci://registry.k8s.io/dra-driver-nvidia/charts",
                version="0.4.0",
                dra_drivers=["gpu.nvidia.com"],
            ),
        ],
        external=[],
        want=["acme: nvidia-dra-driver-gpu pins dra-driver-nvidia-gpu 0.4.0, but the SDK pins 0.5.0"],
    ),
]


@pytest.mark.parametrize("case", PROBLEMS_CASES, ids=lambda case: case.name)
def test_problems(case: ProblemsCase) -> None:
    """problems reports everything a provider declares that doesn't match what it does."""
    got = check.problems(
        case.provider,
        objects=case.objects,
        conformance=case.conformance,
        components=case.components,
        external=case.external,
    )
    assert got == case.want, case.reason
