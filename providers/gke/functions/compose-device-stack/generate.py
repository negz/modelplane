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

"""Generate GKE's device stack from NVIDIA AICR recipes.

Writes function/generated/aicr/gke.py. Run by `nix run .#stacks`; see
modelplane_sdk.aicr.

GKE resolves against a fork of AICR's gke-cos overlay, embedded below.
The `gpuStack` profile locks the GPU-advertiser choice at every output
boundary (including `bundle --set`), and neither upstream value
advertises through DRA; the fork adds a third value, `dra`, selected
with --profile gpuStack=dra. The profile-only --data patch aicr 1.0.0
added (aicr#2985) can't carry it - see the fork's header.
"""

import pathlib

from modelplane_sdk import aicr

# Replaces the embedded overlay of the same name, via `aicr recipe
# --data`.
GKE_COS_OVERLAY = """\
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
# Portions Copyright 2026 The Modelplane Authors.
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

# MODELPLANE FORK of aicr v1.0.0 recipes/overlays/gke-cos.yaml.
#
# aicr's --data mechanism replaces embedded catalog files wholesale by
# name, and that is the one supported way to add a configuration
# profile value that owns new paths: the gpuStack profile is locked at
# every output boundary (a layered overlay can't move its paths, and
# `bundle --set` on them is rejected), and neither upstream value
# advertises GPUs through DRA. v1.0.0's profile-only --data patch
# (aicr#2985) can't replace this fork: it is additive-only and a
# patched-in value must own exactly the embedded path set, while dra
# must own the DRA selector paths the upstream values don't - failing
# union totality by design, per the patch's own contract. So the fork
# adds one profile value, `dra`, gives the two upstream values the DRA
# selector paths at their stock settings (explicit false, union
# totality), raises the Kubernetes floor to DRA's, and changes nothing
# else.
#
# RE-SYNC ON EVERY aicr BUMP: diff this file against the pinned tag's
# recipes/overlays/gke-cos.yaml and re-apply the Modelplane changes
# (marked "Modelplane fork"). The file is high-churn upstream. The fork
# is deleted the day upstream declares a DRA advertiser value - the ask
# is NVIDIA/aicr#2517.

kind: RecipeMetadata
apiVersion: aicr.run/v1beta2
metadata:
  name: gke-cos

spec:
  criteria:
    service: gke
    os: cos

  constraints:
    # Modelplane fork: upstream says >= 1.32. Raised because DRA on GKE
    # needs Standard >= 1.35 (Google's set-up-dra guide), a profile
    # value may not restate a constraint name the composed recipe
    # carries, and this fork is only ever resolved with gpuStack=dra.
    - name: K8s.server.version
      value: ">= 1.35"

  # Upstream's gpuStack profile (ADR-015), plus Modelplane's dra value.
  # Upstream analysis this fork relies on: GKE's managed driver install
  # is finalized by an init container of the SAME kube-system DaemonSet
  # that the gke-no-default-nvidia-gpu-device-plugin=true label
  # disables, so a labeled pool comes up driverless and "GKE-installed
  # driver + another advertiser" is not a reachable state. Any value in
  # which GKE's managed plugin is NOT the advertiser therefore requires
  # the label and a driver supplied another way: bundle-installer
  # carries it in the bundle (the gcp-driver-installer component,
  # aicr#1716), dra takes Google's standalone nvidia-driver-installer
  # DaemonSet as an operational prerequisite outside the bundle.
  profile:
    name: gpuStack
    description: >-
      GPU stack shape for GKE GPU node pools - how the driver is installed
      and which advertiser owns the GPUs. The GPU Operator itself installs
      no driver on COS in any value. "gke-default" (the default) is the
      default-provisioned cluster: GKE's bundled driver install plus GKE's
      managed device plugin as the advertiser. "bundle-installer" is the
      opt-out pool shape (gke-no-default-nvidia-gpu-device-plugin=true,
      pools created with gpu-driver-version=disabled): the GPU Operator's
      device plugin is the sole advertiser, and the bundle-carried
      gcp-driver-installer component supplies the driver. "dra"
      (Modelplane's value) runs the NVIDIA DRA driver's ResourceSlices as
      the sole advertiser on the same labeled pool shape, with the driver
      supplied by Google's standalone nvidia-driver-installer DaemonSet.
    default: gke-default
    values:
      # Bundle-owned driver install (issue #1716): opt-out pools
      # (gpu-driver-version=disabled + the node label), with the
      # cos-gpu-installer DaemonSet carried by the gcp-driver-installer
      # component. The recipe pins the driver version; upgrades roll with
      # the bundle.
      bundle-installer:
        componentRefs:
          - name: gcp-driver-installer
            overrides:
              installer: {enabled: true}
          - name: gpu-operator
            overrides:
              devicePlugin:
                enabled: true
          # The bundle-carried installer IS a driver pod the NVSentinel
          # labeler detects, so no preinstalled-driver assumption.
          - name: nvsentinel
            overrides:
              labeler: {assumeDriverInstalled: false}
          # Modelplane fork: DRA selector paths at stock (union totality).
          - name: nvidia-dra-driver-gpu
            overrides:
              gpuResourcesEnabledOverride: false
              resources:
                gpus: {enabled: false}
        constraints:
          - name: NodeTopology.gpu-nodes.label
            value: gke-no-default-nvidia-gpu-device-plugin=true
            remediation: >-
              The bundle-installer profile requires the GPU Operator's
              device plugin to be the sole nvidia.com/gpu advertiser, so
              every GPU node pool must carry
              gke-no-default-nvidia-gpu-device-plugin=true at creation
              (gcloud container node-pools create ...
              --node-labels=gke-no-default-nvidia-gpu-device-plugin=true)
              and be created with gpu-driver-version=disabled - the label
              forfeits GKE's managed driver install, and the bundle's
              gcp-driver-installer component supplies the driver instead.
              See docs/integrator/gke-gpu-setup.md. To use a
              default-provisioned GKE cluster instead, regenerate with the
              default (--profile gpuStack=gke-default).
          - name: K8s.gke-gpu-pools.gpu-driver-installation
            value: Disabled
            remediation: >-
              The bundle-installer profile requires every GPU node pool to
              never have GKE's own managed driver install running, so it
              never races the bundle's own gcp-driver-installer component.
              This reading is satisfied by an explicit
              gpu-driver-version=disabled at creation, or by leaving the
              field unset on a GKE version whose documented default for
              that case is no install. A pool reporting Installed,
              NotConfigured, or NotInstalled has neither. An existing
              pool's driver-installation mode cannot be changed in place,
              so recreate any pool reporting otherwise with the explicit
              flag, which is the one fix that works regardless of GKE
              version (gcloud container node-pools create ...
              --accelerator type=<type>,count=<n>,gpu-driver-version=disabled).
              Then recapture the snapshot with
              --gke-gpu-pools pointed at a fresh `gcloud container
              node-pools list --cluster <cluster> --format=json` dump.
              See docs/integrator/gke-gpu-setup.md.

      # GKE-installed driver AND GKE's managed device plugin - a
      # default-provisioned GKE cluster (no node label required). The
      # external advertiser is counted as THE nvidia.com/gpu advertiser in
      # the #1327 exactly-one invariant; the resolved policy value stays
      # device-plugin-extended-resource (the policy names the request
      # mechanism, not provider ownership).
      gke-default:
        advertiser: external
        componentRefs:
          - name: gcp-driver-installer
            overrides:
              installer: {enabled: false}
          - name: gpu-operator
            overrides:
              devicePlugin:
                enabled: false
          - name: nvsentinel
            overrides:
              labeler: {assumeDriverInstalled: true}
          # Modelplane fork: DRA selector paths at stock (union totality).
          - name: nvidia-dra-driver-gpu
            overrides:
              gpuResourcesEnabledOverride: false
              resources:
                gpus: {enabled: false}
        constraints:
          - name: NodeTopology.gpu-nodes.label
            value: "!gke-no-default-nvidia-gpu-device-plugin"
            remediation: >-
              The gke-default profile (the default) requires GKE's managed
              device plugin to be the sole nvidia.com/gpu advertiser, so NO
              GPU node may carry the opt-out label
              gke-no-default-nvidia-gpu-device-plugin. Remove the label, or
              regenerate with --profile gpuStack=bundle-installer.

      # Modelplane's value: the NVIDIA DRA driver's ResourceSlices are
      # the sole GPU advertiser. The pool shape matches Google's own DRA
      # guide (docs.cloud.google.com/kubernetes-engine/docs/how-to/
      # set-up-dra): the opt-out label silences GKE's managed plugin
      # (else it would advertise nvidia.com/gpu beside the
      # ResourceSlices, two allocators over one device set), pools are
      # created with gpu-driver-version=disabled, and the driver is
      # installed separately (Google's standalone nvidia-driver-installer
      # DaemonSet) at /home/kubernetes/bin/nvidia. Google's guide also
      # sets gpuResourcesEnabledOverride and disables computeDomains -
      # the same values this profile owns. The bundle-carried
      # gcp-driver-installer stays off: its DaemonSet shares the
      # standalone one's name and Helm will not adopt the pre-existing
      # object, so flipping existing clusters to the bundle-carried
      # driver is a separate, operational change, not a bump side
      # effect. assumeDriverInstalled is true because the bundler
      # requires it whenever no bundle component supplies a driver pod -
      # it cannot see the standalone DaemonSet, which is an operational
      # prerequisite outside the bundle.
      dra:
        componentRefs:
          - name: gcp-driver-installer
            overrides:
              installer: {enabled: false}
          - name: gpu-operator
            overrides:
              devicePlugin:
                enabled: false
          - name: nvsentinel
            overrides:
              labeler: {assumeDriverInstalled: true}
          - name: nvidia-dra-driver-gpu
            overrides:
              gpuResourcesEnabledOverride: true
              resources:
                gpus: {enabled: true}
        constraints:
          # The 1.35 DRA floor lives in spec.constraints above: a profile
          # value may not restate a constraint name the composed recipe
          # already carries.
          - name: NodeTopology.gpu-nodes.label
            value: gke-no-default-nvidia-gpu-device-plugin=true
            remediation: >-
              The dra profile requires the NVIDIA DRA driver to be the sole
              GPU advertiser, so every GPU node pool must carry
              gke-no-default-nvidia-gpu-device-plugin=true at creation and
              be created with gpu-driver-version=disabled, with the driver
              installed separately (Google's standalone
              nvidia-driver-installer DaemonSet; the label forfeits GKE's
              managed driver install). Google's DRA guide additionally
              labels GPU pools nvidia.com/gpu.present=true and, for
              autoscaling, cloud.google.com/gke-nvidia-gpu-dra-driver=true.
              An unlabeled pool leaves GKE's managed device plugin
              advertising nvidia.com/gpu beside the DRA ResourceSlices -
              two allocators with independent ledgers over the same
              devices.

  # Upstream componentRefs, unchanged.
  componentRefs:
    # Values-gated driver installer (issue #1716): present unconditionally so
    # the gpuStack profile may own its installer.enabled gate (a profile can
    # change how components are configured, never the component set). Renders
    # nothing except under bundle-installer, where it supplies the driver.
    - name: gcp-driver-installer
      type: Helm
      valuesFile: components/gcp-driver-installer/values.yaml

    - name: gpu-operator
      type: Helm
      valuesFile: components/gpu-operator/values-gke-cos.yaml
      # Driver must land before GPU Operator operands start. A no-op
      # dependency under gke-default (empty release).
      dependencyRefs:
        - gcp-driver-installer

    - name: kube-prometheus-stack
      type: Helm
      overrides:
        prometheus:
          prometheusSpec:
            storageSpec:
              emptyDir: null
              volumeClaimTemplate:
                spec:
                  accessModes: ["ReadWriteOnce"]
                  resources:
                    requests:
                      storage: 50Gi

    - name: nvidia-dra-driver-gpu
      type: Helm
      overrides:
        nvidiaDriverRoot: /home/kubernetes/bin/nvidia
        controller:
          affinity:
            nodeAffinity: null

    - name: nodewright-operator
      type: Helm
      overrides:
        controllerManager:
          manager:
            env:
              copyDirRoot: /etc/nodewright
              reapplyOnReboot: "true"

  validation:
    conformance:
      checks:
        - platform-health
        - gpu-operator-health
        - dra-support
        - accelerator-metrics
        - ai-service-metrics
"""

CLOUD = aicr.Cloud(
    service="gke",
    aicr="1.0.0",
    os="cos",
    accelerators=["h100", "b200"],
    # The GKECluster XRD's default Kubernetes version. Keep in sync.
    k8s_default="1.35",
    # DRA on GKE needs Standard >= 1.35 (Google's set-up-dra guide) - a
    # floor AICR doesn't carry because neither stock gpuStack value
    # allocates through DRA, and one the fork can't reliably inject:
    # later overlays in the chain restate K8s.server.version, and a
    # profile value may not restate a composed-recipe constraint name.
    # So Modelplane owns it here, in the union.
    floors=["1.35"],
    overlays={"gke-cos.yaml": GKE_COS_OVERLAY},
    profiles=["gpuStack=dra"],
    drop={
        "gcp-driver-installer": (
            "values-gated to render nothing under gpuStack=dra; Modelplane's GKE driver "
            "is Google's standalone nvidia-driver-installer DaemonSet, an operational "
            "prerequisite outside the stack (see the fork's dra value)"
        ),
    },
    managed={
        ("nvidia-dra-driver-gpu", ("nvidiaDriverRoot",)): (
            "/home/kubernetes/bin/nvidia",
            "Google's installer puts the driver here under every gpuStack value",
        ),
        # Forcing the toolkit off here leaves every operand failing
        # pod-sandbox creation with 'no runtime for "nvidia" is
        # configured'; the node-image assumption behind the shared
        # toolkit=false holds everywhere but on COS.
        ("gpu-operator", ("toolkit", "enabled")): (
            True,
            "COS ships no nvidia containerd runtime; per Google's and AICR's "
            "GKE guidance the operator's toolkit configures one",
        ),
    },
    # GKE auto-runs a managed DCGM exporter on GPU nodes
    # (gke-managed-system), and DCGM's profiling module is single-client
    # per GPU - a second exporter dies with 'the third-party Profiling
    # module returned an unrecoverable error'. GPU telemetry on GKE is
    # the platform's, like the driver.
    managed_extra={
        "gpu-operator": [
            (("dcgm", "enabled"), False, "GKE's managed DCGM owns the GPU's single-client profiling module"),
            (("dcgmExporter", "enabled"), False, "GKE's managed dcgm-exporter provides GPU telemetry"),
        ],
    },
    # The fork's dra profile value owns the advertiser paths (aicr
    # rejects a --set on a profile-owned path) and its overlay sets the
    # driver root.
    catalog_owned={
        ("nvidia-dra-driver-gpu", ("gpuResourcesEnabledOverride",)),
        ("nvidia-dra-driver-gpu", ("resources", "gpus", "enabled")),
        ("nvidia-dra-driver-gpu", ("nvidiaDriverRoot",)),
        ("gpu-operator", ("devicePlugin", "enabled")),
        ("nvsentinel", ("labeler", "assumeDriverInstalled")),
    },
    # nodewright 0.19.0 renamed its chart namespace from skyhook (the
    # project's former name) to nodewright; Modelplane's releases live
    # in skyhook.
    namespace_pins={"nodewright-operator": "skyhook"},
)

if __name__ == "__main__":
    aicr.main(CLOUD, pathlib.Path(__file__).parent / "function" / "generated" / "aicr" / "gke.py")
