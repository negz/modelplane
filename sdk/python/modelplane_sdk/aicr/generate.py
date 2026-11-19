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

"""Resolve, bundle and transform one cloud's AICR recipe into a stack file.

One `aicr recipe` at the Cloud's coordinate and one `aicr bundle` with
Modelplane's managed values forced via --set, then a transform that
classifies every component, asserts every managed value, scopes every
wildcard toleration, and emits a COMPONENTS list. The accelerator is
held out of the coordinate, so the generator also resolves once per
covered accelerator and unions the Kubernetes constraint floors,
failing if the strictest outruns the Cloud's default.

Classification detail (drops, managed paths, dropped dependencies,
manifests, constraint floors) goes to stderr; the generated file
carries provenance in its header.
"""

import ast
import difflib
import hashlib
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

from modelplane_sdk.aicr.cloud import Cloud
from modelplane_sdk.aicr.tables import (
    AICR_PIN,
    ALLOW,
    DROP,
    MANAGED,
    MODELPLANE_VALUES,
    OBSERVED,
    REGISTRY_YAML,
    TOLERATIONS,
    TRIM,
    WILDCARD_TOLERATION,
    ValuePath,
    Values,
)
from modelplane_sdk.stacks import dcgm

# Sentinel for "this path is not set at all" in lookups.
ABSENT = "<absent>"

# The Apache header addlicense expects on every .py file, emitted onto
# the generated files so `nix run .#fix` and the license check leave
# them alone. Byte-identical to the header addlicense writes.
LICENSE_HEADER = """\
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
# limitations under the License."""


def tolerations_key(key: str) -> bool:
    """Return whether a values or manifest key holds a toleration list."""
    return key == "tolerations" or key.endswith("Tolerations")


def check_tables(cloud: Cloud) -> None:
    """Refuse a Cloud whose tables collide with the shared ones.

    A component classified twice, or a toleration path scoped twice,
    has no one owner to review on a bump. A managed override naming no
    shared entry would be silently ignored.
    """
    if cloud.aicr != AICR_PIN:
        sys.exit(
            f"{cloud.service}: synced against aicr {cloud.aicr}, but modelplane_sdk.aicr is "
            f'synced against {AICR_PIN}: re-sync the Cloud (see "Bumping aicr" in CONTRIBUTING.md)'
        )
    classified = [*ALLOW, *DROP, *OBSERVED, *cloud.allow, *cloud.drop]
    twice = sorted({name for name in classified if classified.count(name) > 1})
    if twice:
        sys.exit(f"{cloud.service}: components classified more than once: {twice} - fail closed")
    for name, paths in cloud.tolerations.items():
        twice = sorted(set(paths) & set(TOLERATIONS.get(name, {})))
        if twice:
            sys.exit(f"{cloud.service}: {name} tolerations {twice} already scoped in TOLERATIONS - fail closed")
    shared = {(component, path) for component, entries in MANAGED.items() for path, _, _ in entries}
    for component, path in cloud.managed:
        if (component, path) not in shared:
            sys.exit(
                f"{cloud.service}: managed override {component}.{'.'.join(path)} names no MANAGED "
                "entry; add it to managed_extra - fail closed"
            )


def write_catalog(cloud: Cloud, workdir: pathlib.Path) -> pathlib.Path:
    """Lay the Cloud's overlays out where `aicr recipe --data` reads them."""
    catalog = workdir / "catalog"
    (catalog / "overlays").mkdir(parents=True)
    (catalog / "registry.yaml").write_text(REGISTRY_YAML)
    for name, overlay in cloud.overlays.items():
        (catalog / "overlays" / name).write_text(overlay)
    return catalog


def recipe_extra(cloud: Cloud, catalog: pathlib.Path) -> list[str]:
    """Extra `aicr recipe` flags a cloud needs.

    Its catalog overlays and profile values. Applied to every recipe
    call, floor probes included, so an overlay's union-totality and
    coherence checks run on each.
    """
    flags = []
    if cloud.overlays:
        flags += ["--data", str(catalog)]
    for profile in cloud.profiles:
        flags += ["--profile", profile]
    return flags


def managed(cloud: Cloud) -> dict[str, list[tuple[ValuePath, object, str]]]:
    """The MANAGED table with per-cloud expected values substituted."""
    out: dict[str, list[tuple[ValuePath, object, str]]] = {}
    for component, entries in MANAGED.items():
        out[component] = [(path, *cloud.managed.get((component, path), (value, why))) for path, value, why in entries]
    for component, entries in cloud.managed_extra.items():
        out.setdefault(component, []).extend(entries)
    return out


def deep_merge(base: Values | None, override: Values | None) -> Values:
    """Merge override into base, recursing into dicts both have."""
    out = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def lookup(values: Values, path: ValuePath) -> object:
    """Read values[path], or ABSENT if any segment is missing."""
    node: object = values
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return ABSENT
        node = node[key]
    return node


def set_path(values: Values, path: ValuePath, value: object) -> None:
    """Set values[path], creating the dicts along it."""
    node = values
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def prune_nulls(values: Values, prefix: tuple[str, ...] = ()) -> list[str]:
    """Delete null-valued keys from hydrated values, returning their paths.

    A null in a recipe's values is a Helm tombstone: when Helm merges
    values files it deletes the key from a lower layer, and aicr's
    bundler preserves nulls expecting that path. The bundler already
    merged the layers a tombstone aimed at, so by the time values land
    here a null only leaks into the rendered manifests - harmless where
    a field is nullable, fatal where a CRD's schema forbids it (the
    Prometheus CRD rejects storage.emptyDir null). So every null goes,
    reported as a finding. One consequence: a tombstone aimed at a
    chart default lets that default come back; if that is ever wrong
    for a path, pin an explicit value in MODELPLANE_VALUES instead.
    """
    pruned = []
    for key in list(values):
        val = values[key]
        if val is None:
            del values[key]
            pruned.append(".".join((*prefix, key)))
        elif isinstance(val, dict):
            pruned += prune_nulls(val, (*prefix, key))
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, dict):
                    pruned += prune_nulls(item, (*prefix, f"{key}[{i}]"))
    return pruned


def scope_tolerations(name: str, values: Values, table: dict[str, list[Values]]) -> list[str]:
    """Rewrite the bundler's wildcard tolerations per the component's table.

    Mutates values in place and returns findings. A wildcard at a path
    the table doesn't classify fails the run - fail closed, like
    ALLOW/DROP.
    """
    findings = []

    def walk(node: object, prefix: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if tolerations_key(key) and val == WILDCARD_TOLERATION:
                    dotted = ".".join((*prefix, key))
                    if dotted not in table:
                        sys.exit(
                            f"wildcard toleration at unclassified path "
                            f"{name}.{dotted}: classify it in TOLERATIONS - fail closed"
                        )
                    scoped = table[dotted]
                    node[key] = [dict(t) for t in scoped]
                    findings.append(
                        f"scoped toleration: {name}.{dotted} -> "
                        + ("the GPU taint" if scoped else "none (system pool only)")
                    )
                else:
                    walk(val, (*prefix, key))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, (*prefix, f"[{i}]"))

    walk(values, ())
    return findings


def scope_manifest_tolerations(cloud: Cloud, name: str, manifest: Values) -> list[str]:
    """scope_tolerations for a rendered manifest, keyed by kind and name."""
    key = (name, manifest["kind"], manifest["metadata"]["name"])
    findings = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, val in node.items():
                if tolerations_key(k) and val == WILDCARD_TOLERATION:
                    if key not in cloud.manifest_tolerations:
                        sys.exit(
                            f"wildcard toleration in manifest {key[1]}/{key[2]} of "
                            f"{name}: classify it in the Cloud's manifest_tolerations - fail closed"
                        )
                    scoped = cloud.manifest_tolerations[key]
                    node[k] = [dict(t) for t in scoped]
                    findings.append(
                        f"scoped toleration: {name} manifest {key[1]}/{key[2]} -> "
                        + ("the GPU taint" if scoped else "none (system pool only)")
                    )
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(manifest)
    return findings


def version_key(version: str | None) -> tuple[int, ...]:
    """A version's numeric parts, for comparison."""
    return tuple(int(x) for x in re.findall(r"\d+", version or "0"))


def managed_sets(cloud: Cloud) -> list[str]:
    """Render the managed table as `aicr bundle --set` flags."""
    flags = []
    for component, entries in managed(cloud).items():
        for path, value, _ in entries:
            if (component, path) in cloud.catalog_owned:
                continue
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
            flags += ["--set", f"{component}:{'.'.join(path)}={rendered}"]
    return flags


def bundle_values(bundle_dir: pathlib.Path, name: str) -> Values:
    """Load the hydrated values aicr bundle wrote for a component."""
    matches = sorted(bundle_dir.glob(f"[0-9][0-9][0-9]-{name}/values.yaml"))
    if not matches:
        return {}
    return yaml.safe_load(matches[0].read_text()) or {}


def k8s_floor(recipe: Values) -> tuple[list[str], list[str]]:
    """A recipe's Kubernetes floor, and its other constraints verbatim."""
    floors_found, others = [], []
    for constraint in recipe.get("constraints") or []:
        name, value = constraint.get("name", ""), str(constraint.get("value", ""))
        if name == "K8s.server.version":
            match = re.search(r"\d+(?:\.\d+)*", value)
            if not match:
                sys.exit(f"unparseable Kubernetes constraint {value!r} - fail closed")
            floors_found.append(match.group(0))
        else:
            others.append(f"{name} {value}".strip())
    return floors_found, others


def run(argv: list[str]) -> None:
    """Run a command, exiting with its stderr if it fails."""
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.exit(f"{' '.join(argv)}\n{proc.stderr.strip()}")


def check_pin() -> None:
    """Refuse any aicr but the pinned release, before resolving anything."""
    if not shutil.which("aicr"):
        sys.exit("aicr not found on PATH (run via `nix run .#stacks`)")
    proc = subprocess.run(["aicr", "--version"], capture_output=True, text=True, check=False)
    match = re.search(r"aicr version (\S+)", proc.stdout)
    if not match:
        sys.exit(f"could not parse `aicr --version` output: {proc.stdout!r}")
    if match.group(1) != AICR_PIN:
        sys.exit(
            f"aicr {match.group(1)} on PATH, but this generator is synced "
            f'against {AICR_PIN}: follow "Bumping aicr" in CONTRIBUTING.md '
            "(and update nix/aicr.nix) rather than mixing releases"
        )


def transform(cloud: Cloud, recipe: Values, bundle_dir: pathlib.Path) -> tuple[list[Values], list[str]]:
    """Turn a hydrated recipe into Chart and Manifests entries."""
    findings = []
    components = []
    refs = {c["name"]: c for c in recipe["componentRefs"]}
    table = managed(cloud)
    allow = {**ALLOW, **cloud.allow}
    drop = {**DROP, **cloud.drop}

    for name in table:
        if name not in refs:
            findings.append(f"managed component {name} absent from the recipe")

    # AICR's install sequence is discarded as ordering (installs stay
    # concurrent; depends_on drives teardown) but kept as a stable
    # file order.
    for name in recipe["deploymentOrder"]:
        ref = refs[name]
        if name in drop:
            findings.append(f"dropped: {name} ({drop[name]})")
            continue
        if name in OBSERVED:
            findings.append(f"dropped: {name} ({OBSERVED[name][1]}; dependents keep their edge)")
            continue
        if name not in allow:
            sys.exit(f"unknown component {name!r}: classify it in ALLOW or DROP, or the Cloud's allow or drop")
        key, dra_drivers = allow[name]

        values = deep_merge(bundle_values(bundle_dir, name), ref.get("overrides"))
        for path in TRIM.get(name, []):
            blob = lookup(values, path)
            if isinstance(blob, str):
                lines = [line.rstrip() for line in blob.splitlines()]
                set_path(values, path, "\n".join(line for line in lines if line) + "\n")
                findings.append(f"trimmed: {name}.{'.'.join(path)}: blank lines and trailing whitespace")
        for path, expected, why in table.get(name, []):
            dotted = f"{name}.{'.'.join(path)}"
            actual = lookup(values, path)
            if actual != expected:
                sys.exit(
                    f"managed path {dotted} is {actual!r}, expected "
                    f"{expected!r}: the value did not land in the bundle - fail closed"
                )
            via = "the catalog fork" if (name, path) in cloud.catalog_owned else "aicr bundle --set"
            findings.append(f"managed path: {dotted} = {expected} ({why}; via {via})")
        for path, value, why in MODELPLANE_VALUES.get(name, []):
            if lookup(values, path) not in (ABSENT, value):
                sys.exit(
                    f"modelplane value {name}.{'.'.join(path)} collides with a "
                    "recipe-carried value - reconcile the tables, fail closed"
                )
            set_path(values, path, value)
            findings.append(f"modelplane value: {name}.{'.'.join(path)} ({why})")
        for dotted in prune_nulls(values):
            findings.append(f"pruned null: {name}.{dotted} (a Helm tombstone; see prune_nulls)")
        findings += scope_tolerations(name, values, {**TOLERATIONS.get(name, {}), **cloud.tolerations.get(name, {})})

        depends = []
        for dep in ref.get("dependencyRefs", []):
            if dep in allow:
                depends.append(allow[dep][0])
            elif dep in OBSERVED:
                depends.append(OBSERVED[dep][0])
                findings.append(f"observed dep: {name} -> {dep} (the provider joins the Observed entry it names)")
            elif dep in drop:
                findings.append(
                    f"dropped dep: {name} -> {dep} (dropped; the join with "
                    "the hand-written files must satisfy it or nothing needed it)"
                )
            else:
                sys.exit(f"dependency {name} -> {dep!r} names an unclassified component - fail closed")

        namespace = cloud.namespace_pins.get(key, ref["namespace"])
        if namespace != ref["namespace"]:
            findings.append(f"pinned namespace: {name}: {ref['namespace']} -> {namespace} (see namespace_pins)")

        # The bundler ships pre-manifests at sync-wave N-1 - resources
        # the chart's own pods depend on, like the critical-pods
        # ResourceQuota it synthesizes for components that need a
        # critical priority class (a cloud may reject
        # system-node-critical pods in a namespace without one;
        # aicr#915). The chart depends on them, so they exist before
        # its pods and outlive it on teardown.
        pre = sorted(bundle_dir.glob(f"[0-9][0-9][0-9]-{name}-pre/templates/*.yaml"))
        if pre:
            # The chart's namespace leads the entry: aicr's helm
            # deployer installs the pre chart with --create-namespace,
            # and without the namespace the pre Objects can't apply
            # while the chart that would create it waits on them - a
            # deadlock the first run that shipped them hit.
            manifests = [
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": namespace},
                },
            ]
            for f in pre:
                manifests.extend(d for d in yaml.safe_load_all(f.read_text()) if d)
            for doc in manifests:
                findings += scope_manifest_tolerations(cloud, name, doc)
            findings.append(
                f"pre-manifests: {name}: {len(manifests)} objects (namespace + {', '.join(f.name for f in pre)})"
            )
            pre_entry = {
                "type": "Manifests",
                "key": f"{key}-pre-manifests",
                "manifests": manifests,
            }
            # The chart's own tags, because the chart depends on this
            # entry - stacks.check()'s dependency-tagging rule.
            if dra_drivers:
                pre_entry["dra_drivers"] = dra_drivers
            components.append(pre_entry)
            depends.append(f"{key}-pre-manifests")

        entry = {
            "type": "Chart",
            "key": key,
            # provider-helm upgrades in place only while the release
            # name and namespace hold still; mp- reserves a namespace
            # (see the design and Cloud.namespace_pins).
            "release": f"mp-{ref['chart']}",
            "namespace": namespace,
            "chart": ref["chart"],
            "repository": ref["source"],
            "version": ref["version"],
        }
        if depends:
            entry["depends_on"] = depends
        if dra_drivers:
            entry["dra_drivers"] = dra_drivers
        if values:
            entry["values"] = values
        components.append(entry)

        # The operator's exporter ClusterRole grants pods only
        # (assets/state-dcgm-exporter/0210_clusterrole.yaml in v26.7.x),
        # so the stack composes the slice RBAC the exporter's DRA mode
        # needs until the operator ships it. Where the exporter is off
        # (via a Cloud's managed_extra) the RBAC would bind a
        # ServiceAccount that never exists.
        if name == "gpu-operator" and lookup(values, ("dcgmExporter", "enabled")) is not False:
            # One entry per object: the two docs share a metadata.name,
            # which a multi-doc entry keys its Objects by.
            for kind, extra in (
                (
                    "ClusterRole",
                    {
                        "rules": [
                            {
                                "apiGroups": ["resource.k8s.io"],
                                "resources": ["resourceslices"],
                                "verbs": ["get", "list", "watch"],
                            },
                        ],
                    },
                ),
                (
                    "ClusterRoleBinding",
                    {
                        "roleRef": {
                            "apiGroup": "rbac.authorization.k8s.io",
                            "kind": "ClusterRole",
                            "name": "nvidia-dcgm-exporter-read-resourceslices",
                        },
                        "subjects": [
                            {
                                "kind": "ServiceAccount",
                                "name": "nvidia-dcgm-exporter",
                                "namespace": namespace,
                            },
                        ],
                    },
                ),
            ):
                doc: Values = {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": kind,
                    "metadata": {"name": "nvidia-dcgm-exporter-read-resourceslices"},
                }
                doc.update(extra)
                entry = {
                    "type": "Manifests",
                    "key": f"dcgm-exporter-{kind.lower()}",
                    "depends_on": [key],
                    "manifests": [doc],
                }
                if dra_drivers:
                    entry["dra_drivers"] = dra_drivers
                components.append(entry)
            findings.append(
                "manifests: composed nvidia-dcgm-exporter-read-resourceslices "
                "(the gpu-operator's exporter ClusterRole grants no resourceslices)"
            )

        # Components are not always just charts: on some clouds the
        # gpu-operator carries a toolkit-hardening manifest. The bundle
        # materializes them under <NNN>-<name>-post/templates/, and
        # they render as provider-kubernetes Objects. Like the
        # pre-manifests above, the bundle is the source of truth, not
        # the recipe: the registry can supply default manifestFiles a
        # componentRef never declares. A manifest that disappears on a
        # bump shows up as a dropped key in the generated diff and the
        # golden inventory test.
        rendered = sorted(bundle_dir.glob(f"[0-9][0-9][0-9]-{name}-post/templates/*.yaml"))
        if rendered:
            manifests = []
            for f in rendered:
                manifests.extend(d for d in yaml.safe_load_all(f.read_text()) if d)
            for doc in manifests:
                findings += scope_manifest_tolerations(cloud, name, doc)
            findings.append(f"manifests: {name}: {len(manifests)} objects ({', '.join(f.name for f in rendered)})")
            post_entry = {
                "type": "Manifests",
                "key": f"{key}-manifests",
                "depends_on": [key],
                "manifests": manifests,
            }
            # Depending on a tagged chart requires carrying its tags, so
            # the filter keeps or drops the pair together.
            if dra_drivers:
                post_entry["dra_drivers"] = dra_drivers
            components.append(post_entry)

    # The bundle is consumed fail-closed, like the recipe: every
    # component directory must belong to a classified component and use
    # a layout this transform reads (main, -pre, -post). A new suffix or
    # an unclassified name means aicr grew a bundle shape we would
    # otherwise silently drop - the pre-manifests were exactly that
    # once (a critical-pods quota discarded unread).
    for bundle_entry in sorted(bundle_dir.iterdir()):
        if not bundle_entry.is_dir() or not re.match(r"\d{3}-", bundle_entry.name):
            continue
        rest = bundle_entry.name[4:]
        for suffix in ("-pre", "-post"):
            if rest.endswith(suffix):
                rest = rest[: -len(suffix)]
                break
        if rest not in allow and rest not in drop and rest not in OBSERVED:
            sys.exit(f"bundle directory {bundle_entry.name!r} belongs to no classified component - fail closed")

    # A chart another component depends on renders with helm --wait, so
    # its Ready means healthy and the function's install gate orders
    # dependents on health rather than deploy. Derived from the kept
    # edges, so the graph and the waits can't drift apart.
    depended_on = {dep for entry in components for dep in entry.get("depends_on", [])}
    for entry in components:
        if entry["type"] == "Chart" and entry["key"] in depended_on:
            entry["wait"] = True
            findings.append(f"wait: {entry['key']} (another component depends on it)")

    return components, findings


def floors(
    cloud: Cloud,
    base_recipe: Values,
    workdir: pathlib.Path,
    catalog: pathlib.Path,
) -> tuple[str | None, list[str]]:
    """Union the Kubernetes floors over the covered accelerators.

    The cloud coordinate holds the accelerator out, which drops the
    accelerator's constraints - the strictest. Resolve once per covered
    accelerator, union the floors, and fail if the strictest outruns
    the Cloud's default.
    """
    findings = []
    united, others = k8s_floor(base_recipe)
    for floor in cloud.floors:
        united.append(floor)
        findings.append(f"Modelplane-owned floor {floor} (see the Cloud's floors)")
    for other in others:
        findings.append(f"constraint (unenforced here): {other}")
    for accelerator in cloud.accelerators:
        recipe_file = workdir / f"{cloud.service}-{accelerator}-floor.yaml"
        flags = ["--service", cloud.service, "--accelerator", accelerator, "--intent", "inference"]
        if cloud.os:
            flags += ["--os", cloud.os]
        run(["aicr", "recipe", *flags, *recipe_extra(cloud, catalog), "-o", str(recipe_file)])
        got, others = k8s_floor(yaml.safe_load(recipe_file.read_text()))
        united += got
        for other in others:
            findings.append(f"constraint on {accelerator} (unenforced here): {other}")

    floor = max(united, key=version_key, default=None)
    if floor is None:
        findings.append("no Kubernetes floor asserted by any covered recipe")
        return None, findings
    if version_key(floor) > version_key(cloud.k8s_default):
        sys.exit(
            f"{cloud.service}: strictest Kubernetes floor {floor} outruns the "
            f"cloud cluster XRD default {cloud.k8s_default} - fail closed"
        )
    findings.append(
        f"Kubernetes floor {floor} (union over no-accelerator, "
        f"{', '.join(cloud.accelerators)}); XRD default {cloud.k8s_default}"
    )
    return floor, findings


def check_namespace_holds(cloud: Cloud, components: list[Values], out: pathlib.Path) -> None:
    """Fail the run when a component's namespace moves from the last generated file.

    A moved namespace is a wedged upgrade, not a reinstall: provider-helm
    finds no release at the new coordinates, its fresh install trips over
    the old release's ownership annotations on cluster-scoped objects,
    and the stack stays not Ready on every existing cluster while CI -
    fresh clusters only - passes. Pin the old namespace in the Cloud's
    namespace_pins, or land the move as a documented migration that
    deletes this check's trip wire deliberately.
    """
    if not out.exists():
        return
    previous = {}
    for node in ast.walk(ast.parse(out.read_text())):
        if isinstance(node, ast.Call):
            kw = {k.arg: k.value.value for k in node.keywords if isinstance(k.value, ast.Constant)}
            if "key" in kw and "namespace" in kw:
                previous[kw["key"]] = kw["namespace"]
    for entry in components:
        was = previous.get(entry.get("key"))
        if was and was != entry.get("namespace"):
            sys.exit(
                f"{cloud.service}: {entry['key']} moved namespace {was!r} -> "
                f"{entry['namespace']!r}, which Helm can't do: existing "
                "clusters would wedge on a fresh-install ownership error "
                "(NVIDIA/aicr#2899). Pin the old namespace in the Cloud's "
                "namespace_pins or land the move as a documented migration - fail closed"
            )


def check_dcgm(cloud: Cloud, components: list[Values]) -> None:
    """Fail the run when the cloud's DCGM metrics CSV isn't the SDK's.

    The hand-written device stacks run the same exporter with the SDK's
    copy (modelplane_sdk.stacks.dcgm), so an aicr bump that moves the
    CSV moves that copy in the same change.
    """
    for entry in components:
        if entry.get("key") == "gpu-operator":
            csv = lookup(entry["values"], ("dcgmExporter", "config", "data"))
            if isinstance(csv, str):
                break
    else:
        sys.exit(f"{cloud.service}: no dcgm-exporter metrics CSV in the gpu-operator values - fail closed")
    if csv != dcgm.DCGM_METRICS_CSV:
        diff = difflib.unified_diff(
            dcgm.DCGM_METRICS_CSV.splitlines(keepends=True),
            csv.splitlines(keepends=True),
            "modelplane_sdk.stacks.dcgm",
            f"{cloud.service}'s recipe",
        )
        sys.exit(
            f"{cloud.service}: the dcgm-exporter metrics CSV differs from "
            "modelplane_sdk.stacks.dcgm.DCGM_METRICS_CSV; update the SDK's copy "
            f"in the same change - fail closed\n{''.join(diff)}"
        )


def pyfmt(value: object) -> str:
    """Render a value as a Python literal.

    Nearly repr, and layout is ruff format's job (the .#stacks app runs
    it over the output). The one thing ruff can't do is the reason this
    exists: a multi-line string must be emitted triple-quoted, because
    repr escapes its newlines into one enormous line that ruff will
    never split - and a per-line diff of a config blob (dcgm-exporter's
    metrics CSV) is what makes a bump reviewable.
    """
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k!r}: {pyfmt(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(pyfmt(item) for item in value) + "]"
    if isinstance(value, str) and "\n" in value and '"""' not in value and not value.endswith('"'):
        return f'"""{value}"""'
    return repr(value)


def emit(
    cloud: Cloud,
    components: list[Values],
    recipe: Values,
    digest: str,
    floor: str | None,
    out: pathlib.Path,
) -> None:
    """Write the generated module."""
    fields = [
        "key",
        "release",
        "namespace",
        "chart",
        "repository",
        "version",
        "wait",
        "depends_on",
        "dra_drivers",
        "values",
        "manifests",
    ]
    names = ["Chart", "Component"]
    if any(entry["type"] == "Manifests" for entry in components):
        names.append("Manifests")
    lines = [
        LICENSE_HEADER,
        "",
        f"# Generated by generate.py from aicr {recipe['metadata']['version']}. Do not edit.",
        "# Regenerate with `nix run .#stacks`.",
        "#",
        "# The AICR-derived device stack. Its depends_on edges onto",
        "# components another stack installs name modelplane_sdk.stacks",
        "# Observed entries, such as CERT_MANAGER, which the provider joins",
        "# into the list it renders.",
        "#",
        f"# Recipe: {' -> '.join(recipe['metadata']['appliedOverlays'])} (sha256:{digest[:12]})",
        f"# Kubernetes floor: {floor or 'none asserted'}; {cloud.service} XRD default {cloud.k8s_default}",
        "",
        f"from modelplane_sdk.stacks import {', '.join(names)}",
        "",
        "# DRA drivers this cloud supports that arrive outside the device",
        "# stack, for the provider's supported-drivers table (see",
        "# generate.py's external_drivers).",
        f"EXTERNAL_DRIVERS: list[str] = {pyfmt(cloud.external_drivers)}",
        "",
        "COMPONENTS: list[Component] = [",
    ]
    for entry in components:
        lines.append(f"    {entry['type']}(")
        for field in fields:
            if field not in entry:
                continue
            lines.append(f"        {field}={pyfmt(entry[field])},")
        lines.append("    ),")
    lines += ["]", ""]
    text = "\n".join(lines)
    ast.parse(text)  # a generated file that doesn't parse fails the build
    out.write_text(text)


def regenerate(cloud: Cloud, workdir: pathlib.Path, catalog: pathlib.Path, out: pathlib.Path) -> None:
    """Resolve, bundle, transform, check and emit one cloud."""
    flags = ["--service", cloud.service, "--intent", "inference"]
    if cloud.os:
        flags += ["--os", cloud.os]
    flags += recipe_extra(cloud, catalog)
    recipe_file = workdir / f"{cloud.service}-inference.yaml"
    bundle_dir = workdir / f"{cloud.service}-bundle"

    run(["aicr", "recipe", *flags, "-o", str(recipe_file)])
    run(
        [
            "aicr",
            "bundle",
            "--recipe",
            str(recipe_file),
            "--output",
            str(bundle_dir),
            *managed_sets(cloud),
            *cloud.bundle_flags,
        ]
    )

    recipe = yaml.safe_load(recipe_file.read_text())
    components, findings = transform(cloud, recipe, bundle_dir)
    check_namespace_holds(cloud, components, out)
    check_dcgm(cloud, components)
    floor, floor_findings = floors(cloud, recipe, workdir, catalog)
    for finding in findings + floor_findings:
        print(f"[{cloud.service}] {finding}", file=sys.stderr)

    digest = hashlib.sha256(recipe_file.read_bytes()).hexdigest()
    emit(cloud, components, recipe, digest, floor, out)


def main(cloud: Cloud, out: pathlib.Path) -> None:
    """Regenerate a cloud's AICR-derived device stack into out."""
    check_pin()
    check_tables(cloud)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Recipes and bundles are intermediates: reproducible from the
    # pinned aicr's embedded catalog, digested into the file headers,
    # and not worth checking in.
    with tempfile.TemporaryDirectory(prefix="device-stack-gen-") as tmp:
        workdir = pathlib.Path(tmp)
        catalog = write_catalog(cloud, workdir)
        regenerate(cloud, workdir, catalog, out)
        print(f"generated {out}")
