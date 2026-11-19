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

"""The classification tables every AICR-based device stack shares.

What Modelplane keeps, drops, scopes and forces in AICR's catalog
whatever the cloud. A provider adds its own entries through its Cloud;
see modelplane_sdk.aicr.cloud.
"""

from typing import Any

from modelplane_sdk import stacks

Values = dict[str, Any]
ValuePath = tuple[str, ...]

# The aicr release these tables are synced against: check_pin() refuses
# any other, because the ALLOW/DROP tables classify that release's
# catalog, and so does every provider's Cloud, which must name the same
# release. Moves together with `version` in nix/aicr.nix; see
# CONTRIBUTING.md, "Bumping aicr".
AICR_PIN = "1.0.0"

# The registry of the catalog a Cloud's overlays are written into, for
# `aicr recipe --data`.
REGISTRY_YAML = """\
# Required by aicr's layered data provider: an external --data directory
# must carry a registry.yaml, merged with the embedded one by component
# name. Modelplane contributes no components - the catalog exists only so
# its overlays can replace the embedded files of the same name.
apiVersion: aicr.run/v1beta1
kind: ComponentRegistry
components: []
"""

# The bundler stamps a wildcard toleration - a bare {operator: Exists},
# tolerating every taint - onto every pod it renders, Deployments
# included. On a Modelplane cluster that puts control-plane pods on GPU
# nodes: the providers taint GPU pools (nvidia.com/gpu) exactly so that
# only GPU workloads land there, and a tolerate-everything pod squats on
# accelerated capacity and stalls autoscaler scale-down. So the
# transform rewrites every wildcard the bundle ships, per the table
# below: node-scoped workloads that must reach GPU nodes tolerate
# exactly the GPU taint (GPU, matching the hand-written device stacks'
# NFD worker), and everything else tolerates nothing (SYSTEM) and
# schedules on the untainted system pool. A toleration the bundle
# already scoped (a Cloud's --accelerated-node-toleration bundle flag)
# passes through untouched. The wildcard doesn't only arrive under keys
# named tolerations: nvsentinel's bundle puts one under
# global.systemNodeTolerations, so the walkers match any *Tolerations
# key too.
WILDCARD_TOLERATION = [{"operator": "Exists"}]

# Tolerate nothing: schedule on the untainted system pool only.
SYSTEM: list[Values] = []

# Tolerate exactly the GPU taint, whatever value a provider gives it.
GPU: list[Values] = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]

# Recipe component name -> dotted values path -> the tolerations that
# replace a wildcard found there. Like ALLOW/DROP this fails closed: a
# wildcard at an unclassified path fails the run, else an aicr bump
# could put a new control-plane pod back on GPU nodes. Shared, because
# where a component's pods belong is the component's, not the cloud's.
TOLERATIONS = {
    "nfd": {
        # The worker and topology updater must label GPU nodes - the
        # DRA kubelet plugin schedules on a label the worker applies.
        "worker.tolerations": GPU,
        "topologyUpdater.tolerations": GPU,
        "master.tolerations": SYSTEM,
        "gc.tolerations": SYSTEM,
    },
    "gpu-operator": {
        "daemonsets.tolerations": GPU,
        "operator.tolerations": SYSTEM,
    },
    "nvidia-dra-driver-gpu": {
        "kubeletPlugin.tolerations": GPU,
        "controller.tolerations": SYSTEM,
    },
    "nvsentinel": {
        # The chart splits its global knob in two: tolerations goes to
        # the node health monitors (DaemonSets that must reach GPU
        # nodes), systemNodeTolerations to its control plane (the
        # labeler Deployment), which belongs on the system pool.
        "global.tolerations": GPU,
        "global.systemNodeTolerations": SYSTEM,
    },
    "nodewright-operator": {
        "controllerManager.tolerations": SYSTEM,
    },
}

# Recipe component name -> (the key we emit it as, the DRA drivers
# whose device stacks it belongs to). The key and the mp-prefixed
# release name are Modelplane's, not AICR's, so a component's identity
# survives upstream renames (see the design's "Ordering and identity").
# The driver list fails closed like the allowlist itself: classifying a
# new component includes deciding which stacks it serves, the review a
# bump already requires - the tag just records it. What the tag means
# for installs lives on Component.dra_drivers and filter_drivers() in
# modelplane_sdk.stacks (and in design/multi-accelerator.md). Shared:
# these are the NVIDIA device stack on every cloud.
ALLOW = {
    "nfd": ("node-feature-discovery", ["gpu.nvidia.com"]),
    "gpu-operator": ("gpu-operator", ["gpu.nvidia.com"]),
    "nvidia-dra-driver-gpu": ("nvidia-dra-driver-gpu", ["gpu.nvidia.com"]),
    "nvsentinel": ("nvsentinel", ["gpu.nvidia.com"]),
    "nodewright-operator": ("nodewright-operator", ["gpu.nvidia.com"]),
    "nodewright-customizations": ("nodewright-customizations", ["gpu.nvidia.com"]),
}

# Recipe components the serving stack installs on every cluster:
# dropped from the device stack like DROP, but a dependent keeps its
# edge, onto the key of the Observed entry that stands in for it. A
# provider joins that entry into its list, so the edge resolves and the
# dependent's install waits on it. Recipe component name -> (key,
# reason).
OBSERVED = {
    "cert-manager": (stacks.CERT_MANAGER.key, "the serving stack installs it on every cluster"),
}

# In the recipe, deliberately not in the stack. Each entry needs a
# reason; an unclassified component fails the run. The allowlist fails
# closed. Shared: Modelplane drops these whatever the cloud. A Cloud
# drops the components only its recipes carry.
DROP = {
    "agentgateway": "Modelplane routes through an InferencePool behind Envoy AI Gateway",
    "agentgateway-crds": "Modelplane routes through an InferencePool behind Envoy AI Gateway",
    "dra-node-labeler": (
        "serves the opt-in DRA driver-upgrade eviction label "
        "(--dra-eviction-node-label), which Modelplane doesn't opt into; the "
        "bundler drops it from the bundle without the flag"
    ),
    "kube-prometheus-stack": (
        "the telemetry collector scrapes the engines, the gateways and the GPU "
        "exporter and exports to TelemetryDestinations (design/telemetry.md), so "
        "nothing in Modelplane reads an in-cluster Prometheus - and its PVC "
        "outlives the cluster. This takes with it the only kube-state-metrics "
        "any cloud installs; nothing scrapes it today, so no series is lost, but "
        "the substrate metrics design/telemetry.md plans on it "
        "(modelplane_gang_incomplete, modelplane_engine_restarts_total) will need "
        "it reinstated as its own component, the way the collector scrapes the "
        "other exporters (#551)"
    ),
    "prometheus-adapter": (
        "serves Prometheus series as custom metrics for in-cluster autoscaling, "
        "which Modelplane doesn't run; its source, kube-prometheus-stack, is "
        "dropped above (#551)"
    ),
    "prometheus-operator-crds": (
        "with every monitoring.coreos.com object in the stack off - DCGM's "
        "ServiceMonitor and nvsentinel's PodMonitor, see MANAGED - nothing "
        "renders against these CRDs (#551)"
    ),
    "k8s-ephemeral-storage-metrics": (
        "only its ServiceMonitor exposed it, to the Prometheus dropped above; "
        "no collector job scrapes it, so nothing would read the exporter. If "
        "its series are ever wanted, reinstate it with prometheus.io scrape "
        "annotations (the collector's substrate job discovers those) and a "
        "MetricMapping (#551)"
    ),
    "kai-scheduler": (
        "contract surface, not hardware surface; Modelplane's pin beside its Queues, in the serving stack's dynamo.py"
    ),
}

# Long recipe-carried config blobs keep their content, not their
# cosmetics: dcgm-exporter's metrics CSV ships with blank spacer lines
# the exporter ignores. Trimming them keeps the generated file
# readable. Shared, like the CSV (see modelplane_sdk.stacks.dcgm).
TRIM = {
    "gpu-operator": [("dcgmExporter", "config", "data")],
}

# Values Modelplane must own regardless of what a recipe says,
# mirroring the design's modelplane-*-inference overlay: DRA whole-GPU
# allocation over the device plugin (AICR rejects a partial flip), and
# the driver from the node image, which is Modelplane's mode on every
# cloud today ("The GPU driver" leans toward conforming to AICR's
# operator-installed default on one cloud eventually; that lands in its
# Cloud as a value change, not an API change). Turning the operator's
# driver off forces nvidiaDriverRoot to name where the node image put
# it, and forces NVSentinel's labeler to assume a driver it has no pod
# to observe - AICR's bundler refuses to render without both. Shared;
# a Cloud overrides an entry's value where its cloud legitimately
# differs, and adds paths only it manages.
#
# This table is both applied and asserted: managed_sets() turns it
# into `aicr bundle --set` flags, and the transform verifies each
# value landed in the hydrated bundle. The assertion catches aicr
# silently ignoring a --set for a mistyped component key.
MANAGED: dict[str, list[tuple[ValuePath, object, str]]] = {
    "nvidia-dra-driver-gpu": [
        (("gpuResourcesEnabledOverride",), True, "Modelplane allocates GPUs via DRA ResourceSlices"),
        (("resources", "gpus", "enabled"), True, "Modelplane allocates GPUs via DRA ResourceSlices"),
        (("resources", "computeDomains", "enabled"), False, "multi-node NVLink unused"),
        (("nvidiaDriverRoot",), "/", "the node image puts the driver at the default root"),
    ],
    "gpu-operator": [
        (("driver", "enabled"), False, "the node image provides the kernel driver"),
        (("toolkit", "enabled"), False, "the node image provides the container toolkit"),
        (("devicePlugin", "enabled"), False, "the device plugin and DRA ResourceSlices double-advertise GPUs"),
        (("gdrcopy", "enabled"), False, "no operator-managed driver to build the kernel module against"),
        # Rendering it would fail the install - prometheus-operator-crds
        # is dropped, so the ServiceMonitor has no CRD - and nothing
        # would read it anyway: the collector's modelplane-gpu job
        # scrapes the exporter pods directly, as on the hand-written
        # device stacks.
        (
            ("dcgmExporter", "serviceMonitor", "enabled"),
            False,
            "its CRD is dropped, and the collector scrapes the exporter pods",
        ),
    ],
    "nvsentinel": [
        (("labeler", "assumeDriverInstalled"), True, "no driver pod to observe with the operator's driver off"),
        # Its one monitoring.coreos.com object (the mongodb and
        # postgresql subcharts' monitors are off by default), rendered
        # against the dropped prometheus-operator-crds. Its health
        # series are remediation-internal; a user who wants them exports
        # them through the collector and a MetricMapping.
        (("podMonitor", "enabled"), False, "its CRD is dropped, and no Prometheus would read it"),
    ],
    "nodewright-operator": [
        (
            ("controllerManager", "manager", "resources", "requests", "cpu"),
            "500m",
            "the chart's default; two replicas at the recipe's 1000m leave no room on the system pool",
        ),
    ],
}

# Values Modelplane adds that AICR's recipes carry no opinion on,
# deep-set into the hydrated bundle after the merge. Unlike MANAGED
# these aren't --set (a structured value can't ride a --set flag) and
# need no assertion - the generator writes them itself. They keep the
# monitoring shape identical on every cloud: the GPU series carry the
# same identity labels on a generated cloud as on a hand-written one,
# which is why they're shared.
MODELPLANE_VALUES: dict[str, list[tuple[ValuePath, object, str]]] = {
    # The device plugin is off (see MANAGED), so the exporter maps
    # pods only in DRA mode; the allowlist limits label cardinality.
    "gpu-operator": [
        (
            ("dcgmExporter", "enablePodLabels"),
            True,
            "attach the GPU-holding pod's labels to each GPU series",
        ),
        (
            ("dcgmExporter", "podLabelAllowlistRegex"),
            [r"^modelplane\.ai/(deployment|replica|role|engine)$"],
            "only Modelplane's identity labels, not the pod's every label",
        ),
        (
            ("dcgmExporter", "env"),
            [{"name": "KUBERNETES_ENABLE_DRA", "value": "true"}],
            "map DRA ResourceClaim allocations to pods",
        ),
    ],
}
