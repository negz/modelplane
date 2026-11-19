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

"""Generate a provider's device stack from NVIDIA AICR recipes.

The generator design/serving-stack-generation.md describes, for any
provider whose cloud AICR has a `service` value for. A provider
declares what it knows about its cloud as a Cloud - its coordinate,
the catalog overlays and profile values it resolves with, and its
additions to the shared tables in tables - and calls main with the
path of the module to write: a COMPONENTS list of Chart and Manifests
entries, and the EXTERNAL_DRIVERS the provider supports outside it.

    from modelplane_sdk import aicr

    CLOUD = aicr.Cloud(service="...", aicr="1.0.0", ...)

    if __name__ == "__main__":
        aicr.main(CLOUD, pathlib.Path("function/generated/aicr/<cloud>.py"))

Every component in a recipe must be classified in ALLOW, DROP or
OBSERVED, or the Cloud's allow or drop - an unknown component fails
the run, else NVIDIA adding a component would silently appear on every
Modelplane cluster. Every wildcard toleration the bundle stamps on a
pod must likewise be classified, else that pod schedules onto tainted
GPU nodes. The run needs the pinned aicr on PATH.

Bumping the pinned aicr is a reviewed stack change - it moves the
components and versions every managed cluster runs on the next
Modelplane release. The procedure lives in CONTRIBUTING.md, "Bumping
aicr".
"""

from modelplane_sdk.aicr.cloud import Cloud
from modelplane_sdk.aicr.generate import main
from modelplane_sdk.aicr.tables import GPU, SYSTEM

__all__ = ["GPU", "SYSTEM", "Cloud", "main"]
