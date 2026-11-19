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

"""What a provider knows about its cloud that AICR's catalog doesn't."""

import dataclasses

from modelplane_sdk.aicr.tables import ValuePath, Values


@dataclasses.dataclass
class Cloud:
    """One cloud's coordinate, and its additions to the shared tables.

    The coordinate is `aicr recipe --service <service> --intent
    inference`, with `--os` where a recipe covers the provider's node
    image. The accelerator is a property of a node pool rather than a
    cluster, so it's held out of the coordinate; the generator resolves
    once per covered accelerator only to union the Kubernetes floors.

    `allow`, `drop` and `tolerations` add to their shared counterparts
    in modelplane_sdk.aicr.tables and fail closed on a collision with
    them. `managed` overrides the value of a shared MANAGED entry and
    fails closed on one that doesn't exist.
    """

    # aicr's --service value. Also names the cloud in findings.
    service: str

    # The aicr release this Cloud's tables and overlays are synced
    # against. Must equal modelplane_sdk.aicr.tables.AICR_PIN, so an
    # SDK bump fails every provider that hasn't re-synced.
    aicr: str

    # The accelerators the floor union resolves over. Widening it is a
    # line here and a regeneration.
    accelerators: list[str]

    # The default Kubernetes version of the clusters the provider
    # creates. The strictest floor fails the run if it outruns this.
    k8s_default: str

    # aicr's --os value, where a recipe covers the provider's node image.
    os: str | None = None

    # Floors the provider owns that no recipe carries, unioned with the
    # recipes'.
    floors: list[str] = dataclasses.field(default_factory=list)

    # Files that replace embedded catalog overlays of the same name, via
    # `aicr recipe --data`: file name -> YAML.
    overlays: dict[str, str] = dataclasses.field(default_factory=dict)

    # `aicr recipe --profile` values, such as gpuStack=dra.
    profiles: list[str] = dataclasses.field(default_factory=list)

    # Extra `aicr bundle` flags.
    bundle_flags: list[str] = dataclasses.field(default_factory=list)

    # Additions to ALLOW and DROP, for components only this cloud's
    # recipes carry.
    allow: dict[str, tuple[str, list[str]]] = dataclasses.field(default_factory=dict)
    drop: dict[str, str] = dataclasses.field(default_factory=dict)

    # Additions to TOLERATIONS, and wildcards in the bundle's rendered
    # manifests, keyed by (recipe component, kind, name), whose
    # tolerations replace every wildcard in the manifest.
    tolerations: dict[str, dict[str, list[Values]]] = dataclasses.field(default_factory=dict)
    manifest_tolerations: dict[tuple[str, str, str], list[Values]] = dataclasses.field(default_factory=dict)

    # (recipe component, values path) -> (value, reason), where this
    # cloud legitimately differs from a shared MANAGED entry.
    managed: dict[tuple[str, ValuePath], tuple[object, str]] = dataclasses.field(default_factory=dict)

    # Managed paths that exist only on this cloud, appended to its
    # table: recipe component -> [(values path, value, reason)].
    managed_extra: dict[str, list[tuple[ValuePath, object, str]]] = dataclasses.field(default_factory=dict)

    # Managed paths this cloud's catalog data already sets, such as a
    # profile value an overlay owns (aicr rejects a --set on a
    # profile-owned path). Asserted in the hydrated bundle like every
    # managed path, just not --set.
    catalog_owned: set[tuple[str, ValuePath]] = dataclasses.field(default_factory=set)

    # Component key -> the namespace its release already occupies on
    # this provider's existing clusters. Helm release identity is (name,
    # namespace), and provider-helm upgrades in place only while both
    # hold still: on a catalog-side namespace move it finds nothing at
    # the new coordinates and runs a fresh install, which then fails on
    # the meta.helm.sh/release-namespace ownership annotations the old
    # release left on cluster-scoped objects - CRDs, ClusterRoles,
    # webhook configurations (NVIDIA/aicr#2899) - wedging every existing
    # cluster while CI, which only sees fresh installs, stays green. So
    # a release's namespace stays where existing clusters have it;
    # moving one is a documented migration, never a bump side effect.
    # check_namespace_holds() fails the run on any unpinned move.
    namespace_pins: dict[str, str] = dataclasses.field(default_factory=dict)

    # DRA drivers the provider supports that arrive outside its device
    # stack, emitted into the generated module so the provider's
    # supported-drivers table can't drift from the generated file.
    external_drivers: list[str] = dataclasses.field(default_factory=list)
