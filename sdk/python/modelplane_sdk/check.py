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

"""Check what a provider declares to Modelplane against what it does.

    python -m modelplane_sdk.check <provider-dir>

Run it in the provider's compose-device-stack environment, where
`function.stack` is the device stack's component list. The provider is
named for its directory. The check fails unless the directory has a
conformance.yaml, for the conformance suite to run. It checks the
provider's Composition for a ProvisionedCluster: that it targets the
contract, is named <provider>.provisionedclusters.infrastructure.modelplane.ai,
carries the labels Modelplane selects it by, names a cluster config kind
one of the provider's XRDs defines, declares only capabilities the
contract defines, and declares exactly the DRA drivers its device stack
carries plus those it installs outside it, which stack.py lists as
EXTERNAL_DRIVERS. Package object names are global, so it checks every
activation policy is named modelplane-<provider>. Last, it holds the
device stack's component list to modelplane_sdk.stacks.check and to the
SDK's NVIDIA pins.
"""

import pathlib
import sys
from typing import Any

import yaml

from modelplane_sdk import contract, stacks
from modelplane_sdk.stacks import nvidia


def _listed(value: str) -> frozenset[str]:
    return frozenset(v.strip() for v in value.split(",") if v.strip())


def _objects(apis: pathlib.Path) -> list[dict[str, Any]]:
    return [doc for path in sorted(apis.rglob("*.yaml")) for doc in yaml.safe_load_all(path.read_text()) if doc]


def problems(
    provider: str,
    *,
    objects: list[dict[str, Any]],
    conformance: bool,
    components: list[stacks.Component],
    external: list[str],
) -> list[str]:
    """Everything the provider declares that doesn't match what it does.

    provider is the provider's name, objects the package objects under its
    apis directory, and conformance whether it has a conformance.yaml.
    components and external are its device stack's COMPONENTS and
    EXTERNAL_DRIVERS.
    """
    found: list[str] = []
    if not conformance:
        found.append(f"{provider} has no conformance.yaml, so the conformance suite can't run it")

    for o in objects:
        name = o["metadata"]["name"]
        if o["kind"] == "ManagedResourceActivationPolicy" and name != f"modelplane-{provider}":
            found.append(f"activation policy {name} should be named modelplane-{provider}")

    try:
        stacks.check(provider, components)
    except ValueError as e:
        found.append(str(e))
    try:
        nvidia.check_pins(provider, components)
    except ValueError as e:
        found.append(str(e))

    compositions = [
        o
        for o in objects
        if o["kind"] == "Composition"
        and o["spec"]["compositeTypeRef"] == {"apiVersion": contract.API_VERSION, "kind": contract.KIND}
    ]
    if len(compositions) != 1:
        return [*found, f"want one Composition for a {contract.KIND}, found {len(compositions)}"]
    meta = compositions[0]["metadata"]
    labels, annotations = meta.get("labels", {}), meta.get("annotations", {})

    if meta["name"] != f"{provider}.provisionedclusters.infrastructure.modelplane.ai":
        found.append(
            f"Composition {meta['name']} should be named {provider}.provisionedclusters.infrastructure.modelplane.ai"
        )

    group, kind = labels.get(contract.LABEL_CONFIG_GROUP), labels.get(contract.LABEL_CONFIG_KIND)
    xrds = {
        (o["spec"]["group"], o["spec"]["names"]["kind"]) for o in objects if o["kind"] == "CompositeResourceDefinition"
    }
    if (group, kind) not in xrds:
        found.append(f"Composition {meta['name']} selects on {kind}.{group}, which none of the provider's XRDs define")

    unknown = _listed(annotations.get(contract.ANNOTATION_CAPABILITIES, "")) - contract.CAPABILITIES
    if unknown:
        found.append(f"Composition {meta['name']} declares unknown capabilities {sorted(unknown)}")

    declared = _listed(annotations.get(contract.ANNOTATION_DRA_DRIVERS, ""))
    carried = stacks.supported_drivers(components) | frozenset(external)
    if declared != carried:
        found.append(
            f"Composition {meta['name']} declares DRA drivers {sorted(declared)}, "
            f"but the device stack and EXTERNAL_DRIVERS carry {sorted(carried)}"
        )
    return found


def main() -> None:
    """Check the provider directory named on the command line."""
    # The provider's device stack, importable from its venv.
    from function import stack  # noqa: PLC0415  # ty: ignore[unresolved-import]

    provider = pathlib.Path(sys.argv[1])
    found = problems(
        provider.name,
        objects=_objects(provider / "apis"),
        conformance=(provider / "conformance.yaml").is_file(),
        components=stack.COMPONENTS,
        external=getattr(stack, "EXTERNAL_DRIVERS", []),
    )
    for problem in found:
        print(problem, file=sys.stderr)  # noqa: T201
    if found:
        sys.exit(1)


if __name__ == "__main__":
    main()
