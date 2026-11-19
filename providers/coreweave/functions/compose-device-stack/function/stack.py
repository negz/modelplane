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

"""The device stacks the provider installs on a CKS cluster.

No generator covers CoreWeave, so the components are hand-written, and
NVIDIA's pins come from the SDK.

CoreWeave runs the NVIDIA driver, a managed GPU Operator and NFD on every
CKS cluster, and its docs say not to install a second GPU Operator. This
stack installs none of them.

CoreWeave also installs NVIDIA's DRA driver, but only for ComputeDomains.
This stack installs it again with GPU allocation on and ComputeDomains
off; the SDK's pin does exactly that, and the node image puts the driver
at the default root.

CoreWeave's own DCGM exporter attributes a GPU's series to a pod only if
the device plugin allocated it. This stack installs a second exporter
that maps DRA allocations to pods. Both exporters run side by side
without disturbing each other's profiling metrics, and
compose-provisioned-cluster tells the serving stack's collector to
ignore CoreWeave's. The exporter's affinity covers CKS GPU nodes, which
have pci-0302_10de.
"""

from modelplane_sdk.stacks import Component, nvidia

COMPONENTS: list[Component] = [
    nvidia.DRA_DRIVER,
    nvidia.DCGM_EXPORTER,
]
