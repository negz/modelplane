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

"""B. Lifecycle: a model serves once it's created, and deleting it removes what it composed.

Setup applies manifests/lifecycle/: namespace bakeoff-pytest, holding a copy of
ml-team's mock ModelDeployment and a ModelService selecting it. The checks run
in file order, and each builds on the one before: B1 serves a request, B2
deletes the ModelService, and B3 deletes the ModelDeployment. Teardown deletes
whatever is left, whether or not they passed.
"""

import logging
import pathlib
from collections.abc import Iterator

import pytest
from models.ai.modelplane.inferencegateway import v1alpha1 as inferencegatewayv1alpha1
from models.ai.modelplane.modelservice import v1alpha1 as modelservicev1alpha1

from e2e import gateway, kube, wait

log = logging.getLogger(__name__)

pytestmark = [pytest.mark.lifecycle, pytest.mark.diagnose("bakeoff-pytest"), pytest.mark.usefixtures("model")]

MANIFESTS = pathlib.Path(__file__).parent / "manifests" / "lifecycle"
NAMESPACE = "bakeoff-pytest"
NAME = "mock-pytest"

# Modelplane labels a ModelDeployment's ModelReplicas and ModelEndpoints with
# its name, and an engine pod with the name of the ModelReplica it serves.
DEPLOYMENT_SELECTOR = f"modelplane.ai/deployment={NAME}"

BEARER = {"authorization": f"Bearer {gateway.CALLER_KEY}"}


@pytest.fixture(scope="module")
def model(
    control_plane: kube.Cluster,
    workload: kube.Cluster,  # noqa: ARG001 - the model runs there, so bring it up first.
) -> Iterator[None]:
    """Setup: apply the model's manifests. Teardown: delete them, and wait for them to go."""
    try:
        control_plane.apply(MANIFESTS)
        yield
    finally:
        control_plane.delete("modelservice", NAME, NAMESPACE)
        control_plane.delete("modeldeployment", NAME, NAMESPACE, cascade="foreground")
        control_plane.wait_until_gone("modeldeployment", NAME, NAMESPACE, timeout=5 * 60)
        control_plane.delete("namespace", NAMESPACE)
        control_plane.wait_until_gone("namespace", NAMESPACE, None, timeout=5 * 60)


def test_b1_model_service_serves(
    control_plane: kube.Cluster, client: gateway.Client, endpoints: inferencegatewayv1alpha1.Endpoints
) -> None:
    """B1: The ModelService becomes RoutingReady, then serves requests as bakeoff-pytest/mock-pytest."""
    obj = control_plane.wait_for_condition("modelservice", NAME, NAMESPACE, "RoutingReady", timeout=5 * 60)
    ms = modelservicev1alpha1.ModelService.model_validate(obj)
    assert ms.status is not None
    assert ms.status.model is not None, f"ModelService {NAMESPACE}/{NAME} is RoutingReady but has no model name"
    model = ms.status.model

    # The route can be accepted a moment before the gateway serves it, and AI
    # Gateway then rolls the gateway's proxy pods to pick it up.
    def serves() -> None:
        r = client.request(f"{endpoints.openAI}/chat/completions", BEARER, gateway.chat_completion(model))
        assert r.status == 200, r
        assert r.json()["model"] == f"{NAMESPACE}/{NAME}", r

    wait.until(serves, timeout=2 * 60, what=f"{model} to serve a chat completion")


def test_b2_deleting_model_service_stops_routing(
    control_plane: kube.Cluster, client: gateway.Client, endpoints: inferencegatewayv1alpha1.Endpoints
) -> None:
    """B2: Once the ModelService is deleted, requests naming it stop returning 200 within 5 minutes.

    The gateway has to answer them, with some other status. A request that gets
    no answer at all shows the gateway is down, not that the route is gone.
    """
    obj = control_plane.get("modelservice", NAME, NAMESPACE)
    if obj is None or not kube.is_true(obj, "RoutingReady"):
        pytest.skip("the ModelService never routed (see B1), so there's no routing to see stop")
    ms = modelservicev1alpha1.ModelService.model_validate(obj)
    assert ms.status is not None
    assert ms.status.model is not None
    model = ms.status.model

    control_plane.delete("modelservice", NAME, NAMESPACE)

    def stopped() -> gateway.Response:
        r = client.request(f"{endpoints.openAI}/chat/completions", BEARER, gateway.chat_completion(model))
        assert r.status not in (0, 200), r
        return r

    r = wait.until(stopped, timeout=5 * 60, what=f"{model} to stop serving")
    log.info("The gateway answered %d: %s", r.status, r.body)


def test_b3_foreground_delete_removes_replicas(control_plane: kube.Cluster, workload: kube.Cluster) -> None:
    """B3: Deleting the ModelDeployment in the foreground removes its replicas, endpoints and engine pods."""
    replicas = names(control_plane.list_objects("modelreplica", NAMESPACE, DEPLOYMENT_SELECTOR))
    if not replicas:
        pytest.skip("the ModelDeployment has no ModelReplicas, so there's nothing to see removed")
    engines = f"modelplane.ai/serving in ({','.join(replicas)})"

    control_plane.delete("modeldeployment", NAME, NAMESPACE, cascade="foreground")

    def removed() -> None:
        remaining = {
            "ModelReplicas": names(control_plane.list_objects("modelreplica", NAMESPACE, DEPLOYMENT_SELECTOR)),
            "ModelEndpoints": names(control_plane.list_objects("modelendpoint", NAMESPACE, DEPLOYMENT_SELECTOR)),
            "engine pods": names(workload.list_objects("pod", selector=engines)),
        }
        assert remaining == {"ModelReplicas": [], "ModelEndpoints": [], "engine pods": []}

    wait.until(removed, timeout=3 * 60, what=f"ModelDeployment {NAMESPACE}/{NAME}'s replicas to be removed")


def names(objs: list[kube.Object]) -> list[str]:
    """Return the names of some objects."""
    return [o["metadata"]["name"] for o in objs]
