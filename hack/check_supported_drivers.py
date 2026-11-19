#!/usr/bin/env python3
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

"""Check compose-inference-cluster's copy of the serving stack's own DRA drivers.

compose-inference-cluster rejects a class claiming a driver a provisioned
cluster can't serve before it composes anything. A cluster serves the drivers
its provider declares, plus those the serving stack installs on every cluster
whoever provisioned it, which compose-inference-cluster keeps a hand-kept copy
of in _SERVING_STACK_DRA_DRIVERS. Each function's package is named `function`,
so neither can import the other. This runs with compose-serving-stack
installed and reads the copy from compose-inference-cluster's source.
"""

import ast
import pathlib
import sys

from function import stacks  # ty: ignore[unresolved-import]  # compose-serving-stack, installed in the check's venv.
from modelplane_sdk.stacks import supported_drivers

REPO = pathlib.Path(__file__).resolve().parents[1]
MIRROR = REPO / "functions" / "compose-inference-cluster" / "function" / "fn.py"


def mirror() -> frozenset[str]:
    """Return compose-inference-cluster's _SERVING_STACK_DRA_DRIVERS, read from its source."""
    for node in ast.parse(MIRROR.read_text()).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "_SERVING_STACK_DRA_DRIVERS":
            if not isinstance(node.value, ast.Call) or len(node.value.args) != 1:
                sys.exit(f"{MIRROR}:{node.lineno}: _SERVING_STACK_DRA_DRIVERS isn't frozenset({{...}})")
            return frozenset(ast.literal_eval(node.value.args[0]))
    sys.exit(f"{MIRROR}: no _SERVING_STACK_DRA_DRIVERS")


def main() -> int:
    """Report a copy that differs from the drivers the serving stack installs on a provisioned cluster."""
    want = frozenset().union(*(supported_drivers(stacks.join(stack, "External")) for stack in stacks.stacks()))
    got = mirror()
    if want == got:
        return 0
    print(
        f"compose-serving-stack installs {sorted(want)} on every provisioned cluster, "
        f"but compose-inference-cluster's _SERVING_STACK_DRA_DRIVERS has {sorted(got)}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
