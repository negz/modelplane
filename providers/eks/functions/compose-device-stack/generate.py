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

"""Generate EKS's device stack from NVIDIA AICR recipes.

Writes function/generated/aicr/eks.py. Run by `nix run .#stacks`; see
modelplane_sdk.aicr.
"""

import pathlib

from modelplane_sdk import aicr

CLOUD = aicr.Cloud(
    service="eks",
    aicr="1.0.0",
    # No recipe covers AL2023, Modelplane's node image, so the
    # coordinate leaves os unset.
    accelerators=["h100", "h200", "gb200", "rtx-pro-6000"],
    # The EKSCluster XRD's default Kubernetes version. Keep in sync.
    k8s_default="1.36",
    drop={
        "aws-efa": "the EKSCluster installs an EFA DRA driver when a pool asks for the fabric",
        "aws-ebs-csi-driver": "Modelplane's only storage need is RWX, which the EKSCluster serves from EFS",
    },
    # nodewright 0.19.0 renamed its chart namespace from skyhook (the
    # project's former name) to nodewright; Modelplane's releases live
    # in skyhook.
    namespace_pins={"nodewright-operator": "skyhook"},
    # dra.net is the aws-efa drop above as the driver filter sees it:
    # the EKSCluster installs the EFA DRA driver when a pool asks for
    # the fabric.
    external_drivers=["dra.net"],
)

if __name__ == "__main__":
    aicr.main(CLOUD, pathlib.Path(__file__).parent / "function" / "generated" / "aicr" / "eks.py")
