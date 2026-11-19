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

"""The legs of the device stack e2e matrix. See e2e/README.md.

Each leg shapes fake, KWOK-managed Nodes the way a cluster provider (or a
BYO cluster's operator) shapes real ones - labels and taint - and names
the DaemonSets the device stacks must fan out to them. A leg carries one
pool per device stack its classes would claim: an NVIDIA GPU pool and a
CPU pool on every leg, plus an AMD GPU pool on Existing, the one leg
whose serving stack installs the AMD stack.

There are two kinds of leg. The Existing legs apply a ServingStack that
installs the device stacks itself (spec.deviceStacks: Managed). A
provider's leg applies a ServingStack that leaves them to the provider
(External) and the provider's own device stack XR beside it, which
depends on the ServingStack's cert-manager. Its data is the provider's
conformance.yaml, so the provider owns what its pools look like and what
must land on them, and the suite stays generic.

Each leg names the drivers its classes claim, the way
compose-inference-cluster stamps them, so the legs exercise the driver
filter: each device stack must reach its own pool and stay off the
others'. The existing-filtered leg keeps the AMD pool but leaves
gpu.amd.com unclaimed, so a join that ignored the filter fails its
absence check rather than passing every leg.
"""

import dataclasses
import os
import pathlib
import typing

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

# Where bring-up applies the ServingStack and device stack under test, on the
# control plane. They're named apart, as an InferenceCluster's are, because
# each composes ProviderConfigs named for it in the same namespace.
SERVING_STACK = "e2e-cloud"
DEVICE_STACK = "e2e-devices"
NAMESPACE = "modelplane-system"

# Two fake nodes per pool, so per-node DaemonSet fan-out is visible.
FAKE_NODES = ("kwok-gpu-0", "kwok-gpu-1")
FAKE_CPU_NODES = ("kwok-cpu-0", "kwok-cpu-1")
FAKE_AMD_NODES = ("kwok-amd-0", "kwok-amd-1")

# The taints the non-NVIDIA pools carry, fixed across clouds: every
# cluster composition taints CPU pools modelplane.ai/cpu=true, and
# amd.com/gpu=true is the convention BYO AMD pools carry (Modelplane
# doesn't provision them; the AMD stack's operands tolerate exactly it).
CPU_TAINT = {"key": "modelplane.ai/cpu", "value": "true", "effect": "NoSchedule"}
AMD_TAINT = {"key": "amd.com/gpu", "value": "true", "effect": "NoSchedule"}


@dataclasses.dataclass(frozen=True)
class Cloud:
    """One leg: its fake device pools' shapes, and what must land on them."""

    cloud: str
    """The leg's name: Existing, or the provider's directory under providers/."""

    labels: dict[str, str]
    """The cloud's own NVIDIA GPU node labels, stamped on the fake GPU nodes.

    What the cluster composition (or the cloud's platform) puts on a real GPU
    node, plus the NFD labels NFD's worker would set - the GPU presence labels
    (gpu.present, and pci-10de.present or Nebius's pci-0302_10de.present) and
    the OS release labels (system-os_release.ID, system-os_release.VERSION_ID)
    the GPU operator's ClusterPolicy controller reads from GPU nodes since
    v26.7 - pre-set because the worker's pod on a KWOK node is fake and never
    labels anything.
    """

    taint_value: str
    """The value of the nvidia.com/gpu NoSchedule taint the cloud's GPU pools carry."""

    cpu_labels: dict[str, str]
    """The CPU pool's node labels, stamped on the fake CPU nodes.

    What the cluster composition puts on a real CPU node: the pool label and
    modelplane.ai/cpu, which is both what keeps system workloads traceable
    and the selector dra-driver-cpu finds its nodes by, plus the cloud's
    instance-type label for fidelity.
    """

    dra_drivers: tuple[str, ...]
    """The ServingStack's spec.draDrivers: the DRA drivers this leg's pools claim.

    What compose-inference-cluster would collect from the leg's classes; the
    join filters the stack's tagged device stacks to exactly these.
    """

    expected_daemonsets: dict[str, int]
    """The DaemonSets that must fan out to the device pools: namespace/name to ready count.

    The node-reaching DaemonSets from the stack, by the names that cloud's
    charts give them, counted across every node their tolerations and
    selectors reach: the fake pools they serve plus the real node where they
    run everywhere.
    """

    gpu_workloads: tuple[str, ...] = ()
    """Pod-name patterns allowed on the GPU pool beyond the common GPU_WORKLOADS, such as AKS's toolkit hardening."""

    amd_labels: dict[str, str] | None = None
    """The AMD GPU pool's node labels, when the leg has one (Existing only).

    The pool label plus the NFD label (feature.node.kubernetes.io/amd-gpu)
    the AMD stack's operands select their nodes by - pre-set like the NVIDIA
    NFD labels, because nothing real runs on a KWOK node.
    """

    directory: pathlib.Path | None = None
    """The provider's directory, holding its project and conformance.yaml. None on the Existing legs."""

    device_stack: tuple[str, str, str] | None = None
    """The provider's device stack XR, as (apiVersion, kind, plural). None on the Existing legs."""

    provisioning: dict[str, typing.Any] | None = None
    """The provider's cluster config and node classes, for the provisioning test."""

    absent_daemonsets: tuple[str, ...] = ()
    """DaemonSets that must not exist at all: namespace/name of an unclaimed stack's operands.

    The absence side of the driver filter. The pool is on the cluster and
    the cloud's list carries the stack; only spec.draDrivers leaving the
    driver out keeps these off, so a join that ignored the filter fails
    here rather than passing a leg that claims everything.
    """


def _provider(directory: pathlib.Path) -> Cloud:
    """The leg of the provider in directory, read from its conformance.yaml."""
    leg = yaml.safe_load((directory / "conformance.yaml").read_text())
    # The device stack's plural, from the provider's XRD for it.
    plural = next(
        xrd["spec"]["names"]["plural"]
        for xrd in (yaml.safe_load(p.read_text()) for p in sorted(directory.glob("apis/*/definition.yaml")))
        if xrd["spec"]["names"]["kind"] == leg["deviceStack"]["kind"]
    )
    return Cloud(
        cloud=directory.name,
        directory=directory,
        labels=leg["pools"]["gpu"]["labels"],
        taint_value=leg["pools"]["gpu"]["taintValue"],
        cpu_labels=leg["pools"]["cpu"]["labels"],
        dra_drivers=tuple(leg["draDrivers"]),
        expected_daemonsets=leg["daemonSets"],
        gpu_workloads=tuple(leg.get("gpuWorkloads", [])),
        device_stack=(leg["deviceStack"]["apiVersion"], leg["deviceStack"]["kind"], plural),
        provisioning=leg["provisioning"],
    )


# The providers under providers/, and any others the
# MODELPLANE_CONFORMANCE_PROVIDERS environment variable lists by directory,
# separated by the platform's path separator: a provider in a repository of
# its own runs the suite against itself this way.
_DIRECTORIES = {p.parent.name: p.parent for p in (ROOT / "providers").glob("*/conformance.yaml")} | {
    pathlib.Path(d).resolve().name: pathlib.Path(d).resolve()
    for d in os.environ.get("MODELPLANE_CONFORMANCE_PROVIDERS", "").split(os.pathsep)
    if d
}
PROVIDERS = sorted(_DIRECTORIES)

CLOUDS: dict[str, Cloud] = {
    # The model e2e already installs the Existing stack, but on an untainted
    # single node - this leg adds what it can't check: tainted GPU pools, so
    # the placement and toleration audits have teeth. Modelplane doesn't
    # provision or label BYO pools; the operator does, so the labels here are
    # the convention the stack relies on, and the taint is the nvidia.com/gpu
    # one BYO GPU pools commonly carry (the stack tolerates it with
    # operator: Exists).
    "existing": Cloud(
        cloud="Existing",
        labels={
            "modelplane.ai/pool": "gpu-fake",
            "nvidia.com/gpu.present": "true",
            "feature.node.kubernetes.io/pci-10de.present": "true",
            # No convention for BYO node OS; Ubuntu 22.04 is a common one.
            "feature.node.kubernetes.io/system-os_release.ID": "ubuntu",
            "feature.node.kubernetes.io/system-os_release.VERSION_ID": "22.04",
        },
        taint_value="true",
        cpu_labels={
            "modelplane.ai/pool": "cpu-fake",
            "modelplane.ai/cpu": "true",
        },
        # Existing is the one cloud that installs the AMD stack, so its leg
        # is the one with an AMD pool. The operands are named for the AMD
        # chart's default DeviceConfig.
        amd_labels={
            "modelplane.ai/pool": "amd-fake",
            "feature.node.kubernetes.io/amd-gpu": "true",
        },
        dra_drivers=("dra.cpu", "gpu.amd.com", "gpu.nvidia.com"),
        expected_daemonsets={
            "nvidia-dra-driver/dra-driver-nvidia-gpu-kubelet-plugin": 2,
            "dra-driver-cpu/mp-dra-driver-cpu": 2,
            "kube-amd-gpu/default-dra-driver": 2,
            "kube-amd-gpu/default-node-labeller": 2,
            "kube-amd-gpu/default-metrics-exporter": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 5,
        },
    ),
    # The existing leg with gpu.amd.com left unclaimed: same pools, AMD
    # fake nodes included, but spec.draDrivers names only the NVIDIA and
    # CPU stacks. Every other leg claims everything its cloud carries, so
    # only this one fails a join that ignored the filter: the AMD operands
    # must not exist even though their pool is there and Existing's list
    # pins them. NFD still reaches the AMD nodes - it installs for the
    # NVIDIA claim and labels both vendors' nodes - so its count matches
    # the existing leg's.
    "existing-filtered": Cloud(
        cloud="Existing",
        labels={
            "modelplane.ai/pool": "gpu-fake",
            "nvidia.com/gpu.present": "true",
            "feature.node.kubernetes.io/pci-10de.present": "true",
            # No convention for BYO node OS; Ubuntu 22.04 is a common one.
            "feature.node.kubernetes.io/system-os_release.ID": "ubuntu",
            "feature.node.kubernetes.io/system-os_release.VERSION_ID": "22.04",
        },
        taint_value="true",
        cpu_labels={
            "modelplane.ai/pool": "cpu-fake",
            "modelplane.ai/cpu": "true",
        },
        amd_labels={
            "modelplane.ai/pool": "amd-fake",
            "feature.node.kubernetes.io/amd-gpu": "true",
        },
        dra_drivers=("dra.cpu", "gpu.nvidia.com"),
        expected_daemonsets={
            "nvidia-dra-driver/dra-driver-nvidia-gpu-kubelet-plugin": 2,
            "dra-driver-cpu/mp-dra-driver-cpu": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 5,
        },
        absent_daemonsets=(
            "kube-amd-gpu/default-dra-driver",
            "kube-amd-gpu/default-node-labeller",
            "kube-amd-gpu/default-metrics-exporter",
        ),
    ),
}
CLOUDS |= {name: _provider(_DIRECTORIES[name]) for name in PROVIDERS}


def _node(name: str, labels: dict[str, str], taint: dict[str, str]) -> dict[str, typing.Any]:
    """Return one fake Node: a pool's labels, its taint, and KWOK's marks.

    The kwok.x-k8s.io/node annotation hands the Node to KWOK, which keeps it
    Ready and marks every pod scheduled onto it Running. No extended
    resources: the stack is DRA-mode, with every device plugin disabled.
    """
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {
            "name": name,
            "annotations": {"kwok.x-k8s.io/node": "fake", "node.alpha.kubernetes.io/ttl": "0"},
            # type: kwok is how bring-up and the tests find the fake
            # nodes; every cloud's inference nodes are linux/amd64.
            "labels": {
                "type": "kwok",
                "kubernetes.io/os": "linux",
                "kubernetes.io/arch": "amd64",
                **labels,
            },
        },
        "spec": {"taints": [taint]},
    }


def nodes(cloud: Cloud) -> list[dict[str, typing.Any]]:
    """Return the fake Nodes of a cloud's leg, for the workload cluster.

    One tainted pool per device stack the leg claims: the NVIDIA GPU pool,
    the CPU pool, and - where the leg declares one - an AMD GPU pool.
    """
    gpu_taint = {"key": "nvidia.com/gpu", "value": cloud.taint_value, "effect": "NoSchedule"}
    fake = [_node(name, cloud.labels, gpu_taint) for name in FAKE_NODES]
    fake += [_node(name, cloud.cpu_labels, CPU_TAINT) for name in FAKE_CPU_NODES]
    if cloud.amd_labels is not None:
        fake += [_node(name, cloud.amd_labels, AMD_TAINT) for name in FAKE_AMD_NODES]
    return fake


def serving_stack(cloud: Cloud) -> dict[str, typing.Any]:
    """Return the ServingStack under test, for the control plane.

    Applied directly rather than composed by an InferenceCluster, which would
    need a cluster to register or provision. The kubeconfig Secret is the one
    bring-up registers for the model e2e, so the stack installs onto the
    workload kind cluster, whose KWOK nodes are shaped like the leg's pools.
    On a provider's leg it leaves the device stacks to the provider's device
    stack XR. The gateway hostname mirrors the shape compose-inference-cluster
    projects; with no clientCAs the gateway serves nothing, but its readiness
    needs only the LoadBalancer address MetalLB hands out.
    """
    return {
        "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
        "kind": "ServingStack",
        "metadata": {"name": SERVING_STACK, "namespace": NAMESPACE},
        "spec": {
            "deviceStacks": "Managed" if cloud.device_stack is None else "External",
            "stack": "Standard",
            # What compose-inference-cluster would stamp from the leg's
            # classes: the join filters the stack's tagged device stacks to
            # exactly these, so each pool gets its stack and no other's.
            "draDrivers": list(cloud.dra_drivers),
            "gateway": {"hostname": f"gateway-{SERVING_STACK}.{NAMESPACE}.svc.cluster.local"},
            "secrets": [KUBECONFIG],
        },
    }


# The workload cluster's kubeconfig, which bring-up registers on the control
# plane.
KUBECONFIG = {"type": "Kubeconfig", "name": "local-cluster-kubeconfig", "key": "kubeconfig"}


def device_stack(cloud: Cloud) -> dict[str, typing.Any]:
    """Return a provider's device stack XR under test, for the control plane.

    What the provider's ProvisionedCluster composes once its cluster is
    reachable, applied directly against the workload kind cluster, so the
    device stack installs there beside the ServingStack whose cert-manager it
    depends on.
    """
    if cloud.device_stack is None:
        raise ValueError(f"the {cloud.cloud} leg has no device stack")
    api_version, kind, _ = cloud.device_stack
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {"name": DEVICE_STACK, "namespace": NAMESPACE},
        "spec": {"draDrivers": list(cloud.dra_drivers), "secrets": [KUBECONFIG]},
    }


# The InferenceClasses and InferenceCluster the provisioning test applies with
# a provider's cluster config and node classes.
PROVISIONED = "conformance"


def provisioned(cloud: Cloud) -> list[dict[str, typing.Any]]:
    """Return the objects of a provider's provisioning test, for the control plane.

    Anything the provider's leg needs in place first, such as a cloud
    ProviderConfig its cluster composition reads, then the provider's cluster
    config and node classes, an InferenceClass of each
    role, and an InferenceCluster with a pool of each, restricted to the
    zones the provider's leg names, for a provider whose pools need them. The
    GPU class claims an NVIDIA GPU, which every provider supports today.
    """
    if cloud.provisioning is None:
        raise ValueError(f"the {cloud.cloud} leg has no provisioning test")
    config = cloud.provisioning["config"]
    classes = cloud.provisioning["nodeClasses"]
    zones = {"zones": cloud.provisioning["zones"]} if "zones" in cloud.provisioning else {}

    def ref(obj: dict[str, typing.Any]) -> dict[str, str]:
        return {"apiVersion": obj["apiVersion"], "kind": obj["kind"], "name": obj["metadata"]["name"]}

    def inference_class(name: str, node_class: dict[str, typing.Any], device: dict[str, typing.Any]) -> dict:
        return {
            "apiVersion": "modelplane.ai/v1alpha1",
            "kind": "InferenceClass",
            "metadata": {"name": name},
            "spec": {"nodeClassRef": ref(node_class), "devices": [device]},
        }

    return [
        *cloud.provisioning.get("prerequisites", []),
        config,
        *classes.values(),
        inference_class(
            f"{PROVISIONED}-gpu",
            classes["accelerator"],
            {"name": "gpu", "driver": "gpu.nvidia.com", "deviceClassName": "gpu.nvidia.com", "count": 1},
        ),
        inference_class(
            f"{PROVISIONED}-cpu",
            classes["cpu"],
            {"name": "cpu", "driver": "dra.cpu", "deviceClassName": "dra.cpu", "count": 1},
        ),
        {
            "apiVersion": "modelplane.ai/v1alpha1",
            "kind": "InferenceCluster",
            "metadata": {"name": PROVISIONED},
            "spec": {
                "cluster": {"source": "Provisioned", "provisioned": {"configRef": ref(config)}},
                "nodePools": [
                    {"name": "gpu", "className": f"{PROVISIONED}-gpu", "nodeCount": 1, **zones},
                    {"name": "cpu", "className": f"{PROVISIONED}-cpu", "nodeCount": 1, **zones},
                ],
            },
        },
    ]
