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

"""Scenario C: where pods run on the workload cluster. These only read."""

import unittest

import kube

# Pods that may tolerate every taint, as the namespace and kind and name of the
# controller that owns them.
WILDCARD_TOLERATION_ALLOWED = {
    ("monitoring", "DaemonSet", "mp-kube-prometheus-stack-prometheus-node-exporter"),
}


class Placement(unittest.TestCase):
    maxDiff = None

    def test_c1_engine_pods_on_gpu_pool_with_dra_device(self) -> None:
        """C1: Every ml-team/mock-demo engine pod runs in pool gpu-synthetic, with a gpu.example.com device."""
        replicas = kube.replicas("ml-team", "mock-demo")
        self.assertTrue(replicas, "ModelDeployment ml-team/mock-demo has no ModelReplicas")
        pods = kube.engine_pods(replicas)
        self.assertTrue(pods, f"no engine pods serve ModelReplicas {replicas}")

        got = {}
        for pod in pods:
            namespace, name = pod["metadata"]["namespace"], pod["metadata"]["name"]
            node = pod["spec"].get("nodeName")
            pool = (
                kube.get(kube.WORKLOAD, "node", node)["metadata"]["labels"].get("modelplane.ai/pool") if node else None
            )
            drivers = []
            for claim in pod["status"].get("resourceClaimStatuses", []):
                drivers += kube.kubectl(
                    kube.WORKLOAD,
                    "get",
                    "resourceclaim",
                    claim["resourceClaimName"],
                    "-n",
                    namespace,
                    "-o",
                    "jsonpath={.status.allocation.devices.results[*].driver}",
                ).split()
            got[f"{namespace}/{name} on node {node}"] = {"pool": pool, "drivers": drivers}

        want = {pod: {"pool": "gpu-synthetic", "drivers": ["gpu.example.com"]} for pod in got}
        self.assertEqual(got, want)

    def test_c2_no_wildcard_tolerations(self) -> None:
        """C2: No pod outside kube-system tolerates every taint, apart from those allowed."""
        offenders = []
        for pod in kube.items(kube.WORKLOAD, "pods", "-A"):
            namespace, name = pod["metadata"]["namespace"], pod["metadata"]["name"]
            if namespace == "kube-system":
                continue
            wildcards = [
                t for t in pod["spec"].get("tolerations", []) if t.get("operator") == "Exists" and not t.get("key")
            ]
            if not wildcards:
                continue
            owner = next((o for o in pod["metadata"].get("ownerReferences", []) if o.get("controller")), {})
            if (namespace, owner.get("kind"), owner.get("name")) in WILDCARD_TOLERATION_ALLOWED:
                continue
            offenders.append(f"{namespace}/{name} tolerates {wildcards}")

        self.assertEqual(offenders, [])
