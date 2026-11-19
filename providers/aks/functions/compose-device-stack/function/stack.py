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

"""The device stacks the provider installs on an AKS cluster.

The components are generated from NVIDIA AICR recipes, into
generated/aicr/aks.py by generate.py. AKS installs the NVIDIA driver on
GPU nodes itself, so the generated GPU Operator leaves the driver to it.

The generated components depend on cert-manager, which the serving stack
installs on every cluster, so the list joins CERT_MANAGER to observe it.

The NVIDIA network operator an InfiniBand pool needs isn't here:
compose-aks-cluster installs it with the cluster, only when a pool asks
for InfiniBand.
"""

from modelplane_sdk.stacks import CERT_MANAGER, Component

from function.generated.aicr import aks

COMPONENTS: list[Component] = [
    *aks.COMPONENTS,
    CERT_MANAGER,
]
