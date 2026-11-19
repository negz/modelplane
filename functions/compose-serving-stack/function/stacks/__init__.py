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

"""The serving stack component lists, and the join that selects them.

A stack and a cluster's device stacks name the lists to join:
common.py for the components on every cluster, the stack's own file,
and, for an Existing cluster, existing.py's device stacks, which a
cluster provider installs instead on a cluster it provisions. See
design/serving-stack-generation.md and design/cluster-providers.md.
"""

from typing import Any, Literal

from modelplane_sdk.stacks import Component, check, filter_drivers

from function.stacks import common, dynamo, existing, metrics, standard
from function.stacks.metrics import BUILTIN_MAPPINGS

__all__ = [
    "BUILTIN_MAPPINGS",
    "DeviceStacks",
    "Stack",
    "join",
    "metrics",
    "stacks",
]

# The values of ServingStack spec.stack and spec.deviceStacks, so a wrong
# value fails type checking at the caller.
Stack = Literal["Standard", "Dynamo"]
DeviceStacks = Literal["Managed", "External"]

_STACKS: dict[Stack, list[Component]] = {
    "Standard": standard.COMPONENTS,
    "Dynamo": dynamo.COMPONENTS,
}


def stacks() -> list[Stack]:
    """The stacks a serving stack can be joined for."""
    return list(_STACKS)


def join(
    stack: Stack,
    device_stacks: DeviceStacks,
    dra_drivers: list[str] | None = None,
    service: dict[str, Any] | None = None,
) -> list[Component]:
    """Join the component lists for a stack and a cluster's device stacks.

    Managed device stacks add existing.py's. service is the cluster
    provider's settings for the gateways' LoadBalancer Service, which
    common.gateway_proxy builds the EnvoyProxy from.

    Fails closed on a list modelplane_sdk.stacks.check rejects, so a
    broken list fails every join, not just the driver combination that
    trips it.

    dra_drivers filters the tagged components: a tagged component
    survives only when one of its drivers is listed, an untagged one
    always does. None disables filtering, for the tests and integrity
    checks that inspect the full join; the function always passes a
    list. A listed driver no component carries installs nothing and is
    never rejected: on an Existing cluster the user may have installed
    it, and on a provisioned one the provider does.
    """
    if stack not in _STACKS:
        raise ValueError(f"unknown stack {stack!r}; known: {', '.join(_STACKS)}")

    joined = [
        *(existing.COMPONENTS if device_stacks == "Managed" else []),
        *common.COMPONENTS,
        common.gateway_proxy(service or {}),
        *_STACKS[stack],
    ]
    check(f"{device_stacks}/{stack}", joined)

    if dra_drivers is None:
        return joined
    return filter_drivers(joined, dra_drivers)
