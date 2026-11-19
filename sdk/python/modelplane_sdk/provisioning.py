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

"""What every provider's ProvisionedCluster composition does the same way.

A provider's function for a ProvisionedCluster reads the cluster config,
node classes and capacity reservations the contract references, guards
them against deletion, checks each pool's node class suits its role,
activates the managed resource kinds it composes, provisions the cluster
as an XR of its own, composes its device stack once the cluster is
reachable, and reports the cluster's status. Only the provisioning is the
provider's own. These helpers do the rest, so every provider does it the
same way.

They work on plain dicts: each provider validates what they return into
the models it generates from its own XRDs and from Modelplane's
package.
"""

import dataclasses
from typing import Any

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from modelplane_sdk import contract

# Where Modelplane keeps the namespaced resources it composes for a cluster,
# and where a provider composes its namespaced XRs and secrets.
NAMESPACE = "modelplane-system"

# Composed-resource keys.
ACTIVATION = "activation"
DEVICE_STACK = "device-stack"

# Required-resource keys.
_CONFIG = "config"
_NODE_CLASS = "node-class-"
_RESERVATION = "reservation-"


@dataclasses.dataclass
class Inputs:
    """The objects a ProvisionedCluster references, as dicts.

    node_classes and reservations are keyed by name.
    """

    config: dict[str, Any]
    node_classes: dict[str, dict[str, Any]]
    reservations: dict[str, dict[str, Any]]


def is_contract(req: fnv1.RunFunctionRequest) -> bool:
    """Whether the request is for a ProvisionedCluster.

    A provider's cluster config, node class and reservation kinds need a
    Composition like any XR, and compose nothing. A provider can point
    their Compositions at the same function as its ProvisionedCluster's
    and return early when this is False: an XR that composes nothing is
    Ready.
    """
    xr = resource.struct_to_dict(req.observed.composite.resource)
    return xr.get("apiVersion") == contract.API_VERSION and xr.get("kind") == contract.KIND


def name(req: fnv1.RunFunctionRequest) -> str:
    """The ProvisionedCluster's name, which the provider's cluster XR and device stack share."""
    return resource.struct_to_dict(req.observed.composite.resource)["metadata"]["name"]


def _ref_key(ref: dict[str, Any]) -> str:
    return f"{ref['kind']}.{ref['apiVersion'].split('/')[0]}"


def require_inputs(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
    """Require the cluster config, node classes and reservations the ProvisionedCluster references.

    Crossplane discards a function's desired resources on any reconcile that
    still has unfulfilled required resources, so call this first, before
    composing anything.
    """
    spec = resource.struct_to_dict(req.observed.composite.resource)["spec"]
    refs = [(_CONFIG, spec["configRef"])]
    for pool in spec["nodePools"]:
        refs.append((_NODE_CLASS + pool["nodeClassRef"]["name"], pool["nodeClassRef"]))
        if "reservationRef" in pool:
            refs.append((_RESERVATION + pool["reservationRef"]["name"], pool["reservationRef"]))
    for key, ref in sorted(dict(refs).items()):
        response.require_resources(
            rsp, name=key, api_version=ref["apiVersion"], kind=ref["kind"], match_name=ref["name"]
        )


def resolve_inputs(
    req: fnv1.RunFunctionRequest,
    rsp: fnv1.RunFunctionResponse,
    *,
    config: str,
    node_class: str,
    reservation: str | None = None,
) -> Inputs | None:
    """The objects require_inputs required, or None if the reconcile can't continue.

    config, node_class and reservation name the kinds this provider owns,
    as <kind>.<group>, such as AcmeNodeClass.acme.example.org. A reference
    to any other kind is a node pool another provider's class or
    reservation was given to, which fails the reconcile, as does a missing
    object. Returns None without failing while Crossplane hasn't fetched
    them yet. A failure is a fatal result, so Crossplane keeps the
    resources the ProvisionedCluster already composed.
    """
    spec = resource.struct_to_dict(req.observed.composite.resource)["spec"]
    wrong: list[str] = []
    refs: list[tuple[str, dict[str, Any]]] = []
    if _ref_key(spec["configRef"]) != config:
        wrong.append(f"configRef is a {_ref_key(spec['configRef'])}, not a {config}")
    refs.append((_CONFIG, spec["configRef"]))
    for pool in spec["nodePools"]:
        ref = pool["nodeClassRef"]
        if _ref_key(ref) != node_class:
            wrong.append(f"node pool {pool['name']}'s nodeClassRef is a {_ref_key(ref)}, not a {node_class}")
        refs.append((_NODE_CLASS + ref["name"], ref))
        ref = pool.get("reservationRef")
        if ref is None:
            continue
        if _ref_key(ref) != reservation:
            wanted = f"a {reservation}" if reservation else "nothing, because this provider has no reservations"
            wrong.append(f"node pool {pool['name']}'s reservationRef is a {_ref_key(ref)}, not {wanted}")
        refs.append((_RESERVATION + ref["name"], ref))
    if wrong:
        response.fatal(rsp, "; ".join(wrong))
        return None

    found: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for key, ref in refs:
        # Crossplane only fetches a required resource after the function asks
        # for it. An absent key means it hasn't fetched it yet.
        if key not in req.required_resources:
            return None
        obj = request.get_required_resource(req, key)
        if obj is None:
            missing.append(f"{ref['kind']} {ref['name']}")
            continue
        found[key] = obj
    if missing:
        response.fatal(rsp, f"Missing {', '.join(sorted(set(missing)))}")
        return None

    return Inputs(
        config=found[_CONFIG],
        node_classes={k.removeprefix(_NODE_CLASS): v for k, v in found.items() if k.startswith(_NODE_CLASS)},
        reservations={k.removeprefix(_RESERVATION): v for k, v in found.items() if k.startswith(_RESERVATION)},
    )


def check_roles(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse, accelerated: dict[str, bool]) -> bool:
    """Whether every node pool's node class suits the pool's role.

    accelerated says, for each node class by name, whether it names an
    accelerator. An Accelerator pool needs a node class that does, and a
    CPU pool one that doesn't. Returns False, having failed the reconcile
    naming every pool whose node class doesn't suit it, if any doesn't.
    """
    spec = resource.struct_to_dict(req.observed.composite.resource)["spec"]
    wrong: list[str] = []
    for pool in spec["nodePools"]:
        ref = pool["nodeClassRef"]
        if pool["role"] == contract.ROLE_ACCELERATOR and not accelerated[ref["name"]]:
            wrong.append(
                f"node pool {pool['name']} runs accelerators, but {ref['kind']} {ref['name']} names no accelerator"
            )
        if pool["role"] == contract.ROLE_CPU and accelerated[ref["name"]]:
            wrong.append(
                f"node pool {pool['name']} is a CPU pool, but {ref['kind']} {ref['name']} names an accelerator"
            )
    if wrong:
        response.fatal(rsp, "; ".join(wrong))
        return False
    return True


def compose_input_usages(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse, inputs: Inputs) -> None:
    """Block deletion of the objects the ProvisionedCluster references.

    The provider fails every reconcile while one of them is missing, so
    deleting one in use would stop the cluster reconciling until it's
    recreated. Each ClusterUsage blocks that delete while the cluster
    references the object. Once it doesn't, the function stops composing the
    ClusterUsage, and replayDeletion re-issues a delete that was already
    attempted. They give a reason rather than naming the ProvisionedCluster
    as their `by`, because Crossplane keeps a Usage with a `by` until the
    `by` is gone, and a foreground delete of the cluster waits for its
    composed ClusterUsages, so neither would ever finish.
    """
    objects = [inputs.config, *inputs.node_classes.values(), *inputs.reservations.values()]
    for obj in objects:
        kind = obj["kind"]
        key = f"usage-{kind.lower()}-{obj['metadata']['name']}"
        resource.update(
            rsp.desired.resources[key],
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "ClusterUsage",
                "spec": {
                    "of": {
                        "apiVersion": obj["apiVersion"],
                        "kind": kind,
                        "resourceRef": {"name": obj["metadata"]["name"]},
                    },
                    "reason": f"ProvisionedCluster {name(req)} uses this {kind}",
                    "replayDeletion": True,
                },
            },
        )
        rsp.desired.resources[key].ready = fnv1.READY_TRUE


def compose_activation(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse, kinds: list[str]) -> bool:
    """Activate the cloud managed resource kinds the cluster composes, and say whether they're active.

    kinds are ManagedResourceDefinition names, such as
    clusters.compute.acme.m.example.org. Crossplane only creates a managed
    resource kind's CRD once a ManagedResourceActivationPolicy activates
    it, so a control plane runs a cloud's providers only once it has a
    cluster on that cloud. Keep the list in sync with what the cluster
    composes: a kind composed but missing here never gets a CRD, and
    composing it fails the whole reconcile.

    Returns whether the policy has activated every kind, read from its own
    status.activated, so no ManagedResourceDefinition (each of which
    carries a full CRD schema) has to be pulled into the request. The
    policy reports Healthy even when it matched no definitions, e.g. while
    a provider is still installing, so this checks the kinds are present
    rather than trusting Healthy. Compose the cluster only once it
    returns True, or once the cluster already exists.

    TODO(negz): gate on an Established condition instead once the policy
    reports one (crossplane/crossplane#7822). status.activated means the
    policy set these definitions Active, not that their CRDs exist, so a
    managed resource composed in the window before a CRD is served can
    still fail its apply until the next reconcile.
    """
    resource.update(
        rsp.desired.resources[ACTIVATION],
        {
            "apiVersion": "apiextensions.crossplane.io/v1alpha1",
            "kind": "ManagedResourceActivationPolicy",
            "spec": {"activate": list(kinds)},
        },
    )
    observed = req.observed.resources.get(ACTIVATION)
    if observed is None:
        return False
    activated = (resource.struct_to_dict(observed.resource).get("status") or {}).get("activated") or []
    if not all(kind in activated for kind in kinds):
        return False
    rsp.desired.resources[ACTIVATION].ready = fnv1.READY_TRUE
    return True


def secrets(observed: list[dict[str, Any]] | None, namespace: str = NAMESPACE) -> list[dict[str, Any]] | None:
    """A cluster's secrets as the ProvisionedCluster reports them, or None if there's no kubeconfig.

    observed is a list of {type, name, key} entries, with an optional
    namespace, as a provider's cluster XR reports them. An entry without a
    namespace is in namespace.
    """
    if not observed or not any(s["type"] == contract.SECRET_TYPE_KUBECONFIG for s in observed):
        return None
    return [{**s, "namespace": s.get("namespace") or namespace} for s in observed]


def cluster_secrets(
    req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse, cluster: str
) -> list[dict[str, Any]] | None:
    """Mark the provider's cluster XR ready once it is, and return its secrets as the ProvisionedCluster reports them.

    cluster is the composed-resource key of the XR the provider provisions
    the cluster with, which reports its secrets in status.secrets, as
    secrets reads them. Returns None until the XR is Ready and reports a
    kubeconfig: the contract publishes secrets once the cluster is
    reachable.
    """
    observed = req.observed.resources.get(cluster)
    if observed is None or resource.get_condition(observed, "Ready").status != "True":
        return None
    rsp.desired.resources[cluster].ready = fnv1.READY_TRUE
    return secrets(resource.struct_to_dict(observed.resource)["status"].get("secrets"))


def compose_device_stack(
    req: fnv1.RunFunctionRequest,
    rsp: fnv1.RunFunctionResponse,
    *,
    api_version: str,
    kind: str,
    cluster_secrets: list[dict[str, Any]] | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Compose the provider's device stack XR once the cluster is reachable.

    The XR is the provider's own kind, which modelplane_sdk.devicestack
    renders. Its spec carries the cluster's secrets and the drivers the
    ProvisionedCluster's pools claim, plus extra, any spec fields of the
    provider's own. cluster_secrets is None until the cluster is ready and
    reports a kubeconfig. Once the device stack exists it's kept from its
    own observed spec if the secrets go missing, so a cluster briefly
    losing them doesn't delete it.

    Marked ready once its Ready condition is True.
    """
    observed = req.observed.resources.get(DEVICE_STACK)
    if cluster_secrets is None:
        if observed is None:
            return
        cluster_secrets = resource.struct_to_dict(observed.resource)["spec"]["secrets"]
    xr = resource.struct_to_dict(req.observed.composite.resource)
    spec: dict[str, Any] = {
        "secrets": cluster_secrets,
        "draDrivers": xr["spec"].get("draDrivers") or [],
        **(extra or {}),
    }
    resource.update(
        rsp.desired.resources[DEVICE_STACK],
        {
            "apiVersion": api_version,
            "kind": kind,
            "metadata": {"name": xr["metadata"]["name"], "namespace": NAMESPACE},
            "spec": spec,
        },
    )
    if resource.get_condition(observed, "Ready").status == "True":
        rsp.desired.resources[DEVICE_STACK].ready = fnv1.READY_TRUE


def write_status(
    req: fnv1.RunFunctionRequest,
    rsp: fnv1.RunFunctionResponse,
    *,
    cluster: str,
    cluster_secrets: list[dict[str, Any]] | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Report what Modelplane needs to use the cluster in the ProvisionedCluster's status.

    The status carries cluster_secrets, the cache StorageClass the
    provider's cluster XR, at key cluster, reports in
    status.cache.storageClassName, and extra, any status fields the
    provider sets itself, such as gateway. It's written every reconcile,
    even with nothing to report.
    """
    status: dict[str, Any] = {**(extra or {})}
    if cluster_secrets:
        status["secrets"] = cluster_secrets
    observed = req.observed.resources.get(cluster)
    reported = resource.struct_to_dict(observed.resource).get("status", {}) if observed else {}
    cache = reported.get("cache", {})
    if cache.get("storageClassName"):
        status["cache"] = {"storageClassName": cache["storageClassName"]}
    resource.update_status(rsp.desired.composite, status)
