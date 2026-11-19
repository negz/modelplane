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

"""The device stacks the serving stack installs on an Existing cluster.

Modelplane doesn't provision an Existing cluster, so no cluster provider
installs device stacks on it, and the serving stack does instead. The
pins are the SDK's vendor components, shared with every provider that
doesn't generate its own, each tagged with its driver so it installs
only when a class claims it: NVIDIA's, and AMD's, for BYO nodes whose
images already carry the amdgpu kernel driver.

Existing is open-ended for devices: the user owns the cluster and may
have installed any driver, so a claimed driver these components don't
carry installs nothing and is never rejected, and a typo surfaces when
its ResourceClaim can't allocate.
"""

import dataclasses

from modelplane_sdk.stacks import Component, Manifests, amd, nvidia

COMPONENTS: list[Component] = [
    # NFD labels GPU nodes for both GPU stacks - NVIDIA's kubelet plugin
    # and AMD's operator both find their nodes by NFD labels - so it
    # carries both drivers, and tolerates both taints a BYO GPU node may
    # carry.
    dataclasses.replace(
        nvidia.NODE_FEATURE_DISCOVERY,
        dra_drivers=["gpu.nvidia.com", "gpu.amd.com"],
        values={
            "worker": {
                "tolerations": [
                    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                    {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                ],
            },
        },
    ),
    nvidia.DRA_DRIVER,
    # The DRA driver's kubelet plugin runs at system-node-critical
    # priority, which some clusters only admit in a namespace whose
    # ResourceQuota permits those priority classes; without it the
    # daemonset gets FailedCreate and never publishes ResourceSlices. The
    # quota only grants headroom, so it's harmless on clusters that don't
    # restrict them. Lands in the namespace the DRA driver's release
    # creates; provider-kubernetes retries until it exists.
    Manifests(
        key="dra-driver-critical-pods-quota",
        dra_drivers=["gpu.nvidia.com"],
        manifests=[
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {"name": "allow-critical-pods", "namespace": nvidia.DRA_DRIVER.namespace},
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
    amd.METRICS_EXPORTER_CONFIG,
    amd.GPU_OPERATOR,
]
