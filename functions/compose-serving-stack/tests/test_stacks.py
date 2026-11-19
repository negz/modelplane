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

"""Tests for the serving stack component lists.

The join itself is the assertion: join() fails closed on a list
modelplane_sdk.stacks.check rejects, which holds every component list,
core's and each provider's, to the same rules. So iterating every stack
and device stacks pair gates every list without involving fn.py. The
tests after it are properties of join's driver filter. Then tables for
what join keeps for a cluster's DRA drivers, the EnvoyProxy it builds
from the cluster provider's Service settings, and a stack it doesn't
know.
"""

import dataclasses

import pytest
from function import stacks
from function.stacks import existing
from modelplane_sdk.stacks import components

# Every value of ServingStack spec.deviceStacks.
DEVICE_STACKS = ("Managed", "External")


@dataclasses.dataclass
class JoinCase:
    """A test case for the components stacks.join keeps for a cluster's DRA drivers."""

    name: str
    reason: str
    stack: stacks.Stack
    device_stacks: stacks.DeviceStacks
    dra_drivers: list[str]
    want: list[str]


@dataclasses.dataclass
class JoinFailsClosedCase:
    """A test case for stacks.join rejecting a stack."""

    name: str
    reason: str
    stack: str
    want: str


@dataclasses.dataclass
class GatewayProxyCase:
    """A test case for the EnvoyProxy stacks.join builds from a cluster provider's Service settings."""

    name: str
    reason: str
    service: dict | None
    want: dict


def _rendered(*, joined: list[components.Component]) -> list[components.Chart | components.Manifests]:
    """The components of joined the serving stack installs, rather than observes.

    The core join holds no Observed components, which only a device stack
    depending on the serving stack's cert-manager carries.
    """
    return [c for c in joined if not isinstance(c, components.Observed)]


@pytest.mark.parametrize("device_stacks", DEVICE_STACKS)
@pytest.mark.parametrize("stack", stacks.stacks())
def test_join_not_empty(stack: stacks.Stack, device_stacks: stacks.DeviceStacks) -> None:
    """Every stack and device stacks pair joins, passing stacks.check, into a non-empty stack."""
    assert stacks.join(stack, device_stacks), "a joined stack can't be empty"


@pytest.mark.parametrize("device_stacks", DEVICE_STACKS)
@pytest.mark.parametrize("stack", stacks.stacks())
def test_join_filter_empty(stack: stacks.Stack, device_stacks: stacks.DeviceStacks) -> None:
    """An empty draDrivers list keeps exactly the untagged components."""
    joined = _rendered(joined=stacks.join(stack, device_stacks))
    assert stacks.join(stack, device_stacks, dra_drivers=[]) == [c for c in joined if not c.dra_drivers]


@pytest.mark.parametrize("device_stacks", DEVICE_STACKS)
@pytest.mark.parametrize("stack", stacks.stacks())
def test_join_filter_all(stack: stacks.Stack, device_stacks: stacks.DeviceStacks) -> None:
    """Listing every tagged driver reproduces the unfiltered join."""
    joined = stacks.join(stack, device_stacks)
    drivers = sorted({d for c in _rendered(joined=joined) for d in c.dra_drivers})
    assert stacks.join(stack, device_stacks, dra_drivers=drivers) == joined


# Keys rather than whole components: COMPOSE_CASES in test_fn.py covers how
# each component renders, and these are about which of them a cluster gets.
JOIN_CASES = [
    JoinCase(
        name="ManagedNVIDIA",
        reason="On Managed device stacks, gpu.nvidia.com installs NFD, NVIDIA's DRA driver and its quota, and nothing AMD's or the CPU driver's.",
        stack="Standard",
        device_stacks="Managed",
        dra_drivers=["gpu.nvidia.com"],
        want=[
            "node-feature-discovery",
            "nvidia-dra-driver-gpu",
            "dra-driver-critical-pods-quota",
            "cert-manager",
            "envoy-gateway",
            "ai-gateway-crds",
            "ai-gateway",
            "gaie-crds",
            "gateway-namespace",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "cluster-agent",
            "gateway-proxy",
            "leader-worker-set",
        ],
    ),
    JoinCase(
        name="ManagedAMD",
        reason="On Managed device stacks, gpu.amd.com installs AMD's operator and its exporter config, and the NFD both GPU stacks share.",
        stack="Standard",
        device_stacks="Managed",
        dra_drivers=["gpu.amd.com"],
        want=[
            "node-feature-discovery",
            "amd-metrics-exporter-config",
            "amd-gpu-operator",
            "cert-manager",
            "envoy-gateway",
            "ai-gateway-crds",
            "ai-gateway",
            "gaie-crds",
            "gateway-namespace",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "cluster-agent",
            "gateway-proxy",
            "leader-worker-set",
        ],
    ),
    JoinCase(
        name="ManagedCPU",
        reason="On Managed device stacks, dra.cpu installs the CPU driver and its quota, and neither GPU stack, NFD included.",
        stack="Standard",
        device_stacks="Managed",
        dra_drivers=["dra.cpu"],
        want=[
            "cert-manager",
            "envoy-gateway",
            "ai-gateway-crds",
            "ai-gateway",
            "gaie-crds",
            "gateway-namespace",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "dra-driver-cpu",
            "dra-driver-cpu-critical-pods-quota",
            "cluster-agent",
            "gateway-proxy",
            "leader-worker-set",
        ],
    ),
    # The user owns an Existing cluster and may have installed any driver,
    # so a typo surfaces when its ResourceClaim can't allocate.
    JoinCase(
        name="ManagedUnknownDriver",
        reason="On Managed device stacks, a driver no component carries installs nothing and isn't rejected.",
        stack="Standard",
        device_stacks="Managed",
        dra_drivers=["gpu.example.com"],
        want=[
            "cert-manager",
            "envoy-gateway",
            "ai-gateway-crds",
            "ai-gateway",
            "gaie-crds",
            "gateway-namespace",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "cluster-agent",
            "gateway-proxy",
            "leader-worker-set",
        ],
    ),
    JoinCase(
        name="ExternalAllDrivers",
        reason="On External device stacks, every driver claimed installs only the CPU driver every cluster runs, leaving the GPU stacks to the cluster provider.",
        stack="Standard",
        device_stacks="External",
        dra_drivers=["dra.cpu", "gpu.amd.com", "gpu.nvidia.com"],
        want=[
            "cert-manager",
            "envoy-gateway",
            "ai-gateway-crds",
            "ai-gateway",
            "gaie-crds",
            "gateway-namespace",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "dra-driver-cpu",
            "dra-driver-cpu-critical-pods-quota",
            "cluster-agent",
            "gateway-proxy",
            "leader-worker-set",
        ],
    ),
]


@pytest.mark.parametrize("case", JOIN_CASES, ids=lambda case: case.name)
def test_join(case: JoinCase) -> None:
    """join keeps the components a cluster's device stacks and DRA drivers need."""
    got = stacks.join(case.stack, case.device_stacks, dra_drivers=case.dra_drivers)
    assert [c.key for c in got] == case.want, case.reason


# The Literal type rejects this at type-checking time; this exercises the
# runtime guard behind it, which catches the API and the stacks package
# disagreeing on a value.
JOIN_FAILS_CLOSED_CASES = [
    JoinFailsClosedCase(
        name="UnknownStack",
        reason="A stack the stacks package doesn't know fails the join.",
        stack="Turbo",
        want="unknown stack 'Turbo'",
    ),
]


@pytest.mark.parametrize("case", JOIN_FAILS_CLOSED_CASES, ids=lambda case: case.name)
def test_join_fails_closed(case: JoinFailsClosedCase) -> None:
    """join rejects a stack it doesn't know."""
    with pytest.raises(ValueError, match=case.want):
        stacks.join(case.stack, "Managed")  # ty: ignore[invalid-argument-type]  # cases pass values outside the Literal


def test_join_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """join fails closed on a list modelplane_sdk.stacks.check rejects, whatever the drivers."""
    broken = [
        components.Chart(
            key="dep",
            release="mp-dep",
            namespace="d",
            chart="dep",
            repository="r",
            version="1",
            wait=True,
            dra_drivers=["gpu.nvidia.com"],
        ),
        components.Chart(
            key="user",
            release="mp-user",
            namespace="d",
            chart="user",
            repository="r",
            version="1",
            depends_on=["dep"],
        ),
    ]
    monkeypatch.setattr(existing, "COMPONENTS", broken)
    with pytest.raises(ValueError, match=r"Managed/Standard: user .* filtering could remove"):
        stacks.join("Standard", "Managed", dra_drivers=[])


# The EnvoyProxy every gateway's LoadBalancer Service is configured by,
# pinned as literals so the Service settings change deliberately.
GATEWAY_PROXY_CASES = [
    GatewayProxyCase(
        name="NoServiceSettings",
        reason="A cluster provider whose load balancers need nothing gets only the externalTrafficPolicy pin.",
        service=None,
        want={
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "EnvoyProxy",
            "metadata": {"name": "cluster-gateway", "namespace": "modelplane-system"},
            "spec": {
                "provider": {
                    "type": "Kubernetes",
                    "kubernetes": {"envoyService": {"externalTrafficPolicy": "Cluster"}},
                },
            },
        },
    ),
    GatewayProxyCase(
        name="Annotations",
        reason="A cluster provider's Service annotations reach the EnvoyProxy beside the externalTrafficPolicy pin.",
        service={"annotations": {"lb.example.com/scheme": "internet-facing"}},
        want={
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "EnvoyProxy",
            "metadata": {"name": "cluster-gateway", "namespace": "modelplane-system"},
            "spec": {
                "provider": {
                    "type": "Kubernetes",
                    "kubernetes": {
                        "envoyService": {
                            "annotations": {"lb.example.com/scheme": "internet-facing"},
                            "externalTrafficPolicy": "Cluster",
                        },
                    },
                },
            },
        },
    ),
]


@pytest.mark.parametrize("case", GATEWAY_PROXY_CASES, ids=lambda case: case.name)
def test_gateway_proxy(case: GatewayProxyCase) -> None:
    """The joined stack's gateway-proxy EnvoyProxy carries the cluster provider's Service settings."""
    joined = stacks.join("Standard", "External", service=case.service)
    proxy = next(c for c in joined if isinstance(c, components.Manifests) and c.key == "gateway-proxy")
    assert proxy.manifests == [case.want], case.reason
