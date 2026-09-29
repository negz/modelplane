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

"""Scenario B: a ModelDeployment and ModelService serve, stop serving, and delete.

It deploys its own copy of the mock model, in a namespace of its own.
"""

import json
import pathlib
import unittest

import gateway
import kube

NAMESPACE = "bakeoff-unittest"
NAME = "mock-unittest"

MANIFESTS = pathlib.Path(__file__).parents[1] / "manifests"

AUTHORIZED = {"authorization": f"Bearer {gateway.CALLER_KEY}"}


def model_deployment() -> dict:
    """e2e/manifests/40-model-deployment.yaml, renamed into NAMESPACE.

    The suite is stdlib-only, so kubectl parses the YAML.
    """
    manifest = MANIFESTS / "40-model-deployment.yaml"
    md = json.loads(kube.kubectl(kube.CONTROL_PLANE, "create", "--dry-run=client", "-o", "json", "-f", str(manifest)))
    md["metadata"] = {"name": NAME, "namespace": NAMESPACE}
    return md


# Selects the ModelDeployment the way e2e/manifests/50-model-service.yaml does.
MODEL_SERVICE = {
    "apiVersion": "modelplane.ai/v1alpha1",
    "kind": "ModelService",
    "metadata": {"name": NAME, "namespace": NAMESPACE},
    "spec": {"endpoints": [{"name": NAME, "selector": {"matchLabels": {"modelplane.ai/deployment": NAME}}}]},
}


def teardown() -> None:
    """Delete everything the scenario creates, whether or not it exists.

    This leaves the namespace Modelplane mirrors NAMESPACE into on the workload
    cluster. The InferenceCluster composes that, and never deletes it.
    """
    kube.kubectl(kube.CONTROL_PLANE, "delete", "namespace", NAMESPACE, "--ignore-not-found", "--timeout=5m")


class Lifecycle(unittest.TestCase):
    """B1 to B3, which unittest runs in name order. Each acts on what the one before left.

    A check that finds nothing left to act on skips, rather than failing for
    the same reason as the check before it.
    """

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        # Clear out anything an interrupted run left behind.
        teardown()
        cls.addClassCleanup(teardown)

        kube.apply(kube.CONTROL_PLANE, {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
        kube.apply(kube.CONTROL_PLANE, model_deployment())
        kube.apply(kube.CONTROL_PLANE, MODEL_SERVICE)
        cls.client = gateway.start_client(NAMESPACE)
        cls.openai = kube.get(kube.CONTROL_PLANE, "inferencegateway", "local")["status"]["endpoints"]["openAI"]

    def state(self) -> str:
        """A failure message: the state of everything in the namespace."""
        kinds = "modelservice,modeldeployment,modelreplica,modelendpoint"
        return kube.describe(*kube.items(kube.CONTROL_PLANE, kinds, "-n", NAMESPACE))

    def test_b1_model_service_serves(self) -> None:
        """B1: The ModelService becomes RoutingReady, then serves as bakeoff-unittest/mock-unittest."""
        ms = kube.poll(
            lambda: kube.get(kube.CONTROL_PLANE, "modelservice", NAME, "-n", NAMESPACE),
            until=lambda ms: kube.condition(ms, "RoutingReady") == "True",
            timeout=600,
        )
        self.assertEqual(kube.condition(ms, "RoutingReady"), "True", self.state())

        # The route is ready once it's applied, which can be a moment before
        # the gateway serves it.
        got = kube.poll(
            lambda: self.client.post(
                f"{self.openai}/chat/completions", gateway.chat(ms["status"]["model"]), AUTHORIZED
            ),
            until=lambda got: got.status == 200,
            timeout=60,
        )
        self.assertEqual(got.status, 200, got.body)
        self.assertEqual(got.json()["model"], f"{NAMESPACE}/{NAME}", got.body)

    def test_b2_deleted_model_service_stops_serving(self) -> None:
        """B2: Within 5 minutes of deleting the ModelService, requests naming it stop returning 200."""
        model = kube.get(kube.CONTROL_PLANE, "modelservice", NAME, "-n", NAMESPACE)["status"]["model"]
        url = f"{self.openai}/chat/completions"
        got = self.client.post(url, gateway.chat(model), AUTHORIZED)
        if got.status != 200:
            self.skipTest(f"{model} isn't serving (HTTP {got.status}), so it can't stop")

        kube.kubectl(kube.CONTROL_PLANE, "delete", "-n", NAMESPACE, "modelservice", NAME, "--wait=false")
        got = kube.poll(
            lambda: self.client.post(url, gateway.chat(model), AUTHORIZED),
            until=lambda got: got.status != 200,
            timeout=300,
        )
        self.assertNotEqual(got.status, 200, f"{model} still serves 5 minutes after its ModelService was deleted")

    def test_b3_foreground_delete_removes_replicas_endpoints_and_pods(self) -> None:
        """B3: A foreground delete of the ModelDeployment removes its ModelReplicas, ModelEndpoints and engine pods."""
        replicas = kube.replicas(NAMESPACE, NAME)
        pods = kube.engine_pods(replicas) if replicas else []
        if not pods:
            self.skipTest(f"ModelDeployment {NAME} has no engine pods to delete")

        kube.kubectl(
            kube.CONTROL_PLANE,
            "delete",
            "-n",
            NAMESPACE,
            "modeldeployment",
            NAME,
            "--cascade=foreground",
            "--wait=false",
        )

        # The namespace holds only this ModelDeployment and what it composed,
        # so everything of these kinds in it should go.
        left = kube.poll(
            lambda: kube.items(kube.CONTROL_PLANE, "modeldeployment,modelreplica,modelendpoint", "-n", NAMESPACE),
            until=lambda objs: not objs,
            timeout=300,
        )
        self.assertEqual([f"{o['kind']}/{o['metadata']['name']}" for o in left], [], self.state())

        # The engine pods can still be terminating once the ModelDeployment is gone.
        left = kube.poll(lambda: kube.engine_pods(replicas), until=lambda pods: not pods, timeout=180)
        self.assertEqual([f"{p['metadata']['namespace']}/{p['metadata']['name']}" for p in left], [])
