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

"""Render a stack's components as composed resources on a remote cluster.

A Chart renders as a provider-helm Release, a Manifests as one
provider-kubernetes Object per doc, and an Observed as an observe-only
Object, all through the ProviderConfigs a composition function builds
for the cluster. Ordering derives from depends_on in both directions:
compose gates each component's first creation on its dependencies
reporting Ready, and compose_usages holds each dependency until its
dependents are gone.

Composed resources are plain dicts rather than generated models: the
provider-helm and provider-kubernetes models belong to whichever
project imports this package, which generates them from its own
dependencies.
"""

from collections.abc import Sequence
from typing import Any

from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from modelplane_sdk.stacks.components import Chart, Component, Observed, doc_keys

# Label key every rendered Release and Object carries, valued with its
# composed-resource key, so Usage resourceSelectors can name any
# component (or one doc of a bundle) mechanically.
LABEL_RESOURCE = "modelplane.ai/resource"

# Annotation provider-helm reads as the Helm release name. A Chart
# carries the release name (mp-<chart>): stable across chart-version
# upgrades so provider-helm upgrades in place, short enough that
# chart-derived names stay inside the 63-character label limit, and
# mp- reserves a namespace so Modelplane can't adopt a same-named
# release a user already runs.
_EXTERNAL_NAME_ANNOTATION = "crossplane.io/external-name"

# The (apiVersion, kind) a component's composed resources render as,
# used by the derived Usages' of/by references.
RELEASE = ("helm.m.crossplane.io/v1beta1", "Release")
OBJECT = ("kubernetes.m.crossplane.io/v1alpha1", "Object")

# What an Observed component's Object may do: read the object, never
# create, change or delete it.
_OBSERVE = ["Observe"]


def release(chart: Chart, provider_config: str, management_policies: Sequence[str] | None = None) -> dict[str, Any]:
    """A provider-helm Release installing chart through provider_config."""
    for_provider: dict[str, Any] = {
        "chart": {"name": chart.chart, "repository": chart.repository, "version": chart.version},
        "namespace": chart.namespace,
    }
    if chart.wait:
        # Helm --wait: Ready means the workloads rolled out, so the
        # install gate orders dependents on health, not deploy. The
        # default 5m can be tight for the big monitoring charts on a
        # fresh cluster pulling images.
        for_provider["wait"] = True
        for_provider["waitTimeout"] = "10m"
    if chart.values:
        for_provider["values"] = chart.values
    spec: dict[str, Any] = {
        "providerConfigRef": {"kind": "ProviderConfig", "name": provider_config},
        "forProvider": for_provider,
    }
    if management_policies:
        spec["managementPolicies"] = list(management_policies)
    return {
        "apiVersion": RELEASE[0],
        "kind": RELEASE[1],
        "metadata": {
            "annotations": {_EXTERNAL_NAME_ANNOTATION: chart.release},
            "labels": {LABEL_RESOURCE: chart.key},
        },
        "spec": spec,
    }


def k8s_object(
    provider_config: str,
    manifest: dict[str, Any],
    *,
    labels: dict[str, str] | None = None,
    ready_when: str | None = None,
    management_policies: Sequence[str] | None = None,
) -> dict[str, Any]:
    """A provider-kubernetes Object applying manifest through provider_config.

    Readiness defaults to SuccessfulCreate (the Object is Ready once
    applied), which suits resources with no meaningful runtime
    readiness. Pass ready_when for an Object whose readiness must
    reflect a controller-populated field of the observed manifest - it
    selects the DeriveFromCelQuery policy with that query, which also
    keeps provider-kubernetes re-observing on its fast poll until the
    query passes.
    """
    spec: dict[str, Any] = {
        "providerConfigRef": {"kind": "ProviderConfig", "name": provider_config},
        "forProvider": {"manifest": manifest},
    }
    if management_policies:
        spec["managementPolicies"] = list(management_policies)
    if ready_when is not None:
        spec["readiness"] = {"policy": "DeriveFromCelQuery", "celQuery": ready_when}
    obj: dict[str, Any] = {"apiVersion": OBJECT[0], "kind": OBJECT[1], "spec": spec}
    if labels:
        obj["metadata"] = {"labels": labels}
    return obj


def usage(of_ref: tuple[str, str], of_key: str, by_ref: tuple[str, str], by_key: str) -> dict[str, Any]:
    """A Usage holding `of` (a dependency) until `by` is gone."""
    return {
        "apiVersion": "protection.crossplane.io/v1beta1",
        "kind": "Usage",
        "spec": {
            "of": {
                "apiVersion": of_ref[0],
                "kind": of_ref[1],
                "resourceSelector": {"matchControllerRef": True, "matchLabels": {LABEL_RESOURCE: of_key}},
            },
            "by": {
                "apiVersion": by_ref[0],
                "kind": by_ref[1],
                "resourceSelector": {"matchControllerRef": True, "matchLabels": {LABEL_RESOURCE: by_key}},
            },
            "replayDeletion": True,
        },
    }


def is_ready(req: fnv1.RunFunctionRequest, key: str) -> bool:
    """Whether the observed composed resource under key reports Ready."""
    return resource.get_condition(req.observed.resources.get(key), "Ready").status == "True"


def compose(
    req: fnv1.RunFunctionRequest,
    rsp: fnv1.RunFunctionResponse,
    components: list[Component],
    *,
    provider_config: str,
    gate: bool,
    management_policies: Sequence[str] | None = None,
) -> list[str]:
    """Render every component into rsp's desired resources.

    gate is whether the cluster can be reached yet, typically whether
    the ProviderConfigs were observed, so first creation doesn't race
    them. depends_on gates first creation too: a component is created
    only once every doc of every dependency reports Ready, so bring-up
    proceeds in dependency waves (cert-manager before the GPU Operator,
    the GPU Operator before the DRA driver). Once a resource exists it
    always re-composes - the observed check - so a dependency going
    unready later never deletes dependents.

    management_policies applies to every Release and Manifests Object,
    such as a policy without Delete for a cluster whose own deletion
    removes them. Everything carries the LABEL_RESOURCE label the
    derived Usages select on.

    Returns the composed-resource keys it rendered, for mark_ready.
    """
    docs = {c.key: doc_keys(c) for c in components}

    def deps_ready(c: Component) -> bool:
        if isinstance(c, Observed):
            return True
        return all(is_ready(req, key) for dep in c.depends_on for key in docs[dep])

    rendered: list[str] = []
    for c in components:
        open_gate = gate and deps_ready(c)
        if isinstance(c, Chart):
            if not (open_gate or c.key in req.observed.resources):
                continue
            resource.update(rsp.desired.resources[c.key], release(c, provider_config, management_policies))
            rendered.append(c.key)
            continue
        if isinstance(c, Observed):
            if not (open_gate or c.key in req.observed.resources):
                continue
            resource.update(
                rsp.desired.resources[c.key],
                k8s_object(
                    provider_config,
                    c.manifest,
                    labels={LABEL_RESOURCE: c.key},
                    ready_when=c.ready,
                    management_policies=_OBSERVE,
                ),
            )
            rendered.append(c.key)
            continue
        for key, doc in zip(doc_keys(c), c.manifests, strict=True):
            if not (open_gate or key in req.observed.resources):
                continue
            resource.update(
                rsp.desired.resources[key],
                k8s_object(
                    provider_config,
                    doc,
                    labels={LABEL_RESOURCE: key},
                    ready_when=c.ready,
                    management_policies=management_policies,
                ),
            )
            rendered.append(key)
    return rendered


def compose_usages(rsp: fnv1.RunFunctionResponse, components: list[Component]) -> None:
    """Derive teardown-ordering Usages from the components' edges.

    Crossplane applies composed resources concurrently, so without a
    Usage nothing sequences deletion. Each depends_on edge becomes one
    Usage per (dependency doc, dependent doc) pair, holding the
    dependency until the dependent is gone: the kai-scheduler release
    outlives the Queue CRs whose CRD it owns, cert-manager outlives the
    Envoy Gateway release whose webhooks need it, and so on. An edge
    onto an Observed component gets none, because that dependency isn't
    this stack's to delete. Usages reference nothing on the remote
    cluster, so they compose ungated and are ready on arrival.
    """
    refs: dict[str, tuple[str, str]] = {}
    docs: dict[str, list[str]] = {}
    for c in components:
        keys = doc_keys(c)
        docs[c.key] = keys
        for key in keys:
            refs[key] = RELEASE if isinstance(c, Chart) else OBJECT
    observed = {c.key for c in components if isinstance(c, Observed)}

    for c in components:
        if isinstance(c, Observed):
            continue
        for dep in c.depends_on:
            if dep in observed:
                continue
            for of_key in docs[dep]:
                for by_key in docs[c.key]:
                    key = f"usage-{of_key}-by-{by_key}"
                    resource.update(rsp.desired.resources[key], usage(refs[of_key], of_key, refs[by_key], by_key))
                    rsp.desired.resources[key].ready = fnv1.READY_TRUE


def mark_ready(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse, keys: list[str]) -> None:
    """Mark each rendered resource ready once its observed Ready condition is True.

    For a Release that's the Helm release deployed (its workloads rolled
    out, where the chart sets wait), for an Object its readiness policy
    (SuccessfulCreate, or the entry's CEL query).
    """
    for key in keys:
        if is_ready(req, key):
            rsp.desired.resources[key].ready = fnv1.READY_TRUE
