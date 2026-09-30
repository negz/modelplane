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

"""Send requests to an InferenceGateway, and read what it logs about them.

A gateway's address is on the kind Docker network, which a macOS host can't
route to. So requests go from a pod on the control plane, which can.
"""

import dataclasses
import datetime
import json
from typing import Any

from e2e import kube

# The key manifests/platform/10-inference-gateway.yaml gives caller e2e.
CALLER_KEY = "sk-e2e-caller"

# The InferenceGateway's Envoy proxy pods, on the cluster it runs on.
PROXY_NAMESPACE = "envoy-gateway-system"
PROXY_SELECTOR = "gateway.envoyproxy.io/owning-gateway-name=inference-gateway"


@dataclasses.dataclass(frozen=True)
class Response:
    """What came back from a request."""

    # The HTTP status, or 0 if there was no HTTP response at all.
    status: int
    # The response body, or why there was no response.
    body: str

    def json(self) -> dict[str, Any]:
        """Decode the body, a JSON object."""
        return json.loads(self.body)


@dataclasses.dataclass(frozen=True)
class Client:
    """Sends requests with curl, from a pod on a cluster that can reach the gateway."""

    cluster: kube.Cluster
    namespace: str
    pod: str

    def request(self, url: str, headers: dict[str, str], body: object | None = None) -> Response:
        """Send a request: a POST if there's a body, which goes as JSON, otherwise a GET."""
        curl = ["curl", "--silent", "--show-error", "--max-time=15", "--write-out=\n%{http_code}", url]
        for name, value in headers.items():
            curl.append(f"--header={name}: {value}")
        if body is not None:
            curl += ["--header=content-type: application/json", f"--data={json.dumps(body)}"]
        try:
            out = self.cluster.kubectl("exec", f"--namespace={self.namespace}", self.pod, "--", *curl)
        except kube.KubectlError as e:
            # curl exits non-zero only when no HTTP response came back, for
            # example when it couldn't connect.
            return Response(status=0, body=str(e))
        # --write-out puts the status on a line of its own, after the body.
        body_text, _, status = out.rpartition("\n")
        return Response(status=int(status), body=body_text)


def chat_completion(model: str) -> dict[str, Any]:
    """Return an OpenAI chat completion request for a model."""
    return {"model": model, "messages": [{"role": "user", "content": "ping"}]}


def message(model: str) -> dict[str, Any]:
    """Return an Anthropic Messages API request for a model."""
    return {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]}


def usage_records(cluster: kube.Cluster, since: datetime.datetime) -> list[dict[str, Any]]:
    """Return the usage records every InferenceGateway proxy pod has logged since a time.

    Each request lands on one of the proxy pods, so this reads them all. It skips
    pods that aren't running yet, which have no logs to read.
    """
    records = []
    for pod in cluster.list_objects("pod", PROXY_NAMESPACE, PROXY_SELECTOR):
        if pod.get("status", {}).get("phase") != "Running":
            continue
        logs = cluster.kubectl(
            "logs",
            f"--namespace={PROXY_NAMESPACE}",
            pod["metadata"]["name"],
            "--container=envoy",
            f"--since-time={since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        )
        for line in logs.splitlines():
            # Envoy logs other things too. The access log is the JSON lines.
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and "caller" in record:
                records.append(record)
    return records
