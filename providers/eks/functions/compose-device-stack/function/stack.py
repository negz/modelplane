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

"""The device stacks the provider installs on an EKS cluster.

The components are generated from NVIDIA AICR recipes, in
generated/aicr/eks.py; see generate.py. The GPU Operator and NVSentinel
depend on the serving stack's cert-manager, which this list carries as
stacks.CERT_MANAGER.

EXTERNAL_DRIVERS are the DRA drivers the provider supports without
this stack installing them: compose-eks-cluster installs the EFA DRA
driver, dra.net, when a pool's node class asks for the EFA fabric.
"""

from modelplane_sdk import stacks

from function.generated.aicr import eks

EXTERNAL_DRIVERS: list[str] = eks.EXTERNAL_DRIVERS

COMPONENTS: list[stacks.Component] = [
    stacks.CERT_MANAGER,
    *eks.COMPONENTS,
]
