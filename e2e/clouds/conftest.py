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

"""Fixtures the device stack tests share. See e2e/README.md.

The tests run against clusters that are already up, with the leg's fake
device nodes, its ServingStack and, on a provider's leg, its device stack
applied. `nix run .#e2e -- --cloud <leg>` brings them up first and names the
leg under test with pytest's --cloud.
"""

import pytest

from e2e import clouds, environment, kube, wait


def pytest_addoption(parser: pytest.Parser) -> None:
    """Name the cloud leg the environment was brought up with."""
    parser.addoption("--cloud", required=True, choices=sorted(clouds.CLOUDS), help="the leg under test")


@pytest.fixture(scope="session")
def cloud(request: pytest.FixtureRequest) -> clouds.Cloud:
    """The leg under test."""
    return clouds.CLOUDS[request.config.getoption("--cloud")]


@pytest.fixture(scope="session")
def control_plane() -> kube.Cluster:
    """The control plane, running Crossplane and Modelplane."""
    return kube.Cluster(environment.CONTROL_PLANE_CONTEXT)


@pytest.fixture(scope="session")
def workload() -> kube.Cluster:
    """The workload cluster, whose fake nodes play the leg's tainted device pools."""
    return kube.Cluster(environment.WORKLOAD_CONTEXT)


@pytest.fixture(scope="session")
def installed(control_plane: kube.Cluster, cloud: clouds.Cloud) -> None:
    """Wait for the ServingStack, and a provider's device stack, to install on the workload cluster.

    Bring-up returns once the XRs are applied, and the install - a dozen Helm
    releases, gated in waves - reconciles in the background. Every placement
    test needs the stacks in place, so this waits once for the whole session.
    The device stack waits in turn for the ServingStack's cert-manager.
    """
    xrs = [("infrastructure.modelplane.ai/v1alpha1", "servingstacks", "ServingStack", clouds.SERVING_STACK)]
    if cloud.device_stack is not None:
        api_version, kind, plural = cloud.device_stack
        xrs.append((api_version, plural, kind, clouds.DEVICE_STACK))

    def ready() -> None:
        for api_version, plural, kind, name in xrs:
            group, version = api_version.split("/")
            obj = control_plane.modelplane(plural, name, clouds.NAMESPACE, group=group, version=version)
            assert obj is not None, f"{kind} {clouds.NAMESPACE}/{name} doesn't exist"
            conditions = obj.get("status", {}).get("conditions", [])
            assert any(c["type"] == "Ready" and c["status"] == "True" for c in conditions), (
                f"{kind} {name} isn't Ready: "
                f"{[(c['type'], c['status'], c.get('reason'), c.get('message')) for c in conditions]}"
            )

    wait.until(ready, timeout=30 * 60, what=f"the {cloud.cloud} stacks to install", retry=kube.RETRY)
