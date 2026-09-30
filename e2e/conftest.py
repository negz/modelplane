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

"""Options and fixtures every end-to-end test shares. See README.md."""

import pathlib
from collections.abc import Generator, Iterator

import pytest
from models.ai.modelplane.inferencegateway import v1alpha1 as inferencegatewayv1alpha1

from e2e import environment, gateway, kube, wait

MANIFESTS = pathlib.Path(__file__).parent / "manifests"

# What a failed test's report lists, from the namespaces it's marked to
# diagnose.
MODELPLANE_KINDS = "modeldeployments,modelreplicas,modelendpoints,modelservices"

# The only cloud a test can provision so far. See test_clouds.py.
CLOUDS = ("eks",)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the options that pick an environment, and the tests to run in it."""
    group = parser.getgroup("modelplane", "Modelplane end-to-end tests")
    group.addoption(
        "--bring-up",
        action="store_true",
        help="bring up the kind clusters and install Modelplane before the first test that needs them. "
        "Without it, the tests run against clusters that are already up.",
    )
    group.addoption("--control-plane-context", default=environment.CONTROL_PLANE_CONTEXT)
    group.addoption("--workload-context", default=environment.WORKLOAD_CONTEXT)
    group.addoption(
        "--cloud",
        action="append",
        default=[],
        choices=CLOUDS,
        help="also run the tests that provision real infrastructure in this cloud. Repeatable.",
    )
    group.addoption("--aws-credentials", type=pathlib.Path, help="an AWS shared credentials file, for --cloud=eks")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Add the state of a failed test's Modelplane resources to its report.

    A test marked diagnose(namespace, ...) gets a section per namespace, listing
    each Modelplane resource there with its conditions.
    """
    report = yield
    marker = item.get_closest_marker("diagnose")
    if not report.failed or marker is None:
        return report
    control_plane = kube.Cluster(item.config.getoption("control_plane_context"))
    for namespace in marker.args:
        try:
            objs = control_plane.list_objects(MODELPLANE_KINDS, namespace)
            text = "\n".join(kube.describe(o) for o in objs) or "none"
        except kube.KubectlError as e:
            text = str(e)
        report.sections.append((f"Modelplane resources in {namespace}", text))
    return report


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the tests that provision real cloud infrastructure, unless asked to run them."""
    for item in items:
        marker = item.get_closest_marker("cloud")
        if marker is None or marker.args[0] in config.getoption("cloud"):
            continue
        cloud = marker.args[0]
        item.add_marker(pytest.mark.skip(reason=f"provisions real {cloud} infrastructure; run with --cloud={cloud}"))


@pytest.fixture(scope="session")
def control_plane(pytestconfig: pytest.Config) -> kube.Cluster:
    """The control plane, running Crossplane and Modelplane."""
    if pytestconfig.getoption("bring_up"):
        environment.up_control_plane()
    return kube.Cluster(pytestconfig.getoption("control_plane_context"))


@pytest.fixture(scope="session")
def workload(pytestconfig: pytest.Config, control_plane: kube.Cluster) -> kube.Cluster:
    """The workload cluster, registered with the control plane as InferenceCluster local."""
    workload = kube.Cluster(pytestconfig.getoption("workload_context"))
    if pytestconfig.getoption("bring_up"):
        environment.up_workload()
        wait_for_platform(control_plane, workload)
    return workload


def wait_for_platform(control_plane: kube.Cluster, workload: kube.Cluster) -> None:
    """Wait for a freshly brought-up platform to serve ml-team's mock model.

    Each test waits for what it needs, but no longer than that should take on a
    settled environment. On a fresh one the serving stack is still installing,
    so bring-up waits it out once, before the first test.
    """
    # The ModelService's route is composed and accepted on every gateway
    # serving it.
    control_plane.wait_for_condition("modelservice", "mock", "ml-team", "RoutingReady", timeout=20 * 60)

    # AI Gateway rolls the gateway's proxy pods once the first route reaches
    # it, to stamp them with the hash of its sidecar's config, so a fresh
    # gateway is still replacing its pods when the route goes ready. Requests
    # can fail during that rollout. The rollout starts only once AI Gateway has
    # seen the route, so first wait for the stamp.
    def proxies_stamped() -> None:
        deployments = workload.list_objects("deployment", gateway.PROXY_NAMESPACE, gateway.PROXY_SELECTOR)
        annotations = [d["spec"]["template"]["metadata"].get("annotations", {}) for d in deployments]
        assert annotations, "the InferenceGateway has no proxy Deployment"
        assert all("aigateway.envoyproxy.io/extproc-config-hash" in a for a in annotations), (
            "AI Gateway hasn't stamped the InferenceGateway's proxy pods"
        )

    wait.until(proxies_stamped, timeout=2 * 60, what="AI Gateway to stamp the InferenceGateway's proxy pods")
    workload.kubectl(
        "rollout", "status", "deployment", f"--namespace={gateway.PROXY_NAMESPACE}",
        f"--selector={gateway.PROXY_SELECTOR}", "--timeout=5m",
        timeout=6 * 60,
    )  # fmt: skip


@pytest.fixture(scope="session")
def endpoints(
    control_plane: kube.Cluster,
    workload: kube.Cluster,  # noqa: ARG001 - the gateway runs there, so bring it up first.
) -> inferencegatewayv1alpha1.Endpoints:
    """The OpenAI and Anthropic base URLs InferenceGateway local publishes."""

    def published() -> inferencegatewayv1alpha1.Endpoints:
        obj = control_plane.get("inferencegateway", "local")
        assert obj is not None, "InferenceGateway local doesn't exist"
        ig = inferencegatewayv1alpha1.InferenceGateway.model_validate(obj)
        assert ig.status is not None, "InferenceGateway local has no status"
        endpoints = ig.status.endpoints
        assert endpoints is not None, "InferenceGateway local hasn't published its endpoints"
        assert endpoints.openAI is not None, "InferenceGateway local hasn't published its OpenAI endpoint"
        assert endpoints.anthropic is not None, "InferenceGateway local hasn't published its Anthropic endpoint"
        return endpoints

    return wait.until(published, timeout=5 * 60, what="InferenceGateway local to publish its endpoints")


@pytest.fixture(scope="session")
def client(control_plane: kube.Cluster) -> Iterator[gateway.Client]:
    """A pod on the control plane to send requests to the gateway from."""
    try:
        control_plane.apply(MANIFESTS / "client")
        control_plane.wait_for_condition("pod", "curl", "bakeoff-pytest-client", "Ready", timeout=2 * 60)
        yield gateway.Client(control_plane, "bakeoff-pytest-client", "curl")
    finally:
        # Wait, so the next run doesn't try to create the pod in a namespace
        # that's still terminating.
        control_plane.delete("namespace", "bakeoff-pytest-client")
        control_plane.wait_until_gone("namespace", "bakeoff-pytest-client", None, timeout=2 * 60)
