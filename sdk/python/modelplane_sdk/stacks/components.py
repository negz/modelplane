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

"""The types every stack component list is made of.

A stack file - hand-written, or written by a build-time generator such
as modelplane_sdk.aicr - is a COMPONENTS list of these entries. The list
is the intermediate representation: a generator is one producer of it,
never the format, so a hand-written file and a generated one are
interchangeable. modelplane_sdk.stacks.render renders a Chart as a
provider-helm Release, a Manifests as provider-kubernetes Objects, and
an Observed as an observe-only Object; see
design/serving-stack-generation.md.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chart:
    """A Helm chart a stack installs.

    `key` names the composed resource and `release` the Helm release
    (via provider-helm's external-name), mp-<chart>. Neither is the
    upstream catalog's component name, so a component's identity
    survives upstream renames: provider-helm upgrades in place only
    while the release name holds still. The mp- prefix reserves a
    namespace so Modelplane can't adopt a same-named release a user
    already runs.

    `depends_on` names components this one needs, by key, resolved
    against the list the stack joins. It drives ordering in
    both directions: teardown (a dependency outlives its dependents) and
    install (a dependent is first created once its dependencies report
    Ready).

    `wait` renders as provider-helm's wait (helm --wait): the release
    reports Ready only once its workloads roll out, not when Helm
    accepts the manifests. Every chart another component depends on
    sets it, so the install gate orders on health rather than deploy.
    The AICR generator derives it from the dependency edges, and
    hand-written files state it.

    `dra_drivers` names the DRA drivers whose device stacks this
    component belongs to (gpu.nvidia.com, dra.cpu, ...). An untagged
    component is needed regardless of devices and always installs; a
    tagged one installs only when a node pool's InferenceClass claims one
    of its drivers. A component can serve several stacks (NFD labels
    nodes for every GPU driver). The tags on a dependency edge are
    constrained so filtering can't break it; check()'s docstring states
    and enforces that rule.
    """

    key: str
    release: str
    namespace: str
    chart: str
    repository: str
    version: str
    wait: bool = False
    depends_on: list[str] = field(default_factory=list)
    values: dict[str, Any] | None = None
    dra_drivers: list[str] = field(default_factory=list)


@dataclass
class Manifests:
    """Raw manifests a stack applies.

    For the parts of the stack that have never been chart-shaped: CRDs
    vendored from upstream releases, the gateway objects, the
    kai-scheduler Queues, the ModelExpress server bundle. `key` and
    `depends_on` behave as on Chart.

    `ready` is an optional CEL query over the observed manifest, for
    when readiness must reflect a controller-populated status field. It
    applies to every doc in the entry, so an entry with one carries a
    single manifest: a Service or ServiceAccount beside it has no status
    to satisfy it.

    `dra_drivers` behaves as on Chart.
    """

    key: str
    manifests: list[dict[str, Any]]
    depends_on: list[str] = field(default_factory=list)
    ready: str | None = None
    dra_drivers: list[str] = field(default_factory=list)


@dataclass
class Observed:
    """A component another stack installs, which this one depends on.

    A provider's device stack and Modelplane's serving stack install onto
    the same cluster, each from its own composition, so a dependency
    edge between them can't name the other's Release. An Observed entry
    stands in for it: the renderer observes `manifest` on the cluster,
    never creating, changing or deleting it, and it reports Ready when
    the `ready` CEL query passes. A dependent's install gate then waits
    on that, as it would on a component of its own. It creates no
    teardown Usage, because the dependency isn't this stack's to delete.

    The serving stack's cert-manager is the dependency every NVIDIA
    device stack has; modelplane_sdk.stacks.CERT_MANAGER observes it.
    """

    key: str
    manifest: dict[str, Any]
    ready: str


# A plain assignment rather than a `type` statement: the packages
# declare requires-python >=3.11, and the `type` keyword needs 3.12.
# Type checkers treat this as an implicit alias either way.
Component = Chart | Manifests | Observed


def doc_keys(component: Component) -> list[str]:
    """Composed-resource keys for a component, in manifest order.

    A Chart, an Observed, and a Manifests entry with one doc, render
    under the entry's key verbatim. A multi-doc Manifests renders one Object per
    doc, keyed `<key>-<metadata.name>`. Renaming a key deletes and
    recreates the composed resource - for an Object that deletes the
    remote object - so a bundle growing from one doc to several (or
    shrinking to one) renames its keys; keep that reviewable.
    """
    if isinstance(component, Chart | Observed) or len(component.manifests) == 1:
        return [component.key]
    return [f"{component.key}-{doc['metadata']['name']}" for doc in component.manifests]
