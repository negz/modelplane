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

"""NVIDIA's device stack components, pinned once for every stack.

A device stack that AICR doesn't generate - a hand-written provider's,
or the serving stack's for an Existing cluster - takes these rather than
pinning NVIDIA's charts itself, adapting one with dataclasses.replace
where its cloud differs, such as a driver root the node image doesn't
use. How a stack configures the GPU Operator depends on what its
cloud's node image ships, so only the operator's version is pinned
here.

modelplane_sdk.check runs check_pins, which holds every provider's
device stack, AICR-generated or not, to these pins, so an aicr bump
moves them here in the same review.
"""

from modelplane_sdk.stacks.components import Chart, Component
from modelplane_sdk.stacks.dcgm import DCGM_METRICS_CSV

__all__ = [
    "DCGM_EXPORTER",
    "DCGM_METRICS_CSV",
    "DRA_DRIVER",
    "GPU_OPERATOR_VERSION",
    "NODE_FEATURE_DISCOVERY",
    "check_pins",
]

# The taint every provider puts on NVIDIA GPU nodes, and which
# compose-model-replica's engine pods tolerate. Exists matches every
# value a provider picks.
_GPU_TOLERATION = {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}

# Labels NVIDIA GPU nodes for the DRA driver, which schedules its
# kubelet plugin only onto nodes carrying an NFD GPU label
# (feature.node.kubernetes.io/pci-10de and friends). It carries the GPU
# stack's driver, so it stays off clusters whose classes claim no GPUs.
#
# The worker must run on the very nodes it labels. GPU nodes carry the
# nvidia.com/gpu taint, and the chart's worker tolerates nothing by
# default. Without this toleration the chain breaks silently: no worker
# on the GPU node, no pci-10de label, no kubelet plugin, no
# ResourceSlices, and every GPU ResourceClaim stays unallocatable with
# all components looking healthy.
#
# wait, because the components that need its labels depend on it.
NODE_FEATURE_DISCOVERY = Chart(
    key="node-feature-discovery",
    release="mp-node-feature-discovery",
    namespace="node-feature-discovery",
    chart="node-feature-discovery",
    repository="https://kubernetes-sigs.github.io/node-feature-discovery/charts",
    version="0.19.0",
    wait=True,
    dra_drivers=["gpu.nvidia.com"],
    values={"worker": {"tolerations": [_GPU_TOLERATION]}},
)

# Publishes each GPU node's devices as DRA ResourceSlices and registers
# the gpu.nvidia.com DeviceClass ModelReplica ResourceClaims request
# through. GPU allocation is opt-in; ComputeDomains (multi-node NVLink)
# is unused and would pull in extra prerequisites. nvidiaDriverRoot stays
# at the chart default (/), where a node image that ships the driver
# puts it.
DRA_DRIVER = Chart(
    key="nvidia-dra-driver-gpu",
    release="mp-dra-driver-nvidia-gpu",
    namespace="nvidia-dra-driver",
    chart="dra-driver-nvidia-gpu",
    repository="oci://registry.k8s.io/dra-driver-nvidia/charts",
    version="0.5.0",
    dra_drivers=["gpu.nvidia.com"],
    values={
        "gpuResourcesEnabledOverride": True,
        "resources": {"computeDomains": {"enabled": False}},
    },
)

# The version of NVIDIA's GPU Operator, chart gpu-operator from
# https://helm.ngc.nvidia.com/nvidia, that every stack installing it pins.
GPU_OPERATOR_VERSION = "v26.7.1"

# Exports DCGM fields for the serving stack's collector, for a cluster
# whose GPU Operator, if any, runs no exporter of its own. The collector
# finds the pod itself, so no ServiceMonitor. DCGM runs embedded (the
# chart default). The counters are AICR's list, not the chart's shorter
# default, so every cloud exports the same DCGM fields.
DCGM_EXPORTER = Chart(
    key="dcgm-exporter",
    release="mp-dcgm-exporter",
    namespace="dcgm-exporter",
    chart="dcgm-exporter",
    repository="https://nvidia.github.io/dcgm-exporter/helm-charts",
    version="4.8.4",
    dra_drivers=["gpu.nvidia.com"],
    values={
        "serviceMonitor": {"enabled": False},
        "customMetrics": DCGM_METRICS_CSV,
        # Attribute each GPU's series to the pod holding it. GPUs are
        # allocated through DRA, which the exporter only maps back to pods
        # with this set; the chart adds the RBAC it needs.
        "kubernetesDRA": {"enabled": True},
        # The pod labels the collector joins a GPU series to its
        # deployment's engine series on.
        "kubernetes": {
            "enablePodLabels": True,
            "podLabelAllowlistRegex": [r"^modelplane\.ai/(deployment|replica|role|engine)$"],
        },
        "tolerations": [_GPU_TOLERATION],
        # The first three nodeSelectorTerms of dra-driver-nvidia-gpu
        # v0.5.0's kubeletPlugin affinity, so the exporter runs exactly
        # where the DRA driver publishes GPUs. Some clouds' GPU nodes carry
        # pci-0302_10de but not pci-10de. Recheck them when that chart
        # moves.
        "affinity": {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": key, "operator": "In", "values": ["true"]}]}
                        for key in (
                            "feature.node.kubernetes.io/pci-10de.present",
                            "feature.node.kubernetes.io/pci-0302_10de.present",
                            "feature.node.kubernetes.io/pci-0300_10de.present",
                        )
                    ],
                },
            },
        },
    },
)


def check_pins(name: str, components: list[Component]) -> None:
    """Raise ValueError if components pin one of NVIDIA's charts at another version.

    A generated stack takes AICR's pins rather than these, and the two
    must agree, so one Modelplane release runs one version of each of
    NVIDIA's components whichever provider provisioned the cluster.
    """
    pins = {c.chart: c.version for c in (NODE_FEATURE_DISCOVERY, DRA_DRIVER, DCGM_EXPORTER)}
    pins["gpu-operator"] = GPU_OPERATOR_VERSION
    for c in components:
        if isinstance(c, Chart) and c.chart in pins and c.version != pins[c.chart]:
            raise ValueError(f"{name}: {c.key} pins {c.chart} {c.version}, but the SDK pins {pins[c.chart]}")
