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

"""The device stacks the provider installs on a VKE cluster.

No generator covers Vultr, so the components are hand-written, and
NVIDIA's pins come from the SDK.

VKE pre-installs a managed GPU Operator add-on on every GPU cluster. It
runs NFD with workers that tolerate every taint, and a second NFD would
race it over the same labels. It runs DCGM's exporter too. This stack
installs neither.

The add-on leaves the driver to the node image, so the SDK's DRA driver
pin holds as is. The add-on's device plugin would allocate the same
GPUs as the DRA driver, so compose-vultr-cluster labels GPU pools
nvidia.com/gpu.deploy.device-plugin=false, which the operator respects,
leaving the DRA driver the only allocator on them.
"""

from modelplane_sdk.stacks import Component, nvidia

COMPONENTS: list[Component] = [
    nvidia.DRA_DRIVER,
]
