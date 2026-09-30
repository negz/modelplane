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

"""C. Placement: pods land where they should on the workload cluster.

These checks only read, so there's nothing to tear down.
"""

import pytest

from e2e import kube

pytestmark = [pytest.mark.placement, pytest.mark.diagnose("ml-team")]

# Pods allowed to tolerate every taint, outside kube-system. Each entry is the
# namespace, kind and name of the pods' controller. Say why beside each one.
WILDCARD_TOLERATION_ALLOWLIST = {
    # A node exporter has to run on every node, whatever its taints.
    ("monitoring", "DaemonSet", "mp-kube-prometheus-stack-prometheus-node-exporter"),
}


def test_c1_engine_pods_run_on_gpu_pool(control_plane: kube.Cluster, workload: kube.Cluster) -> None:
    """C1: Every ml-team/mock-demo engine pod runs in pool gpu-synthetic, on a gpu.example.com device."""
    replicas = control_plane.list_objects("modelreplica", "ml-team", "modelplane.ai/deployment=mock-demo")
    assert replicas, "ModelDeployment ml-team/mock-demo has no ModelReplicas"
    selector = f"modelplane.ai/serving in ({','.join(r['metadata']['name'] for r in replicas)})"
    pods = workload.list_objects("pod", selector=selector)
    assert pods, f"no engine pods match {selector}"

    problems = []
    for pod in pods:
        name = kube.qualified(pod["metadata"]["namespace"], pod["metadata"]["name"])
        node = pod["spec"].get("nodeName")
        if node is None:
            problems.append(f"{name} isn't scheduled")
            continue
        labels = (workload.get("node", node) or {"metadata": {}})["metadata"].get("labels", {})
        if labels.get("modelplane.ai/pool") != "gpu-synthetic":
            problems.append(f"{name} runs on node {node}, in pool {labels.get('modelplane.ai/pool')!r}")

        claims = pod.get("status", {}).get("resourceClaimStatuses", [])
        if not claims:
            problems.append(f"{name} has no ResourceClaim")
        for status in claims:
            # The claim a pod's template asks for gets a generated name, which
            # the pod's status records once the claim exists.
            claim_name = status.get("resourceClaimName")
            claim = workload.get("resourceclaim", claim_name, pod["metadata"]["namespace"]) if claim_name else None
            if claim is None:
                problems.append(f"{name}'s claim {status['name']} doesn't exist")
                continue
            results = claim.get("status", {}).get("allocation", {}).get("devices", {}).get("results", [])
            drivers = sorted({r["driver"] for r in results})
            if "gpu.example.com" not in drivers:
                problems.append(f"{name}'s claim {status['name']} has devices from {drivers}, not gpu.example.com")

    assert problems == []


def test_c2_no_pod_tolerates_every_taint(workload: kube.Cluster) -> None:
    """C2: No pod outside kube-system tolerates every taint, unless its controller is allowlisted."""
    offenders = []
    for pod in workload.list_objects("pod"):
        namespace = pod["metadata"]["namespace"]
        if namespace == "kube-system":
            continue
        # A toleration with operator Exists and no key matches every taint.
        wildcards = [
            t for t in pod["spec"].get("tolerations", []) if t.get("operator") == "Exists" and not t.get("key")
        ]
        if not wildcards:
            continue
        controllers = {
            (namespace, o["kind"], o["name"]) for o in pod["metadata"].get("ownerReferences", []) if o.get("controller")
        }
        if controllers & WILDCARD_TOLERATION_ALLOWLIST:
            continue
        offenders.append(f"{kube.qualified(namespace, pod['metadata']['name'])} tolerates {wildcards}")

    assert offenders == []
