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

"""AMD's device stack components, pinned once for every stack.

The AMD GPU operator in DRA mode, for nodes whose images already carry
the amdgpu kernel driver. The serving stack installs it on an Existing
cluster whose classes claim gpu.amd.com; a provider whose clusters run
AMD GPUs takes the same pins. It needs node-feature-discovery, tolerating
the amd.com/gpu taint, and the serving stack's cert-manager.
"""

from modelplane_sdk.stacks.components import Chart, Manifests

__all__ = ["GPU_OPERATOR", "METRICS_EXPORTER_CONFIG"]

# The metrics exporter's config, referenced from the DeviceConfig
# below. It exists to turn ProfilerMetrics on: GPU_PROF_* fields ship
# disabled, and tensor activity (MFMA utilization) is one of them -
# without it modelplane_gpu_tensor_active_ratio is NVIDIA-only. The
# hardware runs one profiler instance at a time, so this takes the
# slot application-level profiling would use on these nodes; turn it
# off here to profile. An absent Fields list keeps the exporter's
# default metrics unchanged. Lands in the namespace the chart's
# release creates; provider-kubernetes retries until it exists, like
# the serving stack's critical-pods quotas.
METRICS_EXPORTER_CONFIG = Manifests(
    key="amd-metrics-exporter-config",
    dra_drivers=["gpu.amd.com"],
    manifests=[
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "mp-metrics-exporter-config",
                "namespace": "kube-amd-gpu",
            },
            "data": {
                "config.json": '{"GPUConfig": {"ProfilerMetrics": {"all": true}}}',
            },
        },
    ],
)

# The AMD GPU operator, in DRA mode: the default DeviceConfig the
# chart renders (crds.defaultCR) is set below to enable the DRA
# driver and disable the device plugin, the reverse of the chart's
# defaults - the two are mutually exclusive allocators - so the
# gpu.amd.com DeviceClass the chart registers is the sole path to the
# GPUs.
#
# The default DeviceConfig tracks the release only with
# crds.defaultCR.upgrade: the chart renders it on install, marks it
# helm.sh/resource-policy: keep, and skips it on upgrade by default,
# so without the flag a value change below would upgrade the release
# and leave the live DeviceConfig at its install-time spec.
#
# The chart pin pins the operator's own images but not the
# DeviceConfig's, whose DRA driver, node labeller, metrics exporter
# and utility images default to floating tags - so each is pinned
# here. The DRA driver is the component the claims go through; on a
# floating tag any pod reschedule could change it under a running
# cluster. The exporter's field names are what the modelplane-gpu-amd
# MetricMapping reads, so it is pinned to the operator's release.
#
# Driver management is off: a BYO node's image must already carry
# the amdgpu kernel driver, and the chart's in-cluster driver build
# needs a push registry configured (driver.image defaults to a
# docker.io placeholder), without which KMM loops on a driver image
# it can never resolve and the DRA driver - gated on the KMM-ready
# node label - never schedules. KMM must stay installed even with
# the driver unmanaged: the operator watches KMM's kinds at startup
# no matter what, and crashes without their CRDs, taking every
# operand down with it.
#
# The node labeller stays on for scheduling labels; NFD is the
# stack's standalone instance, so the chart's bundled one is off and
# two instances never race over the same label namespace. Every
# per-node component tolerates the amd.com/gpu taint AMD GPU pools
# carry - the chart defaults tolerate nothing, which strands the
# daemonsets off the very nodes they serve.
GPU_OPERATOR = Chart(
    key="amd-gpu-operator",
    release="mp-gpu-operator-charts",
    namespace="kube-amd-gpu",
    chart="gpu-operator-charts",
    repository="https://rocm.github.io/gpu-operator",
    version="v1.5.1",
    depends_on=["cert-manager", "node-feature-discovery"],
    dra_drivers=["gpu.amd.com"],
    values={
        "node-feature-discovery": {"enabled": False},
        # Argo-based node remediation pulls a workflow controller the
        # serving stack doesn't need.
        "remediation": {"enabled": False, "installCRDs": False},
        "crds": {"defaultCR": {"upgrade": True}},
        "deviceConfig": {
            "spec": {
                "commonConfig": {
                    "utilsContainer": {"image": "docker.io/rocm/gpu-operator-utils:v1.5.1"},
                },
                "driver": {"enable": False},
                "devicePlugin": {
                    "enableDevicePlugin": False,
                    "nodeLabellerImage": "docker.io/rocm/k8s-device-plugin:labeller-1.31.0.11",
                    "nodeLabellerTolerations": [
                        {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                    ],
                },
                "metricsExporter": {
                    "image": "docker.io/rocm/device-metrics-exporter:v1.5.1",
                    # The ConfigMap above: profiler metrics on, for
                    # the tensor-activity mapping.
                    "config": {"name": "mp-metrics-exporter-config"},
                    "tolerations": [
                        {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                    ],
                },
                "draDriver": {
                    "enable": True,
                    "image": "docker.io/rocm/k8s-gpu-dra-driver:v1.0.1",
                    "tolerations": [
                        {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                    ],
                },
            },
        },
    },
)
