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

"""A. Serving: the InferenceGateway authenticates, routes, translates and meters requests.

These checks send requests for ml-team's mock ModelService, which the platform
manifests create. They only read, so there's nothing to tear down.
"""

import datetime

import pytest
from models.ai.modelplane.inferencegateway import v1alpha1 as inferencegatewayv1alpha1
from models.ai.modelplane.modelservice import v1alpha1 as modelservicev1alpha1

from e2e import gateway, kube, wait

pytestmark = [pytest.mark.serving, pytest.mark.diagnose("ml-team")]

# The name the engine serves ml-team/mock-demo's model under, which is the
# ModelDeployment's namespace and name. It rejects requests naming anything
# else, so seeing it proves the gateway rewrote the caller's model name.
SERVED_MODEL = "ml-team/mock-demo"

# How long a request that should succeed gets to do so. AI Gateway rolls the
# gateway's proxy pods whenever a route changes, which other tests' models do,
# and a request can fail while it does.
SERVING_TIMEOUT = 2 * 60

BEARER = {"authorization": f"Bearer {gateway.CALLER_KEY}"}


@pytest.fixture(scope="module")
def model(
    control_plane: kube.Cluster,
    workload: kube.Cluster,  # noqa: ARG001 - the platform creates the ModelService, so bring it up first.
) -> str:
    """The model name callers use for ml-team's mock ModelService.

    A ModelService publishes this before it can route, so it doesn't mean the
    model serves. The checks wait for that themselves, so a routing fault fails
    them but not A2, whose requests are refused before they're routed.
    """

    def published() -> str:
        obj = control_plane.get("modelservice", "mock", "ml-team")
        assert obj is not None, "ModelService ml-team/mock doesn't exist"
        ms = modelservicev1alpha1.ModelService.model_validate(obj)
        assert ms.status is not None, "ModelService ml-team/mock has no status"
        assert ms.status.model is not None, "ModelService ml-team/mock hasn't published its model name"
        return ms.status.model

    return wait.until(published, timeout=2 * 60, what="ModelService ml-team/mock to publish its model name")


def test_a1_chat_completion_returns_200(
    client: gateway.Client, endpoints: inferencegatewayv1alpha1.Endpoints, model: str
) -> None:
    """A1: An OpenAI chat completion naming the ModelService, with the caller's key, returns 200."""

    def returns_200() -> None:
        r = client.request(f"{endpoints.openAI}/chat/completions", BEARER, gateway.chat_completion(model))
        assert r.status == 200, r

    wait.until(returns_200, timeout=SERVING_TIMEOUT, what=f"a chat completion for {model} to return 200")


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no key"),
        pytest.param({"authorization": "Bearer sk-wrong"}, id="wrong key"),
    ],
)
def test_a2_request_without_valid_key_returns_401(
    client: gateway.Client, endpoints: inferencegatewayv1alpha1.Endpoints, model: str, headers: dict[str, str]
) -> None:
    """A2: The same request, with no key or with a key no Secret holds, returns 401."""

    def returns_401() -> None:
        r = client.request(f"{endpoints.openAI}/chat/completions", headers, gateway.chat_completion(model))
        assert r.status == 401, r

    wait.until(returns_401, timeout=SERVING_TIMEOUT, what=f"a chat completion for {model} to return 401")


def test_a3_response_names_served_model(
    client: gateway.Client, endpoints: inferencegatewayv1alpha1.Endpoints, model: str
) -> None:
    """A3: The response names the model the engine served, not the one the caller asked for."""

    def names_served_model() -> None:
        r = client.request(f"{endpoints.openAI}/chat/completions", BEARER, gateway.chat_completion(model))
        assert r.status == 200, r
        assert r.json()["model"] == SERVED_MODEL, r

    wait.until(names_served_model, timeout=SERVING_TIMEOUT, what=f"a chat completion to report {SERVED_MODEL}")


def test_a4_anthropic_message_returns_200(
    client: gateway.Client, endpoints: inferencegatewayv1alpha1.Endpoints, model: str
) -> None:
    """A4: An Anthropic Messages API request, with the key in x-api-key, returns 200."""
    headers = {"x-api-key": gateway.CALLER_KEY, "anthropic-version": "2023-06-01"}

    def returns_200() -> None:
        r = client.request(f"{endpoints.anthropic}/messages", headers, gateway.message(model))
        assert r.status == 200, r

    wait.until(returns_200, timeout=SERVING_TIMEOUT, what=f"an Anthropic message for {model} to return 200")


def test_a5_gateway_logs_usage_record(
    client: gateway.Client, endpoints: inferencegatewayv1alpha1.Endpoints, model: str, workload: kube.Cluster
) -> None:
    """A5: The gateway's access log attributes a request's tokens to its caller, service and served model.

    Other tests send the same request as the same caller, so a matching record
    shows the gateway meters requests, but not that it metered this one.
    """
    want = {
        "caller": "e2e",
        "service": "ml-team/mock",
        "served_model": SERVED_MODEL,
        "input_tokens": 12,
        "output_tokens": 9,
        "total_tokens": 21,
        "status": 200,
    }
    # A little before now, in case the node's clock and ours disagree.
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=10)

    # Envoy buffers its access log for a while before writing it out. If the pod
    # that served a request is replaced before it does, the record is lost, so
    # each attempt sends another request.
    def logged() -> None:
        client.request(f"{endpoints.openAI}/chat/completions", BEARER, gateway.chat_completion(model))
        records = [r for r in gateway.usage_records(workload, since) if r.get("service") == want["service"]]
        got = [{k: r.get(k) for k in want} for r in records]
        assert want in got, f"no matching usage record; the last few for {want['service']} were {records[-3:]}"

    wait.until(logged, timeout=SERVING_TIMEOUT, what="a matching usage record in the gateway's access log")
