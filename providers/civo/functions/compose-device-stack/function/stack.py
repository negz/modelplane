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

"""The device stacks the provider installs on a Civo cluster.

No generator covers Civo, so the components are hand-written, and
NVIDIA's pins come from the SDK where Civo installs the same chart.

Civo pre-installs no GPU stack: its GPU node images ship the NVIDIA
container toolkit but no driver, and there is no managed GPU Operator
add-on. So this stack installs the GPU Operator itself: driver enabled
(the operator's containerized driver installs to /run/nvidia/driver),
toolkit disabled (baked into the node image), and device plugin
disabled so its nvidia.com/gpu ledger never books the same physical
devices the DRA driver's ResourceSlices allocate - the DRA driver stays
the sole GPU allocator. compose-civo-cluster composes no GPU readiness
gate (the operator it would observe is installed here, after the
cluster XR is Ready - gating would deadlock), so the operator chart's
wait=True and the DRA driver's depends_on are what keep GPU health on
the critical path.
"""

import copy
import dataclasses

from modelplane_sdk import stacks
from modelplane_sdk.stacks import nvidia

# The pool label compose-civo-cluster writes on every node pool's nodes.
# with_nvlink_disabled selects a pool's nodes by it, so a per-pool
# NVIDIADriver claims exactly that pool away from the default driver.
_LABEL_POOL = "modelplane.ai/pool"

# ConfigMap the per-pool NVIDIADrivers read kernel module options from.
_KERNEL_MODULE_CONFIGMAP = "nvidia-kernel-config"

# Component keys with_nvlink_disabled adds to the list. Driver entries are
# per pool, suffixed with the pool name.
_NVLINK_CONFIG_KEY = "nvlink-disable-config"
_NVLINK_DRIVER_KEY_PREFIX = "nvlink-disabled-driver"

# The GPU stack's DRA driver, which tags every component here.
_DRA_DRIVER_GPU = "gpu.nvidia.com"

COMPONENTS: list[stacks.Component] = [
    # gpu-operator's chart needs cert-manager, which the serving stack
    # installs on every cluster.
    stacks.CERT_MANAGER,
    nvidia.NODE_FEATURE_DISCOVERY,
    # Installs the NVIDIA driver on Civo's driverless GPU node images and
    # validates the GPU stack. wait=True makes Ready mean the operator's
    # validator passed, which the DRA driver below depends on - without a
    # working driver its kubelet plugin would publish no ResourceSlices.
    stacks.Chart(
        key="gpu-operator",
        release="mp-gpu-operator",
        namespace="gpu-operator",
        chart="gpu-operator",
        repository="https://helm.ngc.nvidia.com/nvidia",
        version=nvidia.GPU_OPERATOR_VERSION,
        wait=True,
        depends_on=["node-feature-discovery", "cert-manager"],
        dra_drivers=[_DRA_DRIVER_GPU],
        values={
            "ccManager": {"enabled": False},
            "cdi": {"default": True, "enabled": True},
            # The operator's DaemonSets must run on the GPU nodes, which
            # carry the nvidia.com/gpu taint from compose-civo-cluster.
            "daemonsets": {"tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]},
            # The DCGM exporter must run: without it every modelplane_gpu_*
            # series and modelplane_energy_joules_total is empty on Civo.
            # The ServiceMonitor stays off: the collector scrapes the
            # exporter pods directly, and a rendered monitor's CRD isn't on
            # this cluster.
            "dcgm": {"enabled": True},
            "dcgmExporter": {
                "config": {"create": True, "data": nvidia.DCGM_METRICS_CSV, "name": "dcgm-exporter"},
                "serviceMonitor": {"enabled": False},
                # Attribute each GPU's series to the pod holding it through
                # its DRA allocation, labelled with the pod labels the
                # collector joins a GPU series to its engine's on.
                "enablePodLabels": True,
                "podLabelAllowlistRegex": [r"^modelplane\.ai/(deployment|replica|role|engine)$"],
                "env": [{"name": "KUBERNETES_ENABLE_DRA", "value": "true"}],
            },
            # The DRA driver is the sole GPU allocator: the device
            # plugin's nvidia.com/gpu ledger would book the same physical
            # devices the DRA ResourceSlices allocate.
            "devicePlugin": {"enabled": False},
            # Civo GPU node images ship no driver; the operator's
            # containerized driver installs to /run/nvidia/driver, where
            # the DRA driver's nvidiaDriverRoot points. The L40S, A100 and
            # H100 all support the open kernel modules. Single-H100 pools
            # additionally need NVLink disabled at driver load;
            # with_nvlink_disabled scopes that to the pool.
            "driver": {
                "enabled": True,
                "kernelModuleType": "open",
                "maxParallelUpgrades": 5,
                "rdma": {"enabled": False},
                "version": "580.173.02",
            },
            "fullnameOverride": "gpu-operator",
            "gdrcopy": {"enabled": False},
            "gfd": {"enabled": True},
            "kataSandboxDevicePlugin": {"enabled": False},
            # No MIG on Civo's GPU shapes (L40S has no MIG; A100 pools are
            # whole-GPU).
            "migManager": {"enabled": False},
            # NODE_FEATURE_DISCOVERY above labels the nodes.
            "nfd": {"enabled": False},
            "operator": {
                "resources": {
                    "limits": {"cpu": "500m", "memory": "700Mi"},
                    "requests": {"cpu": "200m", "memory": "300Mi"},
                },
                "tolerations": [],
                "upgradeCRD": True,
            },
            # The NVIDIA container toolkit is baked into Civo's GPU node
            # images.
            "toolkit": {"enabled": False},
            "validator": {"plugin": {"env": [{"name": "WITH_WORKLOAD", "value": "false"}]}},
        },
    ),
    # The gpu-operator installs the containerized driver to
    # /run/nvidia/driver rather than the chart's default root, and the DRA
    # driver needs it running.
    dataclasses.replace(
        nvidia.DRA_DRIVER,
        depends_on=["gpu-operator"],
        values={**(nvidia.DRA_DRIVER.values or {}), "nvidiaDriverRoot": "/run/nvidia/driver"},
    ),
    # The operator's exporter ClusterRole grants pods only, and the
    # exporter's DRA mode reads resourceslices to map a claim's allocations
    # to GPUs. Without this grant it exports every field, but attaches no
    # pod labels.
    stacks.Manifests(
        key="dcgm-exporter-clusterrole",
        depends_on=["gpu-operator"],
        dra_drivers=[_DRA_DRIVER_GPU],
        manifests=[
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": "nvidia-dcgm-exporter-read-resourceslices"},
                "rules": [
                    {
                        "apiGroups": ["resource.k8s.io"],
                        "resources": ["resourceslices"],
                        "verbs": ["get", "list", "watch"],
                    },
                ],
            },
        ],
    ),
    stacks.Manifests(
        key="dcgm-exporter-clusterrolebinding",
        depends_on=["gpu-operator"],
        dra_drivers=[_DRA_DRIVER_GPU],
        manifests=[
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": "nvidia-dcgm-exporter-read-resourceslices"},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": "nvidia-dcgm-exporter-read-resourceslices",
                },
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": "nvidia-dcgm-exporter",
                        "namespace": "gpu-operator",
                    },
                ],
            },
        ],
    ),
]


def with_nvlink_disabled(components: list[stacks.Component], pools: list[str]) -> list[stacks.Component]:
    """The component list with the given pools' GPU driver loading NVLink
    disabled, leaving every other pool's driver untouched.

    Civo's single-H100 sizes are lone H100 SXM modules whose NVLink links
    have no peer: link bring-up fails and the driver never becomes ready
    unless loaded with NVreg_NvLinkDisable=1 (per Civo's GPU configuration
    docs, validated against GPU Operator v25.10.1). The parameter must not
    reach multi-GPU pools, whose GPUs NVLink interconnects, so the
    gpu-operator chart switches to NVIDIADriver-managed drivers: the
    chart's default NVIDIADriver (deployDefaultCR) keeps driving every GPU
    node not claimed by a more specific instance, and one NVIDIADriver per
    listed pool claims that pool's nodes - same driver pin as the chart's,
    plus the kernel module ConfigMap carrying the parameter.

    The per-pool NVIDIADrivers gate on the gpu-operator release (their CRD
    ships with it) and the DRA driver gates on them, so GPU health stays
    on the critical path: a pool whose driver never loads holds its
    NVIDIADriver's status.state off ready, which holds back the DRA
    driver exactly like an unhealthy operator does. The added components
    carry the GPU stack's driver like the ones they depend on, so they
    install only alongside them.
    """
    driver_keys = [f"{_NVLINK_DRIVER_KEY_PREFIX}-{pool}" for pool in pools]

    def transformed(c: stacks.Component) -> stacks.Component:
        if isinstance(c, stacks.Chart) and c.key == "gpu-operator":
            values = copy.deepcopy(c.values or {})
            # deployDefaultCR renders the chart's driver block as the
            # default (fallback) NVIDIADriver, so unlisted pools keep the
            # exact configuration they get in ClusterPolicy mode.
            values["driver"]["nvidiaDriverCRD"] = {"enabled": True, "deployDefaultCR": True}
            return dataclasses.replace(c, values=values)
        if isinstance(c, stacks.Chart) and c.key == "nvidia-dra-driver-gpu":
            return dataclasses.replace(c, depends_on=[*c.depends_on, *driver_keys])
        return c

    out: list[stacks.Component] = [transformed(c) for c in components]
    out.append(
        stacks.Manifests(
            key=_NVLINK_CONFIG_KEY,
            dra_drivers=[_DRA_DRIVER_GPU],
            # The namespace is created here rather than by the gpu-operator
            # release: the ConfigMap carries no dependency edge, so it can
            # apply before the chart.
            manifests=[
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": "gpu-operator"},
                },
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": _KERNEL_MODULE_CONFIGMAP,
                        "namespace": "gpu-operator",
                    },
                    "data": {"nvidia.conf": "options nvidia NVreg_NvLinkDisable=1"},
                },
            ],
        ),
    )
    for pool, key in zip(pools, driver_keys, strict=True):
        out.append(
            stacks.Manifests(
                key=key,
                depends_on=["gpu-operator", _NVLINK_CONFIG_KEY],
                dra_drivers=[_DRA_DRIVER_GPU],
                # Ready only once the operator reports the pool's driver
                # rolled out, so the DRA driver's install gate orders on
                # driver health, not CR admission.
                ready='object.status.state == "ready"',
                manifests=[
                    {
                        "apiVersion": "nvidia.com/v1alpha1",
                        "kind": "NVIDIADriver",
                        "metadata": {"name": f"nvlink-disabled-{pool}"},
                        "spec": {
                            "driverType": "gpu",
                            # Driver pin and module flavor mirror the chart's
                            # driver block; one review moves both.
                            "version": "580.173.02",
                            "useOpenKernelModules": True,
                            "nodeSelector": {_LABEL_POOL: pool},
                            "kernelModuleConfig": {"name": _KERNEL_MODULE_CONFIGMAP},
                            "tolerations": [
                                {
                                    "key": "nvidia.com/gpu",
                                    "operator": "Exists",
                                    "effect": "NoSchedule",
                                },
                            ],
                        },
                    },
                ],
            ),
        )
    return out
