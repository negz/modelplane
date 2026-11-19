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

"""A provider implements the ProvisionedCluster contract for an InferenceCluster.

Bring-up installs the provider's package beside Modelplane's and applies an
InferenceCluster with a GPU and a CPU pool, from the provider's cluster
config and node classes. The control plane has no credentials for the cloud,
so no cluster ever comes up. What these check is everything up to that:
Modelplane selects the provider's Composition by the config's kind, the
provider accepts what the contract hands it, activates its managed resource
kinds, and composes managed resources of them.
"""

import pytest
from kubernetes import client
from modelplane_sdk import contract

from e2e import clouds, kube, wait


@pytest.fixture(scope="session")
def provisioned(cloud: clouds.Cloud) -> dict:
    """The provider's cluster config, or skip a leg that has no provider."""
    if cloud.provisioning is None:
        pytest.skip(f"the {cloud.cloud} leg has no provider")
    return cloud.provisioning["config"]


def condition(obj: dict | None, typ: str) -> dict:
    """The condition of type typ on obj, or an empty one."""
    for c in (obj or {}).get("status", {}).get("conditions", []):
        if c["type"] == typ:
            return c
    return {}


def test_selects_provider(control_plane: kube.Cluster, provisioned: dict) -> None:
    """The InferenceCluster's ProvisionedCluster runs the provider's Composition, which accepts it.

    Synced is False when the provider fails the reconcile, such as for a node
    class it doesn't own or a pool its node class doesn't fit.
    """
    group = provisioned["apiVersion"].split("/")[0]
    selector = f"{contract.LABEL_CONFIG_GROUP}={group},{contract.LABEL_CONFIG_KIND}={provisioned['kind']}"
    compositions = control_plane.custom.list_cluster_custom_object(
        "apiextensions.crossplane.io",
        "v1",
        "compositions",
        label_selector=selector,
        _request_timeout=kube.TIMEOUT_SECONDS,
    )["items"]
    assert len(compositions) == 1, f"want one Composition labelled {selector}, got {len(compositions)}"
    want = compositions[0]["metadata"]["name"]

    def selected() -> None:
        pc = control_plane.modelplane(
            "provisionedclusters", clouds.PROVISIONED, None, group="infrastructure.modelplane.ai"
        )
        assert pc is not None, f"ProvisionedCluster {clouds.PROVISIONED} doesn't exist"
        got = pc["spec"].get("crossplane", {}).get("compositionRef", {}).get("name")
        assert got == want, f"ProvisionedCluster {clouds.PROVISIONED} runs Composition {got}, not {want}"
        synced = condition(pc, "Synced")
        assert synced.get("status") == "True", f"ProvisionedCluster {clouds.PROVISIONED} isn't Synced: {synced}"

    wait.until(selected, timeout=5 * 60, what=f"the provider to accept ProvisionedCluster {clouds.PROVISIONED}")


def test_composes_managed_resources(control_plane: kube.Cluster, provisioned: dict) -> None:
    """The provider activates its managed resource kinds, and composes managed resources of them.

    The activation policy is the one the ProvisionedCluster composes, found by
    its owner. A managed resource of an activated kind exists only because
    the provider composed it: nothing else on the control plane creates any.
    """
    del provisioned
    extensions = client.ApiextensionsV1Api(control_plane.custom.api_client)

    def composed() -> None:
        policies = control_plane.custom.list_cluster_custom_object(
            "apiextensions.crossplane.io",
            "v1alpha1",
            "managedresourceactivationpolicies",
            label_selector=f"crossplane.io/composite={clouds.PROVISIONED}",
            _request_timeout=kube.TIMEOUT_SECONDS,
        )["items"]
        assert policies, f"ProvisionedCluster {clouds.PROVISIONED} composed no activation policy"
        activated = [kind for p in policies for kind in p.get("status", {}).get("activated", [])]
        assert activated, "the provider's activation policy activated nothing"
        found = []
        for name in activated:
            crd = extensions.read_custom_resource_definition(name, _request_timeout=kube.TIMEOUT_SECONDS)
            version = next(v.name for v in crd.spec.versions if v.storage)
            items = control_plane.custom.list_cluster_custom_object(
                crd.spec.group, version, crd.spec.names.plural, _request_timeout=kube.TIMEOUT_SECONDS
            )["items"]
            found += [f"{crd.spec.names.kind} {i['metadata']['name']}" for i in items]
        assert found, f"no managed resource of an activated kind exists: {sorted(activated)}"

    wait.until(composed, timeout=10 * 60, what="the provider to compose managed resources", retry=kube.RETRY)


def test_inference_cluster_provisioning(control_plane: kube.Cluster, provisioned: dict) -> None:
    """The InferenceCluster reports its cluster as provisioning, not as a failure.

    Every failure compose-inference-cluster reports - no provider, a pool the
    provider can't provision, a driver it doesn't support, a provider error -
    sets ClusterReady False with a reason of its own.
    """
    del provisioned

    def provisioning() -> None:
        ic = control_plane.modelplane("inferenceclusters", clouds.PROVISIONED, None)
        ready = condition(ic, "ClusterReady")
        assert ready.get("reason") == "Provisioning", f"InferenceCluster {clouds.PROVISIONED}: {ready}"

    wait.until(provisioning, timeout=5 * 60, what=f"InferenceCluster {clouds.PROVISIONED} to report provisioning")
