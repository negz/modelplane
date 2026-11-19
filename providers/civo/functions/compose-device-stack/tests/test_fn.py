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

"""Tests for the compose-device-stack function."""

import asyncio
import dataclasses
import json

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.civo.civodevicestack import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The metrics the gpu-operator's DCGM exporter configures, as the SDK pins them.
_DCGM_METRICS_CSV = """# Clocks,,
DCGM_FI_DEV_SM_CLOCK,     gauge, SM clock frequency (in MHz).
DCGM_FI_DEV_MEM_CLOCK, gauge, Memory clock frequency (in MHz).
# Temperature,,
DCGM_FI_DEV_MEMORY_TEMP, gauge, Memory temperature (in C).
DCGM_FI_DEV_GPU_TEMP,    gauge, GPU temperature (in C).
# Power,,
DCGM_FI_DEV_POWER_USAGE,  gauge, Power draw (in W).
DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION, counter, Total energy consumption since boot (in mJ).
# PCIe,,
DCGM_FI_PROF_PCIE_TX_BYTES,  counter, Total number of bytes transmitted through PCIe TX (in KB) via NVML.
DCGM_FI_PROF_PCIE_RX_BYTES,  counter, Total number of bytes received through PCIe RX (in KB) via NVML.
DCGM_FI_DEV_PCIE_REPLAY_COUNTER, counter, Total number of PCIe retries.
# Utilization (the sample period varies depending on the product),,
DCGM_FI_DEV_GPU_UTIL,      gauge, GPU utilization (in %).
DCGM_FI_DEV_MEM_COPY_UTIL, gauge, Memory utilization (in %).
DCGM_FI_DEV_ENC_UTIL,      gauge, Encoder utilization (in %).
DCGM_FI_DEV_DEC_UTIL,      gauge, Decoder utilization (in %).
# Errors and violations,,
DCGM_FI_DEV_XID_ERRORS,            gauge, Value of the last XID error encountered.
DCGM_FI_DEV_POWER_VIOLATION,       counter, Throttling duration due to power constraints (in us).
DCGM_FI_DEV_THERMAL_VIOLATION,     counter, Throttling duration due to thermal constraints (in us).
DCGM_FI_DEV_SYNC_BOOST_VIOLATION,  counter, Throttling duration due to sync-boost constraints (in us).
DCGM_FI_DEV_BOARD_LIMIT_VIOLATION, counter, Throttling duration due to board limit constraints (in us).
DCGM_FI_DEV_LOW_UTIL_VIOLATION,    counter, Throttling duration due to low utilization (in us).
DCGM_FI_DEV_RELIABILITY_VIOLATION, counter, Throttling duration due to reliability constraints (in us).
# Memory usage,,
DCGM_FI_DEV_FB_FREE, gauge, Framebuffer memory free (in MiB).
DCGM_FI_DEV_FB_USED, gauge, Framebuffer memory used (in MiB).
# Retired pages,,
DCGM_FI_DEV_RETIRED_SBE,     counter, Total number of retired pages due to single-bit errors.
DCGM_FI_DEV_RETIRED_DBE,     counter, Total number of retired pages due to double-bit errors.
DCGM_FI_DEV_RETIRED_PENDING, counter, Total number of pages pending retirement.
# NVLink,,
DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL, counter, Total number of NVLink bandwidth counters for all lanes
DCGM_FI_PROF_NVLINK_TX_BYTES,       counter, The rate of data transmitted over NVLink not including protocol headers in bytes per second.
DCGM_FI_PROF_NVLINK_RX_BYTES,       counter, The rate of data received over NVLink not including protocol headers in bytes per second.
# Add DCP metrics,,
DCGM_FI_PROF_GR_ENGINE_ACTIVE,   gauge, Ratio of time the graphics engine is active (in %).
DCGM_FI_PROF_SM_ACTIVE,          gauge, The ratio of cycles an SM has at least 1 warp assigned (in %).
DCGM_FI_PROF_SM_OCCUPANCY,       gauge, The ratio of number of warps resident on an SM (in %).
DCGM_FI_PROF_PIPE_TENSOR_ACTIVE, gauge, Ratio of cycles the tensor (HMMA) pipe is active (in %).
DCGM_FI_PROF_DRAM_ACTIVE,        gauge, Ratio of cycles the device memory interface is active sending or receiving data (in %).
DCGM_FI_PROF_PCIE_TX_BYTES,      counter, The number of bytes of active pcie tx data including both header and payload.
DCGM_FI_PROF_PCIE_RX_BYTES,      counter, The number of bytes of active pcie rx data including both header and payload.
# BCP Additional metrics
DCGM_FI_DEV_GPU_NVLINK_ERRORS,      gauge, Identifies a GPU NVLink error type returned by DCGM_FI_DEV_GPU_NVLINK_ERRORS.
# Added RunAI Additional metrics https://docs.run.ai/latest/developer/metrics/metrics-api/#advanced-metrics
## NVLink
DCGM_FI_DEV_NVLINK_BANDWIDTH_L0, counter, The number of bytes of active NVLink rx or tx data including both header and payload.
## VGPU License status
DCGM_FI_DEV_VGPU_LICENSE_STATUS, gauge, vGPU License status
## Remapped rows
DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS, counter, Number of remapped rows for uncorrectable errors
DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS, counter, Number of remapped rows for correctable errors
DCGM_FI_DEV_ROW_REMAP_FAILURE, gauge, Whether remapping of rows has failed
## Static configuration information. These appear as labels on the other metrics
DCGM_FI_DRIVER_VERSION, label, Driver Version
## Profiling metrics
DCGM_FI_PROF_PIPE_FP64_ACTIVE, gauge, Ratio of cycles the fp64 pipes are active (in %).
DCGM_FI_PROF_PIPE_FP32_ACTIVE, gauge, Ratio of cycles the fp32 pipes are active (in %).
DCGM_FI_PROF_PIPE_FP16_ACTIVE, gauge, Ratio of cycles the fp16 pipes are active (in %).
"""


@dataclasses.dataclass
class Case:
    """A test case for compose-device-stack."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(
    *, dra_drivers: list[v1alpha1.DraDriver] | None, nvlink_disabled_pools: list[v1alpha1.NvlinkDisabledPool] | None
) -> fnv1.Resource:
    """The prod CivoDeviceStack XR, reaching its cluster through a kubeconfig Secret, with the given DRA drivers and NVLink-disabled pools unless they're None."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.CivoDeviceStack(
                metadata=metav1.ObjectMeta(name="prod", namespace="modelplane-system"),
                spec=v1alpha1.Spec(
                    secrets=[
                        v1alpha1.Secret(
                            type="Kubeconfig",
                            name="prod-kubeconfig-6c1d0",
                            namespace="modelplane-system",
                            key="kubeconfig",
                        ),
                    ],
                    draDrivers=dra_drivers,
                    nvlinkDisabledPools=nvlink_disabled_pools,
                ),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        ),
    )


def _provider_config_kubernetes(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed provider-kubernetes ProviderConfig, reaching the cluster through its kubeconfig Secret."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "ProviderConfig",
                "metadata": {"name": "prod-cluster-0fab1"},
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {
                            "name": "prod-kubeconfig-6c1d0",
                            "namespace": "modelplane-system",
                            "key": "kubeconfig",
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _provider_config_helm(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed provider-helm ProviderConfig, reaching the cluster through its kubeconfig Secret."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "ProviderConfig",
                "metadata": {"name": "prod-cluster-0fab1"},
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {
                            "name": "prod-kubeconfig-6c1d0",
                            "namespace": "modelplane-system",
                            "key": "kubeconfig",
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _observed_provider_config(*, api_version: str) -> fnv1.Resource:
    """A ProviderConfig of the given apiVersion as observed, which has no status to report."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": api_version,
                "kind": "ProviderConfig",
                "metadata": {"name": "prod-cluster-0fab1", "namespace": "modelplane-system"},
            }
        ),
    )


def _observed_ready() -> fnv1.Resource:
    """A composed component as observed, with a Ready condition that's True."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "status": {
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True",
                            "reason": "Available",
                            "lastTransitionTime": "2024-01-01T00:00:00Z",
                        },
                    ],
                },
            }
        ),
    )


def _cert_manager(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed observe-only Object watching the serving stack's cert-manager webhook."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {
                    "labels": {
                        "modelplane.ai/resource": "cert-manager",
                        "modelplane.ai/usage-consumer": "cert-manager",
                    },
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                    "managementPolicies": ["Observe"],
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "metadata": {"name": "cert-manager-webhook", "namespace": "cert-manager"},
                        },
                    },
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": (
                            "has(object.status) && has(object.status.availableReplicas)"
                            " && object.status.availableReplicas >= 1"
                        ),
                    },
                },
            }
        ),
        ready=ready,
    )


def _node_feature_discovery(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed NFD Release, whose worker tolerates the GPU taint."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-node-feature-discovery"},
                    "labels": {
                        "modelplane.ai/resource": "node-feature-discovery",
                        "modelplane.ai/usage-consumer": "node-feature-discovery",
                    },
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                    "managementPolicies": ["Observe", "Create", "Update"],
                    "forProvider": {
                        "chart": {
                            "name": "node-feature-discovery",
                            "repository": "https://kubernetes-sigs.github.io/node-feature-discovery/charts",
                            "version": "0.19.0",
                        },
                        "namespace": "node-feature-discovery",
                        "wait": True,
                        "waitTimeout": "10m",
                        "values": {
                            "worker": {
                                "tolerations": [
                                    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                                ],
                            },
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _usage(
    *, of_api_version: str, of_kind: str, of_key: str, by_api_version: str, by_kind: str, by_key: str
) -> fnv1.Resource:
    """The composed Usage holding the component labelled of_key until the one labelled by_key is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": of_api_version,
                        "kind": of_kind,
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": of_key},
                        },
                    },
                    "by": {
                        "apiVersion": by_api_version,
                        "kind": by_kind,
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": by_key},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_provider_config(*, api_version: str, kind: str, consumer: str) -> fnv1.Resource:
    """The composed Usage holding the ProviderConfig of the given apiVersion until the consumer, of the given kind, is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "of": {
                        "apiVersion": api_version,
                        "kind": "ProviderConfig",
                        "resourceRef": {"name": "prod-cluster-0fab1"},
                    },
                    "by": {
                        "apiVersion": api_version,
                        "kind": kind,
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/usage-consumer": consumer},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    # The teardown Usages compose ungated, because they only order deletion.
    Case(
        name="FirstPass",
        reason=(
            "With nothing observed, a CivoDeviceStack composes its ProviderConfigs, not yet ready, and the Usages "
            "ordering its components' teardown, but no components until both ProviderConfigs are observed."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")], nvlink_disabled_pools=None),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_UNSPECIFIED),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_UNSPECIFIED),
                    "usage-node-feature-discovery-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrole": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrole",
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrolebinding": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrolebinding",
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The gpu-operator waits on NFD and the serving stack's cert-manager, and
    # everything else waits on the gpu-operator.
    Case(
        name="ProviderConfigsObserved",
        reason=(
            "Once both ProviderConfigs are observed, a CivoDeviceStack claiming gpu.nvidia.com composes the components "
            "with no dependencies: NFD, and an observe-only Object watching the serving stack's cert-manager."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")], nvlink_disabled_pools=None),
                resources={
                    "provider-config-kubernetes": _observed_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1"
                    ),
                    "provider-config-helm": _observed_provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_TRUE),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_TRUE),
                    "cert-manager": _cert_manager(ready=fnv1.READY_UNSPECIFIED),
                    "node-feature-discovery": _node_feature_discovery(ready=fnv1.READY_UNSPECIFIED),
                    "usage-node-feature-discovery-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrole": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrole",
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrolebinding": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrolebinding",
                    ),
                    "usage-pc-cert-manager": _usage_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1", kind="Object", consumer="cert-manager"
                    ),
                    "usage-pc-node-feature-discovery": _usage_provider_config(
                        api_version="helm.m.crossplane.io/v1beta1", kind="Release", consumer="node-feature-discovery"
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The gpu-operator keeps its ClusterPolicy-managed driver, and the DRA
    # driver points at the root that driver installs to.
    Case(
        name="NvLinkEnabled",
        reason=(
            "A CivoDeviceStack naming no NVLink-disabled pool, whose gpu-operator is Ready, composes the stock "
            "gpu-operator, the DRA driver and the DCGM exporter's RBAC, with no NVIDIADriver or kernel module "
            "ConfigMap."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")], nvlink_disabled_pools=None),
                resources={
                    "provider-config-kubernetes": _observed_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1"
                    ),
                    "provider-config-helm": _observed_provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                    "cert-manager": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "gpu-operator": _observed_ready(),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_TRUE),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_TRUE),
                    "cert-manager": _cert_manager(ready=fnv1.READY_TRUE),
                    "node-feature-discovery": _node_feature_discovery(ready=fnv1.READY_TRUE),
                    "gpu-operator": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-gpu-operator"},
                                    "labels": {
                                        "modelplane.ai/resource": "gpu-operator",
                                        "modelplane.ai/usage-consumer": "gpu-operator",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "chart": {
                                            "name": "gpu-operator",
                                            "repository": "https://helm.ngc.nvidia.com/nvidia",
                                            "version": "v26.7.1",
                                        },
                                        "namespace": "gpu-operator",
                                        "wait": True,
                                        "waitTimeout": "10m",
                                        "values": {
                                            "ccManager": {"enabled": False},
                                            "cdi": {"default": True, "enabled": True},
                                            "daemonsets": {
                                                "tolerations": [
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Exists",
                                                        "effect": "NoSchedule",
                                                    },
                                                ],
                                            },
                                            "dcgm": {"enabled": True},
                                            "dcgmExporter": {
                                                "config": {
                                                    "create": True,
                                                    "data": _DCGM_METRICS_CSV,
                                                    "name": "dcgm-exporter",
                                                },
                                                "serviceMonitor": {"enabled": False},
                                                "enablePodLabels": True,
                                                "podLabelAllowlistRegex": [
                                                    r"^modelplane\.ai/(deployment|replica|role|engine)$"
                                                ],
                                                "env": [{"name": "KUBERNETES_ENABLE_DRA", "value": "true"}],
                                            },
                                            "devicePlugin": {"enabled": False},
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
                                            "migManager": {"enabled": False},
                                            "nfd": {"enabled": False},
                                            "operator": {
                                                "resources": {
                                                    "limits": {"cpu": "500m", "memory": "700Mi"},
                                                    "requests": {"cpu": "200m", "memory": "300Mi"},
                                                },
                                                "tolerations": [],
                                                "upgradeCRD": True,
                                            },
                                            "toolkit": {"enabled": False},
                                            "validator": {
                                                "plugin": {"env": [{"name": "WITH_WORKLOAD", "value": "false"}]}
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "nvidia-dra-driver-gpu": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-dra-driver-nvidia-gpu"},
                                    "labels": {
                                        "modelplane.ai/resource": "nvidia-dra-driver-gpu",
                                        "modelplane.ai/usage-consumer": "nvidia-dra-driver-gpu",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "chart": {
                                            "name": "dra-driver-nvidia-gpu",
                                            "repository": "oci://registry.k8s.io/dra-driver-nvidia/charts",
                                            "version": "0.5.0",
                                        },
                                        "namespace": "nvidia-dra-driver",
                                        "values": {
                                            "gpuResourcesEnabledOverride": True,
                                            "nvidiaDriverRoot": "/run/nvidia/driver",
                                            "resources": {"computeDomains": {"enabled": False}},
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "dcgm-exporter-clusterrole": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "dcgm-exporter-clusterrole",
                                        "modelplane.ai/usage-consumer": "dcgm-exporter-clusterrole",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "manifest": {
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
                                    },
                                },
                            }
                        ),
                    ),
                    "dcgm-exporter-clusterrolebinding": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "dcgm-exporter-clusterrolebinding",
                                        "modelplane.ai/usage-consumer": "dcgm-exporter-clusterrolebinding",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "manifest": {
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
                                    },
                                },
                            }
                        ),
                    ),
                    "usage-node-feature-discovery-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrole": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrole",
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrolebinding": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrolebinding",
                    ),
                    "usage-pc-cert-manager": _usage_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1", kind="Object", consumer="cert-manager"
                    ),
                    "usage-pc-node-feature-discovery": _usage_provider_config(
                        api_version="helm.m.crossplane.io/v1beta1", kind="Release", consumer="node-feature-discovery"
                    ),
                    "usage-pc-gpu-operator": _usage_provider_config(
                        api_version="helm.m.crossplane.io/v1beta1", kind="Release", consumer="gpu-operator"
                    ),
                    "usage-pc-nvidia-dra-driver-gpu": _usage_provider_config(
                        api_version="helm.m.crossplane.io/v1beta1", kind="Release", consumer="nvidia-dra-driver-gpu"
                    ),
                    "usage-pc-dcgm-exporter-clusterrole": _usage_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1",
                        kind="Object",
                        consumer="dcgm-exporter-clusterrole",
                    ),
                    "usage-pc-dcgm-exporter-clusterrolebinding": _usage_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1",
                        kind="Object",
                        consumer="dcgm-exporter-clusterrolebinding",
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The pool's NVIDIADriver isn't Ready yet, so the DRA driver, which waits on
    # it, isn't composed. The Usages hold the operator release and the kernel
    # module config until the pool's driver is gone, and it until the DRA driver
    # is.
    Case(
        name="NvLinkDisabled",
        reason=(
            "A CivoDeviceStack naming an NVLink-disabled pool composes the gpu-operator in NVIDIADriver-CRD mode, the "
            "kernel module ConfigMap, and an NVIDIADriver selecting that pool's nodes."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")],
                    nvlink_disabled_pools=[v1alpha1.NvlinkDisabledPool("h100-pool")],
                ),
                resources={
                    "provider-config-kubernetes": _observed_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1"
                    ),
                    "provider-config-helm": _observed_provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                    "cert-manager": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "gpu-operator": _observed_ready(),
                    "nvlink-disable-config-gpu-operator": _observed_ready(),
                    "nvlink-disable-config-nvidia-kernel-config": _observed_ready(),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_TRUE),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_TRUE),
                    "cert-manager": _cert_manager(ready=fnv1.READY_TRUE),
                    "node-feature-discovery": _node_feature_discovery(ready=fnv1.READY_TRUE),
                    "gpu-operator": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-gpu-operator"},
                                    "labels": {
                                        "modelplane.ai/resource": "gpu-operator",
                                        "modelplane.ai/usage-consumer": "gpu-operator",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "chart": {
                                            "name": "gpu-operator",
                                            "repository": "https://helm.ngc.nvidia.com/nvidia",
                                            "version": "v26.7.1",
                                        },
                                        "namespace": "gpu-operator",
                                        "wait": True,
                                        "waitTimeout": "10m",
                                        "values": {
                                            "ccManager": {"enabled": False},
                                            "cdi": {"default": True, "enabled": True},
                                            "daemonsets": {
                                                "tolerations": [
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Exists",
                                                        "effect": "NoSchedule",
                                                    },
                                                ],
                                            },
                                            "dcgm": {"enabled": True},
                                            "dcgmExporter": {
                                                "config": {
                                                    "create": True,
                                                    "data": _DCGM_METRICS_CSV,
                                                    "name": "dcgm-exporter",
                                                },
                                                "serviceMonitor": {"enabled": False},
                                                "enablePodLabels": True,
                                                "podLabelAllowlistRegex": [
                                                    r"^modelplane\.ai/(deployment|replica|role|engine)$"
                                                ],
                                                "env": [{"name": "KUBERNETES_ENABLE_DRA", "value": "true"}],
                                            },
                                            "devicePlugin": {"enabled": False},
                                            "driver": {
                                                "enabled": True,
                                                "kernelModuleType": "open",
                                                "maxParallelUpgrades": 5,
                                                "rdma": {"enabled": False},
                                                "version": "580.173.02",
                                                "nvidiaDriverCRD": {"enabled": True, "deployDefaultCR": True},
                                            },
                                            "fullnameOverride": "gpu-operator",
                                            "gdrcopy": {"enabled": False},
                                            "gfd": {"enabled": True},
                                            "kataSandboxDevicePlugin": {"enabled": False},
                                            "migManager": {"enabled": False},
                                            "nfd": {"enabled": False},
                                            "operator": {
                                                "resources": {
                                                    "limits": {"cpu": "500m", "memory": "700Mi"},
                                                    "requests": {"cpu": "200m", "memory": "300Mi"},
                                                },
                                                "tolerations": [],
                                                "upgradeCRD": True,
                                            },
                                            "toolkit": {"enabled": False},
                                            "validator": {
                                                "plugin": {"env": [{"name": "WITH_WORKLOAD", "value": "false"}]}
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "dcgm-exporter-clusterrole": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "dcgm-exporter-clusterrole",
                                        "modelplane.ai/usage-consumer": "dcgm-exporter-clusterrole",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "manifest": {
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
                                    },
                                },
                            }
                        ),
                    ),
                    "dcgm-exporter-clusterrolebinding": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "dcgm-exporter-clusterrolebinding",
                                        "modelplane.ai/usage-consumer": "dcgm-exporter-clusterrolebinding",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "manifest": {
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
                                    },
                                },
                            }
                        ),
                    ),
                    "nvlink-disable-config-gpu-operator": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "nvlink-disable-config-gpu-operator",
                                        "modelplane.ai/usage-consumer": "nvlink-disable-config-gpu-operator",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Namespace",
                                            "metadata": {"name": "gpu-operator"},
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "nvlink-disable-config-nvidia-kernel-config": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "nvlink-disable-config-nvidia-kernel-config",
                                        "modelplane.ai/usage-consumer": "nvlink-disable-config-nvidia-kernel-config",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "ConfigMap",
                                            "metadata": {"name": "nvidia-kernel-config", "namespace": "gpu-operator"},
                                            "data": {"nvidia.conf": "options nvidia NVreg_NvLinkDisable=1"},
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "nvlink-disabled-driver-h100-pool": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "nvlink-disabled-driver-h100-pool",
                                        "modelplane.ai/usage-consumer": "nvlink-disabled-driver-h100-pool",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "nvidia.com/v1alpha1",
                                            "kind": "NVIDIADriver",
                                            "metadata": {"name": "nvlink-disabled-h100-pool"},
                                            "spec": {
                                                "driverType": "gpu",
                                                "version": "580.173.02",
                                                "useOpenKernelModules": True,
                                                "nodeSelector": {"modelplane.ai/pool": "h100-pool"},
                                                "kernelModuleConfig": {"name": "nvidia-kernel-config"},
                                                "tolerations": [
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Exists",
                                                        "effect": "NoSchedule",
                                                    },
                                                ],
                                            },
                                        },
                                    },
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": 'object.status.state == "ready"',
                                    },
                                },
                            }
                        ),
                    ),
                    "usage-node-feature-discovery-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrole": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrole",
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrolebinding": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrolebinding",
                    ),
                    "usage-nvlink-disabled-driver-h100-pool-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="nvlink-disabled-driver-h100-pool",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-gpu-operator-by-nvlink-disabled-driver-h100-pool": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="nvlink-disabled-driver-h100-pool",
                    ),
                    "usage-nvlink-disable-config-gpu-operator-by-nvlink-disabled-driver-h100-pool": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="nvlink-disable-config-gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="nvlink-disabled-driver-h100-pool",
                    ),
                    "usage-nvlink-disable-config-nvidia-kernel-config-by-nvlink-disabled-driver-h100-pool": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="nvlink-disable-config-nvidia-kernel-config",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="nvlink-disabled-driver-h100-pool",
                    ),
                    "usage-pc-cert-manager": _usage_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1", kind="Object", consumer="cert-manager"
                    ),
                    "usage-pc-node-feature-discovery": _usage_provider_config(
                        api_version="helm.m.crossplane.io/v1beta1", kind="Release", consumer="node-feature-discovery"
                    ),
                    "usage-pc-gpu-operator": _usage_provider_config(
                        api_version="helm.m.crossplane.io/v1beta1", kind="Release", consumer="gpu-operator"
                    ),
                    "usage-pc-dcgm-exporter-clusterrole": _usage_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1",
                        kind="Object",
                        consumer="dcgm-exporter-clusterrole",
                    ),
                    "usage-pc-dcgm-exporter-clusterrolebinding": _usage_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1",
                        kind="Object",
                        consumer="dcgm-exporter-clusterrolebinding",
                    ),
                    "usage-pc-nvlink-disable-config-gpu-operator": _usage_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1",
                        kind="Object",
                        consumer="nvlink-disable-config-gpu-operator",
                    ),
                    "usage-pc-nvlink-disable-config-nvidia-kernel-config": _usage_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1",
                        kind="Object",
                        consumer="nvlink-disable-config-nvidia-kernel-config",
                    ),
                    "usage-pc-nvlink-disabled-driver-h100-pool": _usage_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1",
                        kind="Object",
                        consumer="nvlink-disabled-driver-h100-pool",
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # cert-manager is observed for the gpu-operator's sake, so with the
    # gpu-operator filtered out nothing observes it either.
    Case(
        name="NoGPUDriver",
        reason="A CivoDeviceStack claiming no DRA driver composes its ProviderConfigs and no components.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=None, nvlink_disabled_pools=None),
                resources={
                    "provider-config-kubernetes": _observed_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1"
                    ),
                    "provider-config-helm": _observed_provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_TRUE),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction installs the device stacks a Civo cluster's InferenceClasses claim."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
