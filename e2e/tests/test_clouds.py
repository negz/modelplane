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

"""Scenario D: Modelplane provisions a real cloud cluster.

It provisions real AWS infrastructure, so it's skipped unless a maintainer asks
for it by naming the control plane to run it on:

    MODELPLANE_E2E_EKS_CONTEXT=<kube context> python3 -m unittest discover -s e2e/tests -p test_clouds.py -v

That control plane isn't the kind one e2e/run.sh brings up, whose lean MRAP
keeps the cloud providers dormant (see e2e/README.md). It's one set up as the
EKS getting started guide does, with AWS credentials in Secret
crossplane-system/aws-creds and the ClusterProviderConfig that reads them. It
needs no workload kind cluster: the InferenceCluster it creates is the workload
cluster.

It creates and deletes the InferenceClass and InferenceCluster in
docs/manifests/getting-started/eks/platform.yaml, so point it at a control
plane that doesn't already have them.
"""

import os
import pathlib
import unittest

import kube

CONTEXT = os.environ.get("MODELPLANE_E2E_EKS_CONTEXT", "")

PLATFORM = pathlib.Path(__file__).parents[2] / "docs/manifests/getting-started/eks/platform.yaml"
CLUSTER = "eks-us-east"


@unittest.skipUnless(CONTEXT, "provisions a real EKS cluster; set MODELPLANE_E2E_EKS_CONTEXT to run it")
class Clouds(unittest.TestCase):
    maxDiff = None

    def test_d1_eks_inference_cluster(self) -> None:
        """D1: An EKS InferenceCluster becomes Ready, then deletes."""
        # Fail now if there are no credentials, rather than when Ready times out.
        kube.kubectl(CONTEXT, "get", "secret", "aws-creds", "-n", "crossplane-system")

        self.addCleanup(kube.kubectl, CONTEXT, "delete", "-f", str(PLATFORM), "--ignore-not-found", "--timeout=30m")
        kube.kubectl(CONTEXT, "apply", "-f", str(PLATFORM))

        cluster = kube.poll(
            lambda: kube.get(CONTEXT, "inferencecluster", CLUSTER),
            until=lambda cluster: kube.condition(cluster, "Ready") == "True",
            timeout=60 * 60,
            interval=30,
        )
        self.assertEqual(kube.condition(cluster, "Ready"), "True", kube.describe(cluster))

        kube.kubectl(CONTEXT, "delete", "inferencecluster", CLUSTER, "--timeout=30m")
        self.assertEqual(
            kube.kubectl(CONTEXT, "get", "inferencecluster", CLUSTER, "--ignore-not-found", "-o", "name"), ""
        )
