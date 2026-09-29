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

"""Scenario A: InferenceGateway local serves ModelService ml-team/mock.

These only read ml-team. The curl pod the requests come from runs in a
namespace of its own.
"""

import json
import time
import unittest

import gateway
import kube

NAMESPACE = "bakeoff-unittest-serving"

# The name the ModelDeployment behind ml-team/mock starts its engine under.
SERVED_MODEL = "ml-team/mock-demo"

PROXIES = "gateway.envoyproxy.io/owning-gateway-name=inference-gateway"
AUTHORIZED = {"authorization": f"Bearer {gateway.CALLER_KEY}"}


def access_log(seconds: int) -> list[dict]:
    """The records the InferenceGateway's Envoy proxies logged in the last seconds."""
    records = []
    for pod in kube.items(kube.WORKLOAD, "pods", "-n", "envoy-gateway-system", "-l", PROXIES):
        log = kube.kubectl(
            kube.WORKLOAD,
            "-n",
            "envoy-gateway-system",
            "logs",
            pod["metadata"]["name"],
            "-c",
            "envoy",
            f"--since={seconds}s",
        )
        # Envoy's own log lines share the container's output with the JSON access log.
        records += [json.loads(line) for line in log.splitlines() if line.startswith("{")]
    return records


class Serving(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.addClassCleanup(
            kube.kubectl, kube.CONTROL_PLANE, "delete", "namespace", NAMESPACE, "--ignore-not-found", "--timeout=2m"
        )
        kube.apply(kube.CONTROL_PLANE, {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
        cls.client = gateway.start_client(NAMESPACE)

        # A caller names a ModelService by its status.model.
        cls.model = kube.get(kube.CONTROL_PLANE, "modelservice", "mock", "-n", "ml-team")["status"]["model"]
        endpoints = kube.get(kube.CONTROL_PLANE, "inferencegateway", "local")["status"]["endpoints"]
        cls.openai = endpoints["openAI"] + "/chat/completions"
        cls.anthropic = endpoints["anthropic"] + "/messages"

    def explain(self, got: gateway.Response) -> str:
        """A failure message: the response, and the state of what should have served it."""
        state = kube.describe(
            kube.get(kube.CONTROL_PLANE, "modelservice", "mock", "-n", "ml-team"),
            kube.get(kube.CONTROL_PLANE, "inferencegateway", "local"),
        )
        return f"HTTP {got.status}: {got.body}\n{state}"

    def test_a1_chat_completion(self) -> None:
        """A1: An OpenAI chat completion with the caller's key returns 200."""
        got = self.client.post(self.openai, gateway.chat(self.model), AUTHORIZED)
        self.assertEqual(got.status, 200, self.explain(got))

    def test_a2_unauthenticated_chat_completion(self) -> None:
        """A2: The same request returns 401 with no key, and with a key no caller holds."""
        cases = {
            "no key": {},
            "wrong key": {"authorization": "Bearer sk-wrong"},
        }
        for name, headers in cases.items():
            with self.subTest(name):
                got = self.client.post(self.openai, gateway.chat(self.model), headers)
                self.assertEqual(got.status, 401, got.body)

    def test_a3_response_names_served_model(self) -> None:
        """A3: The response names the model the engine serves, not the ModelService the caller asked for."""
        got = self.client.post(self.openai, gateway.chat(self.model), AUTHORIZED)
        self.assertEqual(got.status, 200, self.explain(got))
        self.assertEqual(got.json()["model"], SERVED_MODEL, got.body)

    def test_a4_anthropic_messages(self) -> None:
        """A4: An Anthropic Messages request with the key in x-api-key returns 200."""
        headers = {"x-api-key": gateway.CALLER_KEY, "anthropic-version": "2023-06-01"}
        got = self.client.post(self.anthropic, gateway.messages(self.model), headers)
        self.assertEqual(got.status, 200, self.explain(got))

    def test_a5_usage_record(self) -> None:
        """A5: The gateway's access log meters a chat completion to caller e2e."""
        sent = time.monotonic()
        self.client.post(self.openai, gateway.chat(self.model), AUTHORIZED)

        want = {
            "caller": "e2e",
            "service": "ml-team/mock",
            "served_model": SERVED_MODEL,
            "input_tokens": 12,
            "output_tokens": 9,
            "total_tokens": 21,
            "status": 200,
        }

        # Other callers share the proxies and caller e2e, and a record names
        # nothing unique to one request. So the best this can show is that a
        # matching record was logged since the request was sent. The kubelet
        # applies the window, so it's a duration rather than a time, which
        # keeps the host's clock out of it. Envoy buffers its access log, so
        # poll.
        def fetch() -> list[dict]:
            since = int(time.monotonic() - sent) + 5
            return [{k: r.get(k) for k in want} for r in access_log(since)]

        got = kube.poll(fetch, until=lambda records: want in records, timeout=30, interval=2)
        if want not in got:
            seen = "\n".join(json.dumps(r) for r in got) or "none"
            self.fail(f"no access log record since the request matches\n{json.dumps(want)}\nRecords seen:\n{seen}")
