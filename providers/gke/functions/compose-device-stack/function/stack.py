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

"""The device stacks the provider installs on a GKE cluster.

The components are generated from NVIDIA AICR recipes, in
generated/aicr/gke.py; see generate.py. The GPU Operator and NVSentinel
depend on the serving stack's cert-manager, which this list carries as
stacks.CERT_MANAGER. GKE runs the NVIDIA driver (Google's standalone
installer) and a managed DCGM exporter itself, so the generated stack
installs neither.

One component is hand-written: the ResourceQuota GKE needs before it
admits the NVIDIA DRA driver's kubelet plugin.
"""

from modelplane_sdk import stacks

from function.generated.aicr import gke

EXTERNAL_DRIVERS: list[str] = gke.EXTERNAL_DRIVERS

COMPONENTS: list[stacks.Component] = [
    stacks.CERT_MANAGER,
    *gke.COMPONENTS,
    # GKE admits system-node-critical and system-cluster-critical pods only
    # in a namespace whose ResourceQuota permits those priority classes, and
    # the DRA driver's kubelet plugin runs at system-node-critical. Without
    # this its DaemonSet gets FailedCreate and never publishes
    # ResourceSlices. Lands in the namespace the DRA driver's release
    # creates; provider-kubernetes retries until it exists.
    stacks.Manifests(
        key="dra-driver-critical-pods-quota",
        dra_drivers=["gpu.nvidia.com"],
        manifests=[
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {"name": "allow-critical-pods", "namespace": "nvidia-dra-driver"},
                "spec": {
                    "hard": {"pods": "1000"},
                    "scopeSelector": {
                        "matchExpressions": [
                            {
                                "operator": "In",
                                "scopeName": "PriorityClass",
                                "values": ["system-node-critical", "system-cluster-critical"],
                            },
                        ],
                    },
                },
            },
        ],
    ),
]
