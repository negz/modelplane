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

"""Tests for the checks and filters every stack's component list goes through.

A table each for check, filter_drivers, supported_drivers and
nvidia.check_pins.
"""

import dataclasses
import re

import pytest
from modelplane_sdk import stacks
from modelplane_sdk.stacks import nvidia


@dataclasses.dataclass
class CheckCase:
    """A test case for stacks.check."""

    name: str
    reason: str
    components: list[stacks.Component]
    want: str | None


@dataclasses.dataclass
class FilterDriversCase:
    """A test case for stacks.filter_drivers."""

    name: str
    reason: str
    components: list[stacks.Component]
    dra_drivers: list[str]
    want: list[stacks.Component]


@dataclasses.dataclass
class SupportedDriversCase:
    """A test case for stacks.supported_drivers."""

    name: str
    reason: str
    components: list[stacks.Component]
    want: frozenset[str]


@dataclasses.dataclass
class CheckPinsCase:
    """A test case for nvidia.check_pins."""

    name: str
    reason: str
    components: list[stacks.Component]
    want: str | None


def _chart(*, key: str, depends_on: list[str], dra_drivers: list[str]) -> stacks.Chart:
    """A chart named for key, depending on depends_on and tagged with dra_drivers."""
    return stacks.Chart(
        key=key,
        release=f"mp-{key}",
        namespace=key,
        chart=key,
        repository="https://charts.example.com",
        version="1.0.0",
        wait=True,
        depends_on=depends_on,
        dra_drivers=dra_drivers,
    )


def _observed() -> stacks.Observed:
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


CHECK_CASES = [
    CheckCase(
        name="Valid",
        reason="A list whose dependent carries a subset of its tagged dependency's drivers passes.",
        components=[
            _chart(key="dep", depends_on=[], dra_drivers=["gpu.nvidia.com", "gpu.amd.com"]),
            _chart(key="user", depends_on=["dep"], dra_drivers=["gpu.nvidia.com"]),
        ],
        want=None,
    ),
    # An Observed component carries no tags, and no filter removes one that
    # something left in the list depends on.
    CheckCase(
        name="ObservedDependency",
        reason="A tagged component may depend on an Observed one.",
        components=[_observed(), _chart(key="user", depends_on=["cert-manager"], dra_drivers=["gpu.nvidia.com"])],
        want=None,
    ),
    CheckCase(
        name="DuplicateKeys",
        reason="Two components with one key fail the check.",
        components=[
            _chart(key="dep", depends_on=[], dra_drivers=[]),
            _chart(key="dep", depends_on=[], dra_drivers=[]),
        ],
        want="test: duplicate component keys ['dep']",
    ),
    # A multi-doc Manifests renders one composed resource per doc, keyed
    # <key>-<name>, so it can collide with a component of that key.
    CheckCase(
        name="DuplicateComposedResourceKeys",
        reason="Two components rendering under one composed-resource key fail the check.",
        components=[
            stacks.Manifests(
                key="config",
                manifests=[
                    {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a", "namespace": "default"}},
                    {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "b", "namespace": "default"}},
                ],
            ),
            _chart(key="config-a", depends_on=[], dra_drivers=[]),
        ],
        want="test: duplicate composed-resource keys ['config-a']",
    ),
    CheckCase(
        name="MissingDependency",
        reason="A component depending on a key the list doesn't have fails the check.",
        components=[_chart(key="user", depends_on=["dep"], dra_drivers=[])],
        want="test: user depends on 'dep', which the list does not have",
    ),
    CheckCase(
        name="TaggedDependency",
        reason="A dependency not tagged with every driver its dependent carries fails the check.",
        components=[
            _chart(key="dep", depends_on=[], dra_drivers=["gpu.nvidia.com"]),
            _chart(key="user", depends_on=["dep"], dra_drivers=["gpu.nvidia.com", "dra.cpu"]),
        ],
        want=(
            "test: user (drivers ['gpu.nvidia.com', 'dra.cpu']) depends on 'dep' (drivers ['gpu.nvidia.com']), "
            "which filtering could remove from under it"
        ),
    ),
    # Every filter keeps an untagged component, and no filter keeps every
    # tagged one.
    CheckCase(
        name="UntaggedDependent",
        reason="An untagged component depending on a tagged one fails the check.",
        components=[
            _chart(key="dep", depends_on=[], dra_drivers=["gpu.nvidia.com"]),
            _chart(key="user", depends_on=["dep"], dra_drivers=[]),
        ],
        want=(
            "test: user (drivers untagged) depends on 'dep' (drivers ['gpu.nvidia.com']), "
            "which filtering could remove from under it"
        ),
    ),
    # The mp- prefix reserves a namespace, so Modelplane can't adopt a
    # same-named release a user already runs.
    CheckCase(
        name="ReleaseNotPrefixed",
        reason="A chart whose release isn't mp-<chart> fails the check.",
        components=[
            stacks.Chart(
                key="operator",
                release="operator",
                namespace="operator",
                chart="operator",
                repository="https://charts.example.com",
                version="1.0.0",
            ),
        ],
        want="test: charts ['operator'] aren't released as mp-<chart>",
    ),
    CheckCase(
        name="EmptyManifests",
        reason="A Manifests carrying no manifests fails the check.",
        components=[stacks.Manifests(key="config", manifests=[])],
        want="test: Manifests ['config'] carry no manifests",
    ),
    CheckCase(
        name="ReadyMultiDoc",
        reason="A Manifests with a readiness query and two manifests fails the check.",
        components=[
            stacks.Manifests(
                key="driver",
                ready='object.status.state == "ready"',
                manifests=[
                    {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "a", "namespace": "d"}},
                    {"apiVersion": "nvidia.com/v1alpha1", "kind": "NVIDIADriver", "metadata": {"name": "b"}},
                ],
            ),
        ],
        want="test: Manifests ['driver'] have a readiness query but more than one manifest",
    ),
    CheckCase(
        name="DependencyNotWaiting",
        reason="A chart another component depends on that doesn't set wait fails the check.",
        components=[
            stacks.Chart(
                key="operator",
                release="mp-operator",
                namespace="operator",
                chart="operator",
                repository="https://charts.example.com",
                version="1.0.0",
            ),
            stacks.Manifests(
                key="config",
                depends_on=["operator"],
                manifests=[{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a", "namespace": "d"}}],
            ),
        ],
        want="test: charts ['operator'] are depended on but don't set wait",
    ),
    CheckCase(
        name="KeyedTolerations",
        reason="Tolerations that each name a taint key pass, in values and manifests alike.",
        components=[
            stacks.Chart(
                key="operator",
                release="mp-operator",
                namespace="operator",
                chart="operator",
                repository="https://charts.example.com",
                version="1.0.0",
                values={"daemonsets": {"tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists"}]}},
            ),
            stacks.Manifests(
                key="pod",
                manifests=[
                    {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "metadata": {"name": "a", "namespace": "d"},
                        "spec": {"tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists"}]},
                    }
                ],
            ),
        ],
        want=None,
    ),
    CheckCase(
        name="WildcardInValues",
        reason="A chart whose values carry a keyless toleration fails the check.",
        components=[
            stacks.Chart(
                key="operator",
                release="mp-operator",
                namespace="operator",
                chart="operator",
                repository="https://charts.example.com",
                version="1.0.0",
                values={"daemonsets": {"tolerations": [{"operator": "Exists"}]}},
            ),
        ],
        want="test: components ['operator'] carry a keyless toleration",
    ),
    # One chart has hidden the wildcard under global.systemNodeTolerations.
    CheckCase(
        name="WildcardUnderOtherKey",
        reason="A keyless toleration under a key ending in Tolerations fails the check.",
        components=[
            stacks.Chart(
                key="operator",
                release="mp-operator",
                namespace="operator",
                chart="operator",
                repository="https://charts.example.com",
                version="1.0.0",
                values={"global": {"systemNodeTolerations": [{"operator": "Exists"}]}},
            ),
        ],
        want="test: components ['operator'] carry a keyless toleration",
    ),
    CheckCase(
        name="WildcardInManifests",
        reason="A Manifests whose manifest carries a keyless toleration fails the check.",
        components=[
            stacks.Manifests(
                key="daemonset",
                manifests=[
                    {
                        "apiVersion": "apps/v1",
                        "kind": "DaemonSet",
                        "metadata": {"name": "a", "namespace": "d"},
                        "spec": {"template": {"spec": {"tolerations": [{"operator": "Exists"}]}}},
                    }
                ],
            ),
        ],
        want="test: components ['daemonset'] carry a keyless toleration",
    ),
]


@pytest.mark.parametrize("case", CHECK_CASES, ids=lambda case: case.name)
def test_check(case: CheckCase) -> None:
    """check rejects a component list that can't be rendered safely."""
    if case.want is None:
        stacks.check("test", case.components)
        return
    with pytest.raises(ValueError, match=f"^{re.escape(case.want)}$"):
        stacks.check("test", case.components)


FILTER_DRIVERS_CASES = [
    FilterDriversCase(
        name="NoDrivers",
        reason="With no drivers claimed, only the untagged components survive.",
        components=[
            _chart(key="common", depends_on=[], dra_drivers=[]),
            _chart(key="nvidia", depends_on=[], dra_drivers=["gpu.nvidia.com"]),
        ],
        dra_drivers=[],
        want=[_chart(key="common", depends_on=[], dra_drivers=[])],
    ),
    FilterDriversCase(
        name="OneOfItsDrivers",
        reason="A component survives when any one of its drivers is claimed, and one tagged with none of them doesn't.",
        components=[
            _chart(key="nfd", depends_on=[], dra_drivers=["gpu.nvidia.com", "gpu.amd.com"]),
            _chart(key="nvidia", depends_on=[], dra_drivers=["gpu.nvidia.com"]),
            _chart(key="cpu", depends_on=[], dra_drivers=["dra.cpu"]),
        ],
        dra_drivers=["gpu.amd.com"],
        want=[_chart(key="nfd", depends_on=[], dra_drivers=["gpu.nvidia.com", "gpu.amd.com"])],
    ),
    FilterDriversCase(
        name="ObservedDependedOn",
        reason="An Observed component survives while a component left in the list depends on it.",
        components=[
            _observed(),
            _chart(key="nvidia", depends_on=["cert-manager"], dra_drivers=["gpu.nvidia.com"]),
        ],
        dra_drivers=["gpu.nvidia.com"],
        want=[
            _observed(),
            _chart(key="nvidia", depends_on=["cert-manager"], dra_drivers=["gpu.nvidia.com"]),
        ],
    ),
    FilterDriversCase(
        name="ObservedNotDependedOn",
        reason="An Observed component goes when the filter removes every component that depends on it.",
        components=[
            _observed(),
            _chart(key="nvidia", depends_on=["cert-manager"], dra_drivers=["gpu.nvidia.com"]),
        ],
        dra_drivers=["dra.cpu"],
        want=[],
    ),
]


@pytest.mark.parametrize("case", FILTER_DRIVERS_CASES, ids=lambda case: case.name)
def test_filter_drivers(case: FilterDriversCase) -> None:
    """filter_drivers keeps the components a cluster claiming dra_drivers needs."""
    assert stacks.filter_drivers(case.components, case.dra_drivers) == case.want, case.reason


SUPPORTED_DRIVERS_CASES = [
    SupportedDriversCase(
        name="TaggedComponents",
        reason="A list supports the union of its components' drivers, and its Observed components add none.",
        components=[
            _observed(),
            _chart(key="common", depends_on=[], dra_drivers=[]),
            _chart(key="nfd", depends_on=[], dra_drivers=["gpu.nvidia.com", "gpu.amd.com"]),
            stacks.Manifests(
                key="quota",
                manifests=[
                    {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "q", "namespace": "cpu"}}
                ],
                dra_drivers=["dra.cpu"],
            ),
        ],
        want=frozenset({"gpu.nvidia.com", "gpu.amd.com", "dra.cpu"}),
    ),
    SupportedDriversCase(
        name="NoTags",
        reason="A list with no tagged component supports no drivers.",
        components=[_chart(key="common", depends_on=[], dra_drivers=[])],
        want=frozenset(),
    ),
]


@pytest.mark.parametrize("case", SUPPORTED_DRIVERS_CASES, ids=lambda case: case.name)
def test_supported_drivers(case: SupportedDriversCase) -> None:
    """supported_drivers is the union of a list's dra_drivers tags."""
    assert stacks.supported_drivers(case.components) == case.want, case.reason


CHECK_PINS_CASES = [
    # A device stack adapts the SDK's components, so only the version is held.
    CheckPinsCase(
        name="SameVersion",
        reason="NVIDIA's chart at the SDK's version passes, whatever its key and values.",
        components=[
            stacks.Chart(
                key="dra-driver",
                release="mp-dra-driver-nvidia-gpu",
                namespace="nvidia-dra-driver",
                chart="dra-driver-nvidia-gpu",
                repository="oci://registry.k8s.io/dra-driver-nvidia/charts",
                version="0.5.0",
                values={"nvidiaDriverRoot": "/run/nvidia/driver"},
            ),
        ],
        want=None,
    ),
    CheckPinsCase(
        name="OtherVersion",
        reason="NVIDIA's chart at another version than the SDK's fails.",
        components=[
            stacks.Chart(
                key="node-feature-discovery",
                release="mp-node-feature-discovery",
                namespace="node-feature-discovery",
                chart="node-feature-discovery",
                repository="https://kubernetes-sigs.github.io/node-feature-discovery/charts",
                version="0.18.3",
            ),
        ],
        want="test: node-feature-discovery pins node-feature-discovery 0.18.3, but the SDK pins 0.19.0",
    ),
    # The SDK pins only the GPU Operator's version, not a whole Chart.
    CheckPinsCase(
        name="GPUOperatorVersion",
        reason="The GPU Operator at another version than the SDK's fails.",
        components=[
            stacks.Chart(
                key="gpu-operator",
                release="mp-gpu-operator",
                namespace="gpu-operator",
                chart="gpu-operator",
                repository="https://helm.ngc.nvidia.com/nvidia",
                version="v25.10.1",
            ),
        ],
        want="test: gpu-operator pins gpu-operator v25.10.1, but the SDK pins v26.7.1",
    ),
    CheckPinsCase(
        name="OtherCharts",
        reason="A chart the SDK doesn't pin, a Manifests and an Observed pass.",
        components=[
            _chart(key="nvsentinel", depends_on=[], dra_drivers=["gpu.nvidia.com"]),
            stacks.Manifests(
                key="quota",
                manifests=[{"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "q", "namespace": "n"}}],
            ),
            _observed(),
        ],
        want=None,
    ),
]


@pytest.mark.parametrize("case", CHECK_PINS_CASES, ids=lambda case: case.name)
def test_check_pins(case: CheckPinsCase) -> None:
    """check_pins rejects a stack pinning one of NVIDIA's charts at another version than the SDK's."""
    if case.want is None:
        nvidia.check_pins("test", case.components)
        return
    with pytest.raises(ValueError, match=f"^{re.escape(case.want)}$"):
        nvidia.check_pins("test", case.components)
