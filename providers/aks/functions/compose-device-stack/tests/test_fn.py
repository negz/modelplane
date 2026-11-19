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
from models.ai.modelplane.aks.aksdevicestack import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The metrics the GPU Operator's DCGM exporter configures, as the generated
# stack pins them.
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

# The script the toolkit-hardening DaemonSet runs, as the generated stack
# pins it.
_TOOLKIT_HARDENING_SCRIPT = """prev=""
while true; do
  if chroot /host nvidia-ctk config --in-place \
       --set accept-nvidia-visible-devices-envvar-when-unprivileged=false \
       --set accept-nvidia-visible-devices-as-volume-mounts=true >/dev/null 2>&1; then
    cur=hardened
    # Fail-closed sentinel: present only while the CURRENT assert
    # succeeds. Touched on success, removed on any failure below,
    # so the readiness probe tracks live state instead of latching.
    touch /tmp/hardened
  else
    cur=failed
    # Assert failed (nvidia-ctk missing/incompatible): the keys
    # could not be hardened. Drop out of Ready so the inability to
    # harden surfaces. (A merely-reverted-but-valid config is not
    # a failure here — the --in-place re-assert above repairs it.)
    rm -f /tmp/hardened
  fi
  if [ "$cur" != "$prev" ]; then
    if [ "$cur" = "hardened" ]; then
      echo "nvidia-container-toolkit device-isolation keys asserted"
    else
      echo "nvidia-ctk config assert failed (toolkit missing or incompatible); NOT ready, retrying every 60s"
    fi
    prev=$cur
  fi
  sleep 60
done
"""


@dataclasses.dataclass
class Case:
    """A test case for compose-device-stack."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(*, dra_drivers: list[v1alpha1.DraDriver] | None) -> fnv1.Resource:
    """The prod AKSDeviceStack XR, reaching its cluster through a kubeconfig Secret, with DRA drivers unless they're None."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.AKSDeviceStack(
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


def _teardown_usage(*, of_key: str, by_api_version: str, by_kind: str, by_key: str) -> fnv1.Resource:
    """A composed Usage holding the Release of_key until the dependent by_key, of the given apiVersion and kind, is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
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


# Used by one case, which observes every component Ready. Written out, those
# nine observations would bury what the case composes.
def _observed_ready(*, api_version: str, kind: str) -> fnv1.Resource:
    """A composed resource of the given apiVersion and kind as observed, reporting Ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": api_version,
                "kind": kind,
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


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    Case(
        name="FirstPass",
        reason=(
            "With nothing observed, an AKSDeviceStack composes its ProviderConfigs, not yet ready, and the Usages "
            "ordering its teardown, but no components until both ProviderConfigs are observed."
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
                    "usage-gpu-operator-by-dcgm-exporter-clusterrole": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrole",
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrolebinding": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrolebinding",
                    ),
                    "usage-gpu-operator-by-gpu-operator-manifests": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gpu-operator-manifests",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-gpu-operator-by-nvsentinel": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-node-feature-discovery-by-gpu-operator": _teardown_usage(
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The GPU Operator depends on NFD and on the serving stack's cert-manager,
    # and everything else on the GPU Operator, so only NFD, nodewright and the
    # observer of cert-manager are created. Every Usage ordering teardown is
    # composed at once, and none holds cert-manager, which isn't this stack's
    # to delete.
    Case(
        name="ProviderConfigsObserved",
        reason=(
            "Once both ProviderConfigs are observed, an AKSDeviceStack claiming gpu.nvidia.com composes the "
            "components with no dependencies without Delete, observes cert-manager, and composes the Usages "
            "ordering the whole stack's teardown and holding the ProviderConfigs."
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
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "cert-manager",
                                        "modelplane.ai/usage-consumer": "cert-manager",
                                    }
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe"],
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "apps/v1",
                                            "kind": "Deployment",
                                            "metadata": {"name": "cert-manager-webhook", "namespace": "cert-manager"},
                                        }
                                    },
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": (
                                            "has(object.status) && has(object.status.availableReplicas) && "
                                            "object.status.availableReplicas >= 1"
                                        ),
                                    },
                                },
                            }
                        )
                    ),
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
                                            "gc": {"enable": True, "tolerations": []},
                                            "master": {"enable": True, "tolerations": []},
                                            "topologyUpdater": {
                                                "createCRDs": True,
                                                "enable": False,
                                                "kubeletStateDir": "",
                                                "resources": {
                                                    "limits": {"memory": "256Mi"},
                                                    "requests": {"cpu": "50m", "memory": "128Mi"},
                                                },
                                                "tolerations": [
                                                    {
                                                        "effect": "NoSchedule",
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Equal",
                                                        "value": "true",
                                                    }
                                                ],
                                            },
                                            "worker": {
                                                "enable": True,
                                                "tolerations": [
                                                    {
                                                        "effect": "NoSchedule",
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Equal",
                                                        "value": "true",
                                                    }
                                                ],
                                            },
                                        },
                                    },
                                },
                            }
                        )
                    ),
                    "nodewright-operator": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-nodewright"},
                                    "labels": {
                                        "modelplane.ai/resource": "nodewright-operator",
                                        "modelplane.ai/usage-consumer": "nodewright-operator",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "chart": {
                                            "name": "nodewright",
                                            "repository": "oci://ghcr.io/nvidia/nodewright/charts",
                                            "version": "v0.19.0",
                                        },
                                        "namespace": "skyhook",
                                        "values": {
                                            "controllerManager": {
                                                "manager": {
                                                    "resources": {
                                                        "limits": {"cpu": "1000m", "memory": "4000Mi"},
                                                        "requests": {"cpu": "500m", "memory": "2000Mi"},
                                                    }
                                                },
                                                "tolerations": [],
                                            },
                                            "fullnameOverride": "skyhook-operator",
                                            "limitRange": {
                                                "default": {"cpu": "1", "memory": "1Gi"},
                                                "defaultRequest": {"cpu": "500m", "memory": "512Mi"},
                                            },
                                        },
                                    },
                                },
                            }
                        )
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrole": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrole",
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrolebinding": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrolebinding",
                    ),
                    "usage-gpu-operator-by-gpu-operator-manifests": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gpu-operator-manifests",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-gpu-operator-by-nvsentinel": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-node-feature-discovery-by-gpu-operator": _teardown_usage(
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-pc-cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "Object",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "cert-manager"},
                                        },
                                    },
                                    "replayDeletion": True,
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
                    "usage-pc-nodewright-operator": fnv1.Resource(
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
                                            "matchLabels": {"modelplane.ai/usage-consumer": "nodewright-operator"},
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
    # Nothing left in the list depends on cert-manager, so it isn't observed.
    Case(
        name="NoGPUDriver",
        reason="An AKSDeviceStack claiming no DRA driver composes its ProviderConfigs and no components.",
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
    # cert-manager is the serving stack's, so this stack only observes it.
    Case(
        name="AllReady",
        reason=(
            "With every component observed Ready, an AKSDeviceStack composes the whole generated NVIDIA stack, "
            "including the GPU Operator's toolkit-hardening DaemonSet and the DCGM exporter's RBAC, and marks it "
            "ready."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(dra_drivers=[v1alpha1.DraDriver("gpu.nvidia.com")]),
                resources={
                    "provider-config-kubernetes": _observed_provider_config(
                        api_version="kubernetes.m.crossplane.io/v1alpha1"
                    ),
                    "provider-config-helm": _observed_provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                    "cert-manager": _observed_ready(api_version="kubernetes.m.crossplane.io/v1alpha1", kind="Object"),
                    "node-feature-discovery": _observed_ready(
                        api_version="helm.m.crossplane.io/v1beta1", kind="Release"
                    ),
                    "nodewright-operator": _observed_ready(api_version="helm.m.crossplane.io/v1beta1", kind="Release"),
                    "gpu-operator": _observed_ready(api_version="helm.m.crossplane.io/v1beta1", kind="Release"),
                    "dcgm-exporter-clusterrole": _observed_ready(
                        api_version="kubernetes.m.crossplane.io/v1alpha1", kind="Object"
                    ),
                    "dcgm-exporter-clusterrolebinding": _observed_ready(
                        api_version="kubernetes.m.crossplane.io/v1alpha1", kind="Object"
                    ),
                    "gpu-operator-manifests": _observed_ready(
                        api_version="kubernetes.m.crossplane.io/v1alpha1", kind="Object"
                    ),
                    "nvidia-dra-driver-gpu": _observed_ready(
                        api_version="helm.m.crossplane.io/v1beta1", kind="Release"
                    ),
                    "nvsentinel": _observed_ready(api_version="helm.m.crossplane.io/v1beta1", kind="Release"),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "provider-config-kubernetes": _provider_config_kubernetes(ready=fnv1.READY_TRUE),
                    "provider-config-helm": _provider_config_helm(ready=fnv1.READY_TRUE),
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "cert-manager",
                                        "modelplane.ai/usage-consumer": "cert-manager",
                                    }
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe"],
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "apps/v1",
                                            "kind": "Deployment",
                                            "metadata": {"name": "cert-manager-webhook", "namespace": "cert-manager"},
                                        }
                                    },
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": (
                                            "has(object.status) && has(object.status.availableReplicas) && "
                                            "object.status.availableReplicas >= 1"
                                        ),
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
                                    }
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
                                                }
                                            ],
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
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
                                    }
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
                                                }
                                            ],
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
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
                                            "daemonsets": {
                                                "tolerations": [
                                                    {
                                                        "effect": "NoSchedule",
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Equal",
                                                        "value": "true",
                                                    }
                                                ]
                                            },
                                            "dcgm": {"enabled": True},
                                            "dcgmExporter": {
                                                "config": {
                                                    "create": True,
                                                    "data": _DCGM_METRICS_CSV,
                                                    "name": "dcgm-exporter",
                                                },
                                                "serviceMonitor": {
                                                    "enabled": False,
                                                    "honorLabels": True,
                                                    "interval": "30s",
                                                },
                                                "enablePodLabels": True,
                                                "podLabelAllowlistRegex": [
                                                    "^modelplane\\.ai/(deployment|replica|role|engine)$"
                                                ],
                                                "env": [{"name": "KUBERNETES_ENABLE_DRA", "value": "true"}],
                                            },
                                            "devicePlugin": {
                                                "enabled": False,
                                                "env": [
                                                    {"name": "DP_DISABLE_HEALTHCHECKS", "value": "109"},
                                                    {
                                                        "name": "DEVICE_LIST_STRATEGY",
                                                        "value": "cdi-cri,cdi-annotations,volume-mounts",
                                                    },
                                                ],
                                            },
                                            "driver": {
                                                "enabled": False,
                                                "kernelModuleType": "auto",
                                                "maxParallelUpgrades": 5,
                                                "rdma": {"enabled": True, "useHostMofed": False},
                                                "version": "580.173.02",
                                            },
                                            "fullnameOverride": "gpu-operator",
                                            "gdrcopy": {"enabled": False},
                                            "gfd": {"enabled": True},
                                            "hostPaths": {"driverInstallDir": "/run/nvidia/driver"},
                                            "kataSandboxDevicePlugin": {"enabled": False},
                                            "migManager": {"enabled": True},
                                            "nfd": {"enabled": False},
                                            "operator": {
                                                "resources": {
                                                    "limits": {"cpu": "500m", "memory": "700Mi"},
                                                    "requests": {"cpu": "200m", "memory": "300Mi"},
                                                },
                                                "runtimeClass": "nvidia-container-runtime",
                                                "tolerations": [],
                                                "upgradeCRD": True,
                                            },
                                            "toolkit": {
                                                "enabled": False,
                                                "env": [
                                                    {"name": "RUNTIME_CONFIG_SOURCE", "value": "file"},
                                                    {
                                                        "name": "ACCEPT_NVIDIA_VISIBLE_DEVICES_ENVVAR_WHEN_UNPRIVILEGED",
                                                        "value": "false",
                                                    },
                                                    {
                                                        "name": "ACCEPT_NVIDIA_VISIBLE_DEVICES_AS_VOLUME_MOUNTS",
                                                        "value": "true",
                                                    },
                                                ],
                                            },
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
                    "gpu-operator-manifests": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "gpu-operator-manifests",
                                        "modelplane.ai/usage-consumer": "gpu-operator-manifests",
                                    }
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "apps/v1",
                                            "kind": "DaemonSet",
                                            "metadata": {
                                                "name": "nvidia-toolkit-hardening",
                                                "namespace": "gpu-operator",
                                                "labels": {
                                                    "app.kubernetes.io/name": "nvidia-toolkit-hardening",
                                                    "app.kubernetes.io/managed-by": "aicr",
                                                },
                                            },
                                            "spec": {
                                                "selector": {
                                                    "matchLabels": {
                                                        "app.kubernetes.io/name": "nvidia-toolkit-hardening"
                                                    }
                                                },
                                                "updateStrategy": {"type": "RollingUpdate"},
                                                "template": {
                                                    "metadata": {
                                                        "labels": {"app.kubernetes.io/name": "nvidia-toolkit-hardening"}
                                                    },
                                                    "spec": {
                                                        "tolerations": [
                                                            {
                                                                "key": "nvidia.com/gpu",
                                                                "operator": "Exists",
                                                                "effect": "NoSchedule",
                                                            }
                                                        ],
                                                        "affinity": {
                                                            "nodeAffinity": {
                                                                "requiredDuringSchedulingIgnoredDuringExecution": {
                                                                    "nodeSelectorTerms": [
                                                                        {
                                                                            "matchExpressions": [
                                                                                {
                                                                                    "key": "kubernetes.azure.com/accelerator",
                                                                                    "operator": "In",
                                                                                    "values": ["nvidia"],
                                                                                }
                                                                            ]
                                                                        }
                                                                    ]
                                                                }
                                                            }
                                                        },
                                                        "priorityClassName": "system-node-critical",
                                                        "containers": [
                                                            {
                                                                "name": "nvidia-toolkit-hardening",
                                                                "image": "docker.io/library/busybox:1.38.0@sha256:fd7dc98638c8e305f4dc34e979f1c0fdfdcaeb0fbf8fcff77ae834b6da3d7e6e",
                                                                "command": ["/bin/sh", "-c", _TOOLKIT_HARDENING_SCRIPT],
                                                                "readinessProbe": {
                                                                    "exec": {
                                                                        "command": ["test", "-f", "/tmp/hardened"]
                                                                    },
                                                                    "initialDelaySeconds": 2,
                                                                    "periodSeconds": 10,
                                                                },
                                                                "securityContext": {"privileged": True},
                                                                "resources": {
                                                                    "requests": {"cpu": "10m", "memory": "16Mi"},
                                                                    "limits": {"cpu": "50m", "memory": "32Mi"},
                                                                },
                                                                "volumeMounts": [
                                                                    {"name": "host-root", "mountPath": "/host"}
                                                                ],
                                                            }
                                                        ],
                                                        "volumes": [
                                                            {
                                                                "name": "host-root",
                                                                "hostPath": {"path": "/", "type": "Directory"},
                                                            }
                                                        ],
                                                    },
                                                },
                                            },
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
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
                                            "gc": {"enable": True, "tolerations": []},
                                            "master": {"enable": True, "tolerations": []},
                                            "topologyUpdater": {
                                                "createCRDs": True,
                                                "enable": False,
                                                "kubeletStateDir": "",
                                                "resources": {
                                                    "limits": {"memory": "256Mi"},
                                                    "requests": {"cpu": "50m", "memory": "128Mi"},
                                                },
                                                "tolerations": [
                                                    {
                                                        "effect": "NoSchedule",
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Equal",
                                                        "value": "true",
                                                    }
                                                ],
                                            },
                                            "worker": {
                                                "enable": True,
                                                "tolerations": [
                                                    {
                                                        "effect": "NoSchedule",
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Equal",
                                                        "value": "true",
                                                    }
                                                ],
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "nodewright-operator": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-nodewright"},
                                    "labels": {
                                        "modelplane.ai/resource": "nodewright-operator",
                                        "modelplane.ai/usage-consumer": "nodewright-operator",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "chart": {
                                            "name": "nodewright",
                                            "repository": "oci://ghcr.io/nvidia/nodewright/charts",
                                            "version": "v0.19.0",
                                        },
                                        "namespace": "skyhook",
                                        "values": {
                                            "controllerManager": {
                                                "manager": {
                                                    "resources": {
                                                        "limits": {"cpu": "1000m", "memory": "4000Mi"},
                                                        "requests": {"cpu": "500m", "memory": "2000Mi"},
                                                    }
                                                },
                                                "tolerations": [],
                                            },
                                            "fullnameOverride": "skyhook-operator",
                                            "limitRange": {
                                                "default": {"cpu": "1", "memory": "1Gi"},
                                                "defaultRequest": {"cpu": "500m", "memory": "512Mi"},
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
                                            "controller": {
                                                "affinity": {},
                                                "podAnnotations": {"aicr.run/gpu-operator-chart-version": "v26.7.1"},
                                                "priorityClassName": "",
                                                "tolerations": [],
                                            },
                                            "fullnameOverride": "nvidia-dra-driver-gpu",
                                            "gpuResourcesEnabledOverride": True,
                                            "kubeletPlugin": {
                                                "podAnnotations": {"aicr.run/gpu-operator-chart-version": "v26.7.1"},
                                                "priorityClassName": "",
                                                "tolerations": [
                                                    {
                                                        "effect": "NoSchedule",
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Equal",
                                                        "value": "true",
                                                    }
                                                ],
                                            },
                                            "nameOverride": "nvidia-dra-driver-gpu",
                                            "nvidiaDriverRoot": "/",
                                            "resources": {
                                                "computeDomains": {"enabled": False},
                                                "gpus": {"enabled": True},
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "nvsentinel": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-nvsentinel"},
                                    "labels": {
                                        "modelplane.ai/resource": "nvsentinel",
                                        "modelplane.ai/usage-consumer": "nvsentinel",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "prod-cluster-0fab1"},
                                    "managementPolicies": ["Observe", "Create", "Update"],
                                    "forProvider": {
                                        "chart": {
                                            "name": "nvsentinel",
                                            "repository": "oci://ghcr.io/nvidia",
                                            "version": "v1.25.0",
                                        },
                                        "namespace": "nvsentinel",
                                        "values": {
                                            "fullnameOverride": "nvsentinel",
                                            "global": {
                                                "initContainerImage": {
                                                    "repository": "docker.io/library/busybox",
                                                    "tag": "1.38.0@sha256:fd7dc98638c8e305f4dc34e979f1c0fdfdcaeb0fbf8fcff77ae834b6da3d7e6e",
                                                },
                                                "nicHealthMonitor": {"enabled": True},
                                                "systemNodeTolerations": [],
                                                "tolerations": [
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Exists",
                                                        "effect": "NoSchedule",
                                                    }
                                                ],
                                            },
                                            "janitor-provider": {"csp": {"provider": "generic"}},
                                            "labeler": {"assumeDriverInstalled": True},
                                            "metadata-collector": {"runtimeClassName": "nvidia-container-runtime"},
                                            "networkPolicy": {"enabled": False},
                                            "nic-health-monitor": {"processingStrategy": "STORE_ONLY"},
                                            "platformConnector": {
                                                "maxUnavailable": 1,
                                                "resources": {
                                                    "limits": {"cpu": "200m", "memory": "512Mi"},
                                                    "requests": {"cpu": "200m", "memory": "512Mi"},
                                                },
                                                "updateStrategy": "RollingUpdate",
                                            },
                                            "podMonitor": {"enabled": False},
                                            "syslog-health-monitor": {
                                                "enabledChecks": [
                                                    "SysLogsXIDError",
                                                    "SysLogsSXIDError",
                                                    "SysLogsGPUFallenOff",
                                                    "SysLogsNICDriverError",
                                                ],
                                                "nicDriverDetection": {
                                                    "patterns": [
                                                        {
                                                            "enabled": True,
                                                            "name": "cmd_exec_timeout",
                                                            "processingStrategy": "STORE_ONLY",
                                                        },
                                                        {
                                                            "enabled": True,
                                                            "name": "health_poll_failed",
                                                            "processingStrategy": "STORE_ONLY",
                                                        },
                                                        {
                                                            "enabled": True,
                                                            "name": "unrecoverable_err",
                                                            "processingStrategy": "STORE_ONLY",
                                                        },
                                                        {
                                                            "enabled": True,
                                                            "name": "netdev_watchdog",
                                                            "processingStrategy": "STORE_ONLY",
                                                        },
                                                        {
                                                            "enabled": True,
                                                            "name": "pci_power_insufficient",
                                                            "processingStrategy": "STORE_ONLY",
                                                        },
                                                        {
                                                            "enabled": True,
                                                            "name": "port_module_high_temp",
                                                            "processingStrategy": "STORE_ONLY",
                                                        },
                                                        {
                                                            "enabled": True,
                                                            "name": "access_reg_failed",
                                                            "processingStrategy": "STORE_ONLY",
                                                        },
                                                        {
                                                            "enabled": True,
                                                            "name": "module_unplugged",
                                                            "processingStrategy": "STORE_ONLY",
                                                        },
                                                        {
                                                            "enabled": True,
                                                            "name": "mlx5_tx_timeout_detected",
                                                            "processingStrategy": "STORE_ONLY",
                                                        },
                                                        {
                                                            "enabled": True,
                                                            "name": "mlx5_rx_timeout_detected",
                                                            "processingStrategy": "STORE_ONLY",
                                                        },
                                                        {
                                                            "enabled": True,
                                                            "name": "mlx5_napi_soft_lockup",
                                                            "processingStrategy": "STORE_ONLY",
                                                        },
                                                    ]
                                                },
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrole": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrole",
                    ),
                    "usage-gpu-operator-by-dcgm-exporter-clusterrolebinding": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="dcgm-exporter-clusterrolebinding",
                    ),
                    "usage-gpu-operator-by-gpu-operator-manifests": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gpu-operator-manifests",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-gpu-operator-by-nvsentinel": _teardown_usage(
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-node-feature-discovery-by-gpu-operator": _teardown_usage(
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-pc-cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "Object",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "cert-manager"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-dcgm-exporter-clusterrole": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "Object",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {
                                                "modelplane.ai/usage-consumer": "dcgm-exporter-clusterrole"
                                            },
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-dcgm-exporter-clusterrolebinding": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "Object",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {
                                                "modelplane.ai/usage-consumer": "dcgm-exporter-clusterrolebinding"
                                            },
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-gpu-operator": fnv1.Resource(
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
                                            "matchLabels": {"modelplane.ai/usage-consumer": "gpu-operator"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-gpu-operator-manifests": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "prod-cluster-0fab1"},
                                    },
                                    "by": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "Object",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "gpu-operator-manifests"},
                                        },
                                    },
                                    "replayDeletion": True,
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
                    "usage-pc-nodewright-operator": fnv1.Resource(
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
                                            "matchLabels": {"modelplane.ai/usage-consumer": "nodewright-operator"},
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
                    "usage-pc-nvsentinel": fnv1.Resource(
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
                                            "matchLabels": {"modelplane.ai/usage-consumer": "nvsentinel"},
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
    """RunFunction installs the device stacks an AKS cluster's InferenceClasses claim."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
