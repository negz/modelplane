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

"""Read and change the e2e clusters through kubectl.

The suite imports only the stdlib, so kubectl is the Kubernetes client and
objects are the plain dicts `kubectl get -o json` returns.
"""

import json
import subprocess
import time
from collections.abc import Callable

# The kube contexts kind creates for the clusters e2e/run.sh brings up.
CONTROL_PLANE = "kind-modelplane-e2e-local"
WORKLOAD = "kind-modelplane-e2e-workload"

type Object = dict


def kubectl(context: str, *args: str, stdin: str | None = None) -> str:
    """Run kubectl against a context and return its stdout, raising with its stderr if it fails."""
    result = subprocess.run(
        ["kubectl", "--context", context, *args], input=stdin, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl --context {context} {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def get(context: str, *args: str) -> Object:
    """Get one object, e.g. get(CONTROL_PLANE, "modelservice", "mock", "-n", "ml-team")."""
    return json.loads(kubectl(context, "get", "-o", "json", *args))


def items(context: str, *args: str) -> list[Object]:
    """List objects, e.g. items(WORKLOAD, "pods", "-A", "-l", "app=x")."""
    return get(context, *args)["items"]


def apply(context: str, obj: Object) -> None:
    """Apply one object."""
    kubectl(context, "apply", "-f", "-", stdin=json.dumps(obj))


def poll[T](fetch: Callable[[], T], until: Callable[[T], bool], timeout: float, interval: float = 5) -> T:
    """Fetch until the result satisfies until, or timeout seconds pass.

    Returns the last result either way, so the caller can assert on it and
    report what it saw rather than only that it timed out.
    """
    deadline = time.monotonic() + timeout
    while True:
        got = fetch()
        if until(got) or time.monotonic() >= deadline:
            return got
        time.sleep(interval)


def condition(obj: Object, condition_type: str) -> str:
    """The status of a condition, e.g. "True", or "Unknown" when it isn't set."""
    for c in obj.get("status", {}).get("conditions", []):
        if c["type"] == condition_type:
            return c["status"]
    return "Unknown"


def describe(*objs: Object) -> str:
    """One line per object, giving its conditions. Failure messages use it to show state."""
    lines = []
    for obj in objs:
        conditions = [
            " ".join([f"{c['type']}={c['status']}", f"({c.get('reason')})", c.get("message", "")]).strip()
            for c in obj.get("status", {}).get("conditions", [])
        ]
        lines.append(f"{obj['kind']} {obj['metadata']['name']}: {'; '.join(conditions) or 'no conditions'}")
    return "\n".join(lines)


def replicas(namespace: str, deployment: str) -> list[str]:
    """The names of a ModelDeployment's ModelReplicas."""
    selector = f"modelplane.ai/deployment={deployment}"
    return [r["metadata"]["name"] for r in items(CONTROL_PLANE, "modelreplicas", "-n", namespace, "-l", selector)]


def engine_pods(replicas: list[str]) -> list[Object]:
    """The engine pods on the workload cluster serving some ModelReplicas.

    Each engine pod is labelled with the ModelReplica it serves. It runs in the
    namespace mirrored from the ModelReplica's, whose name carries a hash, so
    search every namespace.
    """
    return items(WORKLOAD, "pods", "-A", "-l", f"modelplane.ai/serving in ({','.join(replicas)})")
