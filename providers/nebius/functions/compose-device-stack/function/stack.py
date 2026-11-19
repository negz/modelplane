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

"""The device stacks the provider installs on an mk8s cluster.

No generator covers Nebius, so the components are hand-written, and
NVIDIA's pins come from the SDK.

mk8s preinstalls the NVIDIA driver on a GPU node group's nodes, from the
group's driversPreset, at the default root. This stack installs what
binds those GPUs to pods through DRA: NFD to label the GPU nodes, and
NVIDIA's DRA driver, which runs only on nodes NFD labelled. It also
installs a DCGM exporter. Its affinity covers Nebius GPU nodes, which
have pci-0302_10de but not pci-10de.
"""

from modelplane_sdk.stacks import Component, nvidia

COMPONENTS: list[Component] = [
    nvidia.NODE_FEATURE_DISCOVERY,
    nvidia.DRA_DRIVER,
    nvidia.DCGM_EXPORTER,
]
