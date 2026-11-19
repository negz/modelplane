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

"""Compose a provider's device stack onto a cluster it provisioned.

A cluster provider installs the device stacks for the DRA drivers its
pools claim, from a component list fixed at build time, through a
namespaced XR of its own whose spec carries:

    secrets:     # [{type, name, key, namespace?}], as a ProvisionedCluster
                 # reports them: a Kubeconfig entry, and optionally an
                 # identity entry whose type is a provider-kubernetes
                 # identity type.
    draDrivers:  # [str], the drivers the cluster's classes claim.

compose renders that list onto the cluster: ProviderConfigs built from
the secrets, the components the claimed drivers need, the Usages
ordering their teardown and protecting the ProviderConfigs, and
readiness. It composes every component without Delete, because the
provider deletes the cluster itself, and the components with it.
"""

from typing import Any, Literal

from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from modelplane_sdk import contract, usages
from modelplane_sdk.stacks import Component, check, filter_drivers, render

# Composed-resource keys of the two ProviderConfigs.
PROVIDER_CONFIG_HELM = "provider-config-helm"
PROVIDER_CONFIG_KUBERNETES = "provider-config-kubernetes"

# Management policies without Delete, for what a provider composes onto a
# cluster it provisioned: its device stack's components, and anything else
# installed through provider-helm or provider-kubernetes. These exist only
# to configure the cluster, and are only ever deleted because the cluster
# is being torn down. Deleting them then means asking provider-helm and
# provider-kubernetes to reach a cluster that may be going away, which can
# wedge their finalizers and hang the composite. Orphaning them sidesteps
# that: they go with the cluster. Typed with literals, so a provider can
# pass it to its generated models' managementPolicies.
ORPHAN: list[Literal["Observe", "Create", "Update"]] = ["Observe", "Create", "Update"]


def provider_configs(name: str, namespace: str, secrets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """A provider-helm and a provider-kubernetes ProviderConfig for the cluster.

    Keyed by composed-resource key. The kubeconfig provides the cluster
    endpoint and CA certificate. If an identity secret is present it's
    layered on as an identity block, so the providers authenticate as
    that cloud identity instead of relying on whatever auth is baked
    into the kubeconfig. A secret is in namespace unless it says
    otherwise.
    """
    kubeconfig = next(s for s in secrets if s["type"] == contract.SECRET_TYPE_KUBECONFIG)
    identity = next((s for s in secrets if s["type"] != contract.SECRET_TYPE_KUBECONFIG), None)

    def spec() -> dict[str, Any]:
        s: dict[str, Any] = {
            "credentials": {
                "source": "Secret",
                "secretRef": {
                    "name": kubeconfig["name"],
                    "namespace": kubeconfig.get("namespace") or namespace,
                    "key": kubeconfig["key"],
                },
            },
        }
        if identity:
            s["identity"] = {
                "type": identity["type"],
                "source": "Secret",
                "secretRef": {
                    "name": identity["name"],
                    "namespace": identity.get("namespace") or namespace,
                    "key": identity["key"],
                },
            }
        return s

    return {
        PROVIDER_CONFIG_KUBERNETES: {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "ProviderConfig",
            "metadata": {"name": name},
            "spec": spec(),
        },
        PROVIDER_CONFIG_HELM: {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "ProviderConfig",
            "metadata": {"name": name},
            "spec": spec(),
        },
    }


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse, components: list[Component]) -> None:
    """Compose a device stack XR's components onto its cluster.

    components is the provider's whole list, which compose checks and
    then filters to the drivers spec.draDrivers names. The XR is Ready
    once the ProviderConfigs are observed and every component reports
    Ready.
    """
    xr = resource.struct_to_dict(req.observed.composite.resource)
    check(xr["kind"], components)
    spec = xr["spec"]

    name = resource.child_name(xr["metadata"]["name"], "cluster")
    for key, pc in provider_configs(name, xr["metadata"]["namespace"], spec["secrets"]).items():
        resource.update(rsp.desired.resources[key], pc)
        # Ready once observed, not on arrival: on the first reconcile they and
        # the Usages are the only desired resources, and marking them ready
        # would let the XR report Ready before a single component exists.
        if key in req.observed.resources:
            rsp.desired.resources[key].ready = fnv1.READY_TRUE

    wanted = filter_drivers(components, spec.get("draDrivers") or [])
    gate = PROVIDER_CONFIG_HELM in req.observed.resources and PROVIDER_CONFIG_KUBERNETES in req.observed.resources
    rendered = render.compose(req, rsp, wanted, provider_config=name, gate=gate, management_policies=ORPHAN)
    render.compose_usages(rsp, wanted)
    render.mark_ready(req, rsp, rendered)
    usages.protect_provider_configs(req, rsp)
