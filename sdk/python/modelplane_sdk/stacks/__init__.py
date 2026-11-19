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

"""Stacks: lists of components a composition function installs on a cluster.

Modelplane's serving stack and each cluster provider's device stack are
both a list of Chart, Manifests and Observed entries, fixed at build
time. This package holds the types, the checks every list must pass,
the DRA driver filter, and the components shared across stacks: the
vendor device stacks in nvidia and amd, and CERT_MANAGER, through which
a device stack depends on the serving stack's cert-manager. render
turns a list into composed resources.
"""

from modelplane_sdk.stacks.components import Chart, Component, Manifests, Observed, doc_keys

__all__ = [
    "CERT_MANAGER",
    "Chart",
    "Component",
    "Manifests",
    "Observed",
    "check",
    "doc_keys",
    "filter_drivers",
    "supported_drivers",
]

# The serving stack installs cert-manager on every cluster, as release
# mp-cert-manager in namespace cert-manager with fullnameOverride
# cert-manager, so its webhook is this Deployment. A device stack
# component that creates cert-manager resources, or whose chart does,
# depends on this key. It's Ready once the webhook serves, which is
# when cert-manager admits the resources its dependents create.
CERT_MANAGER = Observed(
    key="cert-manager",
    manifest={
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "cert-manager-webhook", "namespace": "cert-manager"},
    },
    ready="has(object.status) && has(object.status.availableReplicas) && object.status.availableReplicas >= 1",
)


def check(name: str, components: list[Component]) -> None:
    """Raise ValueError if a component list can't be rendered safely.

    Fails closed, at import or test time rather than on a cluster: on a
    key two components both use, on a composed-resource key two
    components both render, on a depends_on edge naming a component the
    list doesn't have - which catches a generator allowlist that dropped
    something another component needs - and on a dependency that is
    neither untagged nor tagged with every driver its dependent carries,
    which filter_drivers could then remove from under it. Then on a Chart
    whose release isn't mp-<chart>, a Manifests with no manifests, a
    Manifests with a readiness query and more than one manifest, a Chart
    another component depends on that doesn't set wait, and a keyless
    toleration anywhere in a Chart's values or a Manifests' manifests.
    Chart, Manifests and _wildcard_tolerations say why each matters.

    name identifies the list in the error, such as "Existing/Dynamo".
    """
    keys = [c.key for c in components]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ValueError(f"{name}: duplicate component keys {duplicates}")

    # The composed-resource keys a component renders under (one per
    # manifest for a multi-doc bundle) must be unique too, or two
    # components would fight over one desired resource.
    rendered = [k for c in components for k in doc_keys(c)]
    duplicates = sorted({k for k in rendered if rendered.count(k) > 1})
    if duplicates:
        raise ValueError(f"{name}: duplicate composed-resource keys {duplicates}")

    by_key = {c.key: c for c in components}
    for c in components:
        if isinstance(c, Observed):
            continue
        for dep in c.depends_on:
            if dep not in by_key:
                raise ValueError(f"{name}: {c.key} depends on {dep!r}, which the list does not have")
            dependency = by_key[dep]
            # An Observed dependency is never filtered out.
            if isinstance(dependency, Observed):
                continue
            # The `c.dra_drivers and` leg: an untagged dependent is kept
            # by every filter, which no tagged dependency survives.
            if dependency.dra_drivers and not (c.dra_drivers and set(c.dra_drivers) <= set(dependency.dra_drivers)):
                raise ValueError(
                    f"{name}: {c.key} (drivers {c.dra_drivers or 'untagged'}) depends on "
                    f"{dep!r} (drivers {dependency.dra_drivers}), which filtering could remove from under it"
                )

    _check_components(name, components)


def _check_components(name: str, components: list[Component]) -> None:
    """Raise ValueError on a component check rejects for how it's written rather than how it's keyed or tagged."""
    charts = [c for c in components if isinstance(c, Chart)]
    manifests = [c for c in components if isinstance(c, Manifests)]

    misnamed = [c.key for c in charts if c.release != f"mp-{c.chart}"]
    if misnamed:
        raise ValueError(f"{name}: charts {misnamed} aren't released as mp-<chart>")

    empty = [c.key for c in manifests if not c.manifests]
    if empty:
        raise ValueError(f"{name}: Manifests {empty} carry no manifests")

    multi = [c.key for c in manifests if c.ready is not None and len(c.manifests) != 1]
    if multi:
        raise ValueError(f"{name}: Manifests {multi} have a readiness query but more than one manifest")

    depended_on = {dep for c in [*charts, *manifests] for dep in c.depends_on}
    not_waiting = [c.key for c in charts if c.key in depended_on and not c.wait]
    if not_waiting:
        raise ValueError(f"{name}: charts {not_waiting} are depended on but don't set wait")

    wildcards = [c.key for c in charts if _wildcard_tolerations(c.values)]
    wildcards += [c.key for c in manifests if _wildcard_tolerations(c.manifests)]
    if wildcards:
        raise ValueError(f"{name}: components {wildcards} carry a keyless toleration")


def _wildcard_tolerations(node: object) -> bool:
    """Whether node holds a keyless toleration.

    A keyless toleration tolerates every taint, so its pod lands on
    tainted GPU nodes, where a control-plane pod squats on accelerated
    capacity and its eviction stalls autoscaler scale-down. This matches
    a list under any key named tolerations or ending in Tolerations: one
    chart has hidden the wildcard under global.systemNodeTolerations.
    """
    if isinstance(node, list):
        return any(_wildcard_tolerations(item) for item in node)
    if not isinstance(node, dict):
        return False
    for key, value in node.items():
        named = key == "tolerations" or key.endswith("Tolerations")
        if named and isinstance(value, list) and any(not (isinstance(t, dict) and "key" in t) for t in value):
            return True
        if _wildcard_tolerations(value):
            return True
    return False


def supported_drivers(components: list[Component]) -> frozenset[str]:
    """The DRA drivers whose device stacks a component list carries.

    The union of the list's dra_drivers tags. A provider's supported
    drivers are these plus any its cluster composition installs outside
    its device stack, such as an EFA driver a node pool's fabric needs.
    """
    return frozenset(d for c in components if not isinstance(c, Observed) for d in c.dra_drivers)


def filter_drivers(components: list[Component], dra_drivers: list[str]) -> list[Component]:
    """The components a cluster whose classes claim dra_drivers needs.

    A tagged component survives only when one of its drivers is
    listed; an untagged one always does, and so does an Observed one
    that something left in the list still depends on.
    """
    wanted = set(dra_drivers)
    kept = [c for c in components if isinstance(c, Observed) or not c.dra_drivers or wanted & set(c.dra_drivers)]
    needed = {dep for c in kept if not isinstance(c, Observed) for dep in c.depends_on}
    return [c for c in kept if not isinstance(c, Observed) or c.key in needed]
