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

"""D. Clouds: Modelplane provisions a real cloud cluster, and deletes it.

These tests create real, billed infrastructure, so they're skipped unless run
with --cloud. They need only a control plane, not the workload cluster, and
credentials for the cloud:

    pytest e2e --cloud=eks --aws-credentials=$HOME/.aws/credentials -m cloud
"""

import pathlib
from collections.abc import Iterator

import pytest

from e2e import kube

pytestmark = pytest.mark.cloud("eks")

MANIFESTS = pathlib.Path(__file__).parent / "manifests" / "clouds" / "eks"
CLUSTER = "e2e-eks-us-east"

# The getting-started guide says an EKS cluster takes about 15 minutes to
# provision.
EKS_TIMEOUT = 30 * 60


@pytest.fixture(scope="module")
def aws(control_plane: kube.Cluster, pytestconfig: pytest.Config) -> Iterator[None]:
    """Setup: give the control plane AWS credentials, the way the getting-started guide does."""
    credentials = pytestconfig.getoption("aws_credentials")
    if credentials is None:
        pytest.fail("--cloud=eks needs --aws-credentials, an AWS shared credentials file")
    try:
        secret = control_plane.kubectl(
            "create", "secret", "generic", "aws-creds", "--namespace=crossplane-system",
            f"--from-file=credentials={credentials}", "--dry-run=client", "--output=json",
        )  # fmt: skip
        control_plane.kubectl("apply", "--filename=-", stdin=secret)
        control_plane.apply(MANIFESTS / "setup")
        yield
    finally:
        control_plane.delete("inferenceclass", "e2e-l4-1x-g6")
        control_plane.delete("clusterproviderconfig.aws.m.upbound.io", "default")
        control_plane.delete("secret", "aws-creds", "crossplane-system")


@pytest.fixture
def inference_cluster(control_plane: kube.Cluster, aws: None) -> Iterator[None]:  # noqa: ARG001 - needs credentials.
    """Setup: create an EKS InferenceCluster. Teardown: make sure it's gone, so it stops costing money."""
    try:
        control_plane.apply(MANIFESTS / "inference-cluster.yaml")
        yield
    finally:
        control_plane.delete("inferencecluster", CLUSTER)
        control_plane.wait_until_gone("inferencecluster", CLUSTER, None, timeout=EKS_TIMEOUT)


@pytest.mark.usefixtures("inference_cluster")
def test_d1_eks_inference_cluster_provisions_and_deletes(control_plane: kube.Cluster) -> None:
    """D1: An EKS InferenceCluster becomes Ready, and once deleted it's gone."""
    control_plane.wait_for_condition("inferencecluster", CLUSTER, None, "Ready", timeout=EKS_TIMEOUT)
    control_plane.delete("inferencecluster", CLUSTER)
    control_plane.wait_until_gone("inferencecluster", CLUSTER, None, timeout=EKS_TIMEOUT)
