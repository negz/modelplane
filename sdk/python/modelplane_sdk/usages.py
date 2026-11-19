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

"""Protect ProviderConfigs from deletion until their consumers are gone.

protect_provider_configs reads the desired resources a composition
function has accumulated and, for every Helm Release or
provider-kubernetes Object that references a ProviderConfig, composes a
Usage holding that ProviderConfig until the Release or Object is
deleted. Call it last, once everything else is composed. A pipeline
step that runs it over the earlier steps' desired resources works too;
Modelplane's compose-usages function is one.

A Usage's `by` selector resolves to a single resource: the Usage
controller lists candidates, takes the first one, and stops. So one
Usage protects a ProviderConfig against exactly one consumer. To protect
it against all of them this emits one Usage per consumer, pinning each
`by` selector to a single resource with a unique label it stamps on that
resource.
"""

from typing import Any

from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

# Label stamped on a consumer so its Usage's `by` selector resolves to it alone.
# The value is the consumer's composition resource name, which is unique within
# a composition.
LABEL_USAGE_CONSUMER = "modelplane.ai/usage-consumer"

# Consumers that reference a ProviderConfig, keyed by (API group, kind). A
# ProviderConfig shares its consumer's apiVersion (a Release and its
# ProviderConfig are both helm.m.crossplane.io/v1beta1; an Object and its
# ProviderConfig are both kubernetes.m.crossplane.io/v1alpha1), so we carry the
# consumer's apiVersion straight onto the Usage's `of`.
_CONSUMER_KINDS = {
    ("helm.m.crossplane.io", "Release"),
    ("kubernetes.m.crossplane.io", "Object"),
}

_PROVIDER_CONFIG_KIND = "ProviderConfig"


def _api_group(api_version: str) -> str:
    """The group of an apiVersion, e.g. helm.m.crossplane.io/v1beta1 -> ...io."""
    return api_version.split("/", 1)[0]


def protect_provider_configs(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
    """Compose one Usage per ProviderConfig-referencing consumer.

    Iterates rsp's desired resources and, for each Helm Release or
    provider-kubernetes Object that references a ProviderConfig, stamps a
    unique label on it and composes a Usage of the ProviderConfig by that
    single labelled resource.
    """
    # The Usage, its `by` consumer, and the namespaced ProviderConfig it
    # protects all live in the XR's namespace. Composed resources don't
    # always carry an explicit metadata.namespace in desired state
    # (Crossplane defaults it to the XR's), so the XR's namespace is the
    # authoritative source, not the consumer's. Without it we can't place
    # the Usage correctly, so there's nothing safe to do.
    composite = resource.struct_to_dict(req.observed.composite.resource)
    namespace = composite.get("metadata", {}).get("namespace")
    if not namespace:
        return

    # Iterate a snapshot of the keys: we add Usage resources as we go.
    for name in list(rsp.desired.resources.keys()):
        dr = rsp.desired.resources[name]
        d = resource.struct_to_dict(dr.resource)

        api_version = d.get("apiVersion", "")
        kind = d.get("kind", "")
        if (_api_group(api_version), kind) not in _CONSUMER_KINDS:
            continue

        pc = d.get("spec", {}).get("providerConfigRef", {}).get("name")
        if not pc:
            continue

        usage_name = f"usage-pc-{name}"
        if usage_name in rsp.desired.resources:
            # The key is derived from the consumer's composition resource
            # name, which is unique, so this only happens if something
            # already composed a resource under this key. Don't clobber it.
            response.warning(rsp, f"cannot compose Usage: desired resource {usage_name!r} already exists")
            continue

        # Pin the Usage's `by` selector to this one resource. The consumer's
        # composition resource name is unique within the composition, so it
        # makes a stable selector value that resolves to exactly this one.
        labels = d.setdefault("metadata", {}).setdefault("labels", {})
        labels[LABEL_USAGE_CONSUMER] = name
        resource.update(dr, d)

        resource.update(rsp.desired.resources[usage_name], _usage(api_version, kind, pc, name, namespace))
        rsp.desired.resources[usage_name].ready = fnv1.READY_TRUE


def _usage(api_version: str, kind: str, pc: str, consumer: str, namespace: str) -> dict[str, Any]:
    """A Usage of the ProviderConfig by a single labelled consumer.

    `of` references the ProviderConfig by name in the Usage's own namespace,
    where the namespaced ProviderConfig lives. `by` selects the one consumer
    carrying the unique label. matchControllerRef scopes the selector to this
    composition; the label pins it to a single resource within it.
    """
    return {
        "apiVersion": "protection.crossplane.io/v1beta1",
        "kind": "Usage",
        "metadata": {"namespace": namespace},
        "spec": {
            "of": {
                "apiVersion": api_version,
                "kind": _PROVIDER_CONFIG_KIND,
                "resourceRef": {"name": pc},
            },
            "by": {
                "apiVersion": api_version,
                "kind": kind,
                "resourceSelector": {
                    "matchControllerRef": True,
                    "matchLabels": {LABEL_USAGE_CONSUMER: consumer},
                },
            },
            "replayDeletion": True,
        },
    }
