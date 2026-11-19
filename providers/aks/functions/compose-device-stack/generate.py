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

"""Generate AKS's device stack from NVIDIA AICR recipes.

Writes function/generated/aicr/aks.py. Run by `nix run .#stacks`; see
modelplane_sdk.aicr.
"""

import pathlib

from modelplane_sdk import aicr

CLOUD = aicr.Cloud(
    service="aks",
    aicr="1.0.0",
    # AKS refuses --os without an accelerator, so the coordinate leaves
    # os unset.
    accelerators=["h100"],
    # The AKSCluster XRD's default Kubernetes version. Keep in sync.
    k8s_default="1.34",
    # aicr rejects its own wildcard toleration under AKS admission, so
    # the bundle takes an explicit one. The value must match the taint
    # compose-aks-cluster puts on GPU pools (nvidia.com/gpu=true) - an
    # Equal toleration with any other value excludes every accelerated
    # daemonset (NFD's worker first, and with it the label the DRA
    # kubelet plugin schedules on) from exactly the nodes it exists
    # for.
    bundle_flags=["--accelerated-node-toleration", "nvidia.com/gpu=true:NoSchedule"],
    drop={
        "network-operator": "the AKSCluster composition installs it when a pool asks for InfiniBand",
    },
    # The toolkit-hardening DaemonSet AKS's gpu-operator carries exists
    # to reconfigure containerd on GPU nodes.
    manifest_tolerations={
        ("gpu-operator", "DaemonSet", "nvidia-toolkit-hardening"): aicr.GPU,
    },
    # nodewright 0.19.0 renamed its chart namespace from skyhook (the
    # project's former name) to nodewright; Modelplane's releases live
    # in skyhook.
    namespace_pins={"nodewright-operator": "skyhook"},
)

if __name__ == "__main__":
    aicr.main(CLOUD, pathlib.Path(__file__).parent / "function" / "generated" / "aicr" / "aks.py")
