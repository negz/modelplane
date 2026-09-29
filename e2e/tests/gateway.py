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

"""Send requests to an InferenceGateway from a pod.

Gateway addresses are on the kind Docker network, which a macOS host can't
route to. So requests run curl in a pod on the control plane, through kubectl
exec, which needs only the Kubernetes API.
"""

import dataclasses
import json
import subprocess

import kube

# The same pin run.sh uses.
CURL_IMAGE = "curlimages/curl@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13"

# The key e2e/manifests/10-inference-gateway.yaml gives caller e2e.
CALLER_KEY = "sk-e2e-caller"

POD = "curl"


def chat(model: str) -> dict:
    """The OpenAI chat completion run.sh sends. The mock engine counts it as 12 input and 9 output tokens."""
    return {"model": model, "messages": [{"role": "user", "content": "ping"}]}


def messages(model: str) -> dict:
    """The Anthropic Messages request run.sh sends."""
    return {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]}


@dataclasses.dataclass
class Response:
    """An HTTP response. A status of 0 means curl got none, and the body holds curl's error."""

    status: int
    body: str

    def json(self) -> dict:
        """The body, parsed as JSON."""
        return json.loads(self.body)


@dataclasses.dataclass
class Client:
    """curl in a pod on the control plane."""

    namespace: str

    def post(self, url: str, body: dict, headers: dict[str, str]) -> Response:
        """POST a JSON body to url."""
        curl = ["curl", "-sS", "--max-time", "15", "-w", "\n%{http_code}", "-H", "content-type: application/json"]
        for name, value in headers.items():
            curl += ["-H", f"{name}: {value}"]
        curl += ["--data-raw", json.dumps(body), url]
        result = subprocess.run(
            ["kubectl", "--context", kube.CONTROL_PLANE, "-n", self.namespace, "exec", POD, "--", *curl],
            capture_output=True,
            text=True,
            check=False,
        )
        # -w appends the status on its own line, and writes 000 when there was no response.
        got, _, status = result.stdout.rpartition("\n")
        if not status.isdigit():
            raise RuntimeError(f"kubectl exec {POD} in {self.namespace}: {result.stderr.strip()}")
        return Response(status=int(status), body=got or result.stderr.strip())


def start_client(namespace: str) -> Client:
    """Start a curl pod in an existing namespace. Deleting the namespace deletes the pod."""
    kube.apply(
        kube.CONTROL_PLANE,
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": POD, "namespace": namespace},
            "spec": {
                # sleep ignores SIGTERM, so don't hold up namespace deletion waiting for it.
                "terminationGracePeriodSeconds": 0,
                "containers": [{"name": "curl", "image": CURL_IMAGE, "command": ["sleep", "infinity"]}],
            },
        },
    )
    kube.kubectl(kube.CONTROL_PLANE, "-n", namespace, "wait", "--for=condition=Ready", f"pod/{POD}", "--timeout=2m")
    return Client(namespace=namespace)
