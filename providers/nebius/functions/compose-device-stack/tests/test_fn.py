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
from models.ai.modelplane.nebius.nebiusdevicestack import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The metrics the DCGM exporter Release configures, as the SDK pins them.
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


def _xr(*, dra_drivers: list[v1alpha1.DraDriver] | None) -> fnv1.Resource:
    """The prod NebiusDeviceStack XR, with its kubeconfig and Nebius credentials Secrets, and DRA drivers unless they're None."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.NebiusDeviceStack(
                metadata=metav1.ObjectMeta(name="prod", namespace="modelplane-system"),
                spec=v1alpha1.Spec(
                    secrets=[
                        v1alpha1.Secret(
                            type="Kubeconfig",
                            name="prod-kubeconfig-6c1d0",
                            namespace="modelplane-system",
                            key="kubeconfig",
                        ),
                        v1alpha1.Secret(
                            type="NebiusServiceAccountCredentials",
                            name="nebius-credentials",
                            namespace="crossplane-system",
                            key="credentials.json",
                        ),
                    ],
                    draDrivers=dra_drivers,
                ),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        ),
    )


def _provider_config_kubernetes(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed provider-kubernetes ProviderConfig, reaching the cluster through its kubeconfig as the Nebius service account."""
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
                    "identity": {
                        "type": "NebiusServiceAccountCredentials",
                        "source": "Secret",
                        "secretRef": {
                            "name": "nebius-credentials",
                            "namespace": "crossplane-system",
                            "key": "credentials.json",
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _provider_config_helm(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed provider-helm ProviderConfig, reaching the cluster through its kubeconfig as the Nebius service account."""
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
                    "identity": {
                        "type": "NebiusServiceAccountCredentials",
                        "source": "Secret",
                        "secretRef": {
                            "name": "nebius-credentials",
                            "namespace": "crossplane-system",
                            "key": "credentials.json",
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


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    Case(
        name="FirstPass",
        reason=(
            "With nothing observed, a NebiusDeviceStack composes only its ProviderConfigs, authenticating as the "
            "Nebius service account, not yet ready, and no components until both are observed."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")]),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_UNSPECIFIED),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="ProviderConfigsObserved",
        reason=(
            "Once both ProviderConfigs are observed, a NebiusDeviceStack claiming gpu.nvidia.com composes the NFD, "
            "NVIDIA DRA driver and DCGM exporter Releases without Delete, and a Usage of the ProviderConfig by each."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")]),
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
                    "node-feature-discovery": fnv1.Resource(
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
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Exists",
                                                        "effect": "NoSchedule",
                                                    },
                                                ],
                                            },
                                        },
                                    },
                                },
                            }
                        ),
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
                                            "resources": {"computeDomains": {"enabled": False}},
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "dcgm-exporter": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-dcgm-exporter"},
                                    "labels": {
                                        "modelplane.ai/resource": "dcgm-exporter",
                                        "modelplane.ai/usage-consumer": "dcgm-exporter",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "chart": {
                                            "name": "dcgm-exporter",
                                            "repository": "https://nvidia.github.io/dcgm-exporter/helm-charts",
                                            "version": "4.8.4",
                                        },
                                        "namespace": "dcgm-exporter",
                                        "values": {
                                            "serviceMonitor": {"enabled": False},
                                            "customMetrics": _DCGM_METRICS_CSV,
                                            "kubernetesDRA": {"enabled": True},
                                            "kubernetes": {
                                                "enablePodLabels": True,
                                                "podLabelAllowlistRegex": [
                                                    r"^modelplane\.ai/(deployment|replica|role|engine)$"
                                                ],
                                            },
                                            "tolerations": [
                                                {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                                            ],
                                            "affinity": {
                                                "nodeAffinity": {
                                                    "requiredDuringSchedulingIgnoredDuringExecution": {
                                                        "nodeSelectorTerms": [
                                                            {
                                                                "matchExpressions": [
                                                                    {
                                                                        "key": "feature.node.kubernetes.io/pci-10de.present",
                                                                        "operator": "In",
                                                                        "values": ["true"],
                                                                    },
                                                                ],
                                                            },
                                                            {
                                                                "matchExpressions": [
                                                                    {
                                                                        "key": "feature.node.kubernetes.io/pci-0302_10de.present",
                                                                        "operator": "In",
                                                                        "values": ["true"],
                                                                    },
                                                                ],
                                                            },
                                                            {
                                                                "matchExpressions": [
                                                                    {
                                                                        "key": "feature.node.kubernetes.io/pci-0300_10de.present",
                                                                        "operator": "In",
                                                                        "values": ["true"],
                                                                    },
                                                                ],
                                                            },
                                                        ],
                                                    },
                                                },
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "usage-pc-node-feature-discovery": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "node-feature-discovery"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-nvidia-dra-driver-gpu": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "nvidia-dra-driver-gpu"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-dcgm-exporter": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "dcgm-exporter"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="NoGPUDriver",
        reason="A NebiusDeviceStack claiming no DRA driver composes its ProviderConfigs and no components.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=None),
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
    Case(
        name="AllReady",
        reason="With all three Releases observed Ready, a NebiusDeviceStack marks them and its ProviderConfigs ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")]),
                resources={
                    "provider-config-kubernetes": _observed_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1"
                    ),
                    "provider-config-helm": _observed_provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                    "node-feature-discovery": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
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
                    ),
                    "nvidia-dra-driver-gpu": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
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
                    ),
                    "dcgm-exporter": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
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
                    ),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_TRUE),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_TRUE),
                    "node-feature-discovery": fnv1.Resource(
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
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Exists",
                                                        "effect": "NoSchedule",
                                                    },
                                                ],
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
                                            "resources": {"computeDomains": {"enabled": False}},
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "dcgm-exporter": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-dcgm-exporter"},
                                    "labels": {
                                        "modelplane.ai/resource": "dcgm-exporter",
                                        "modelplane.ai/usage-consumer": "dcgm-exporter",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "chart": {
                                            "name": "dcgm-exporter",
                                            "repository": "https://nvidia.github.io/dcgm-exporter/helm-charts",
                                            "version": "4.8.4",
                                        },
                                        "namespace": "dcgm-exporter",
                                        "values": {
                                            "serviceMonitor": {"enabled": False},
                                            "customMetrics": _DCGM_METRICS_CSV,
                                            "kubernetesDRA": {"enabled": True},
                                            "kubernetes": {
                                                "enablePodLabels": True,
                                                "podLabelAllowlistRegex": [
                                                    r"^modelplane\.ai/(deployment|replica|role|engine)$"
                                                ],
                                            },
                                            "tolerations": [
                                                {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                                            ],
                                            "affinity": {
                                                "nodeAffinity": {
                                                    "requiredDuringSchedulingIgnoredDuringExecution": {
                                                        "nodeSelectorTerms": [
                                                            {
                                                                "matchExpressions": [
                                                                    {
                                                                        "key": "feature.node.kubernetes.io/pci-10de.present",
                                                                        "operator": "In",
                                                                        "values": ["true"],
                                                                    },
                                                                ],
                                                            },
                                                            {
                                                                "matchExpressions": [
                                                                    {
                                                                        "key": "feature.node.kubernetes.io/pci-0302_10de.present",
                                                                        "operator": "In",
                                                                        "values": ["true"],
                                                                    },
                                                                ],
                                                            },
                                                            {
                                                                "matchExpressions": [
                                                                    {
                                                                        "key": "feature.node.kubernetes.io/pci-0300_10de.present",
                                                                        "operator": "In",
                                                                        "values": ["true"],
                                                                    },
                                                                ],
                                                            },
                                                        ],
                                                    },
                                                },
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-node-feature-discovery": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "node-feature-discovery"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-nvidia-dra-driver-gpu": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "nvidia-dra-driver-gpu"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-dcgm-exporter": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "dcgm-exporter"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction installs the device stacks an mk8s cluster's InferenceClasses claim."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
