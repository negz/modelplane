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

"""Check the boundary between Modelplane and its cluster providers.

Each provider under providers/ is built and released as a package of its
own, and has to stay extractable into a repository of its own. So:

- Modelplane's own APIs, functions, activation policy, project file, and
  the provider SDK name no provider: no cloud, no cloud's Crossplane
  provider or managed resource group, and no file under providers/.
- A provider reaches nothing outside its own directory but the SDK, and
  of Modelplane's APIs imports only the models of the ProvisionedCluster
  contract, which it generates from Modelplane's built package.

Docs, design docs, the end-to-end tests and the functions' unit tests
are outside the check: they describe the providers, or record where a
fixture was captured.
"""

import ast
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]

# What Modelplane's own source is.
CORE = [
    REPO / "apis",
    REPO / "functions",
    REPO / "sdk",
    REPO / "crossplane-project.yaml",
]

# Words that name a cloud, a cloud's Kubernetes service, or its Crossplane
# providers' managed resource groups.
CLOUD = re.compile(
    r"\b(gke|eks|aks|nebius|vultr|civo|coreweave|cks|mk8s|vke|aws|gcp|azure|google|amazon|microsoft)\b"
    r"|\.(aws|gcp|azure|nebius|vultr|civo|coreweave)\.m\.upbound\.io"
    r"|provider-(aws|gcp|azure|nebius|vultr|civo|coreweave)",
    re.IGNORECASE,
)

# provider-kubernetes's identity types. They name clouds, but they're that
# provider's vocabulary, which an Existing cluster's identity secret uses.
# The protobuf runtime's package is google.protobuf, which names no cloud.
ALLOWED = re.compile(
    r"GoogleApplicationCredentials|AWSWebIdentityCredentials|NebiusServiceAccountCredentials|google[./]protobuf"
)

SOURCE_SUFFIXES = {".py", ".yaml", ".yml", ".toml", ".json", ".md", ".csv"}

# The only Modelplane models a provider may import: the contract's.
CONTRACT_MODELS = "models.ai.modelplane.infrastructure.provisionedcluster"


def files(root: pathlib.Path, *, skip: frozenset[str] = frozenset()) -> list[pathlib.Path]:
    """The source files under root, skipping build output and any directory named in skip."""
    if root.is_file():
        return [root]
    skipped = {"_output", "__pycache__", ".venv"} | skip
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix in SOURCE_SUFFIXES and not skipped & set(p.relative_to(REPO).parts)
    )


def check_core() -> list[str]:
    """Places Modelplane's own source names a provider."""
    problems = []
    for root in CORE:
        for path in files(root, skip=frozenset({"tests"})):
            for n, line in enumerate(path.read_text().splitlines(), start=1):
                stripped = ALLOWED.sub("", line)
                match = CLOUD.search(stripped)
                if match:
                    problems.append(f"{path.relative_to(REPO)}:{n}: names a provider ({match.group(0)!r})")
                if "providers/" in stripped:
                    problems.append(f"{path.relative_to(REPO)}:{n}: refers to a provider's directory")
    return problems


def imports(source: str) -> list[tuple[int, str]]:
    """The modules a Python source file imports, with the lines that import them."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
    return found


def check_provider(provider: pathlib.Path) -> list[str]:
    """Places a provider reaches outside its own directory, or imports Modelplane's models."""
    problems = []
    group = f"models.ai.modelplane.{provider.name}"
    sdk = (REPO / "sdk" / "python").resolve()
    for path in files(provider):
        rel = path.relative_to(REPO)
        if "schemas" in rel.parts:
            continue
        text = path.read_text()
        # A relative path that leaves the provider's directory, other than to
        # the SDK.
        for n, line in enumerate(text.splitlines(), start=1):
            for ref in re.findall(r"(?:\.\./)+[\w./-]*", line):
                target = (path.parent / ref).resolve()
                if not target.is_relative_to(provider.resolve()) and not target.is_relative_to(sdk):
                    problems.append(f"{rel}:{n}: refers to {ref}, outside the provider")
        if path.suffix != ".py":
            continue
        for lineno, module in imports(text):
            if module.startswith("models.ai.modelplane") and not module.startswith((group + ".", CONTRACT_MODELS)):
                problems.append(f"{rel}:{lineno}: imports {module}, which isn't the contract or its own")
    for path in provider.rglob("*"):
        if "_output" in path.parts or not path.is_symlink():
            continue
        if not path.resolve().is_relative_to(provider.resolve()):
            problems.append(f"{path.relative_to(REPO)}: links outside the provider")
    return problems


def main() -> None:
    """Report every boundary crossing, and fail if there are any."""
    problems = check_core()
    for provider in sorted(p for p in (REPO / "providers").iterdir() if p.is_dir()):
        problems += check_provider(provider)
    for problem in problems:
        print(problem)
    if problems:
        sys.exit(f"{len(problems)} boundary problems; see hack/check_boundaries.py")


if __name__ == "__main__":
    main()
