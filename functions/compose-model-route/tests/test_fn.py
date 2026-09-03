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

"""Tests for the compose-model-route function."""

import base64
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn, names
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.inferencegateway import v1alpha1 as igv1alpha1
from models.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1
from models.ai.modelplane.modelroute import v1alpha1

_NS = "ml-team"
_SVC = "assistant"
_MODEL = f"{_NS}/{_SVC}"
_GW = "eu"
_CLUSTER_CA = "-----BEGIN CERTIFICATE-----\ncluster\n-----END CERTIFICATE-----\n"
_CLIENT_CA = "-----BEGIN CERTIFICATE-----\nclient\n-----END CERTIFICATE-----\n"


def _entry(label: str, *, priority: int | None = None, weight: int | None = None) -> v1alpha1.Endpoint:
    kwargs = {}
    if priority is not None:
        kwargs["priority"] = priority
    if weight is not None:
        kwargs["weight"] = weight
    return v1alpha1.Endpoint(selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": label}), **kwargs)


def _route_xr(entries: list[v1alpha1.Endpoint], *, gateway: str = _GW) -> dict:
    xr = v1alpha1.ModelRoute(
        apiVersion="modelplane.ai/v1alpha1",
        kind="ModelRoute",
        metadata={"name": f"{_SVC}-{gateway}", "namespace": _NS},
        spec=v1alpha1.Spec(gatewayName=gateway, serviceName=_SVC, endpoints=entries),
    )
    return xr.model_dump(exclude_none=True, mode="json", by_alias=True)


def _endpoint(
    name: str,
    *,
    origin: str,
    model: str | None = None,
    credential: str | None = None,
    ready: bool = True,
    composed: bool = False,
) -> dict:
    ep = mev1alpha1.ModelEndpoint(
        apiVersion="modelplane.ai/v1alpha1",
        kind="ModelEndpoint",
        metadata={"name": name, "namespace": _NS},
        spec=mev1alpha1.Spec(
            origin=origin,
            **({"model": model} if model else {}),
            **({"credentialRef": mev1alpha1.CredentialRef(name=credential)} if credential else {}),
        ),
    )
    d = ep.model_dump(exclude_none=True, mode="json", by_alias=True)
    if composed:
        d["metadata"]["labels"] = {"modelplane.ai/cluster": "gw-eu", "modelplane.ai/deployment": "d"}
    d["status"] = {
        "conditions": [
            {
                "type": "EndpointReady",
                "status": "True" if ready else "False",
                "reason": "EndpointUsable" if ready else "CredentialMissing",
                "lastTransitionTime": "2026-06-08T00:00:00Z",
            }
        ]
    }
    return d


def _gateway(*, client_ca: str | None = _CLIENT_CA, address: str | None = "203.0.113.1") -> dict:
    gw = igv1alpha1.InferenceGateway(
        apiVersion="modelplane.ai/v1alpha1",
        kind="InferenceGateway",
        metadata={"name": _GW},
        spec=igv1alpha1.Spec(clusterName="gw-eu", hostname="eu.example.com"),
    )
    d = gw.model_dump(exclude_none=True, mode="json", by_alias=True)
    status: dict = {}
    if address:
        status["address"] = address
    if client_ca:
        status["clientCACertificate"] = client_ca
    if status:
        d["status"] = status
    return d


def _cluster(name: str, *, provider_config: str | None = "gw-eu-pc", ca: str | None = _CLUSTER_CA) -> dict:
    c = icv1alpha1.InferenceCluster(
        apiVersion="modelplane.ai/v1alpha1",
        kind="InferenceCluster",
        metadata={"name": name},
        spec=icv1alpha1.Spec(
            cluster=icv1alpha1.Cluster(
                source="Existing",
                existing=icv1alpha1.Existing(
                    secretRef=icv1alpha1.SecretRef(name=f"{name}-kubeconfig", key="kubeconfig")
                ),
            )
        ),
    )
    d = c.model_dump(exclude_none=True, mode="json", by_alias=True)
    status: dict = {}
    if provider_config:
        status["providerConfigRef"] = {"name": provider_config}
    if ca:
        status["gateway"] = {"caCertificate": ca}
    if status:
        d["status"] = status
    return d


def _secret(name: str, data: dict[str, str]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": _NS},
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }


def _required(**resources) -> dict:  # noqa: ANN003
    return {
        name: fnv1.Resources(items=[fnv1.Resource(resource=resource.dict_to_struct(r)) for r in items])
        for name, items in resources.items()
    }


def _manifest(rsp: fnv1.RunFunctionResponse, key: str) -> dict:
    return resource.struct_to_dict(rsp.desired.resources[key].resource)["spec"]["forProvider"]["manifest"]


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_gateway_pki_not_published(self) -> None:
        """A gateway whose client PKI hasn't issued has no client certificate for
        a composed backend to name, so nothing is composed yet."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr([_entry("d")])))),
            required_resources=_required(
                gateway=[_gateway(client_ca=None)],
                clusters=[_cluster("gw-eu")],
                **{"endpoints-0": [_endpoint("self", origin="https://gw-eu.example.com", composed=True)]},
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(len(got.desired.resources), 0, "composes nothing")
        self.assertEqual(next(iter(got.conditions)).reason, fn.CONDITION_REASON_WAITING_FOR_GATEWAY)
        # A pass that composes nothing must mark the XR not-ready, or it
        # aggregates to trivially ready and the service thinks it's serving.
        self.assertEqual(got.desired.composite.ready, fnv1.READY_FALSE)

    async def test_no_ready_endpoints(self) -> None:
        """None of the selected endpoints is ready, so no route is composed."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr([_entry("d")])))),
            required_resources=_required(
                gateway=[_gateway()],
                clusters=[_cluster("gw-eu")],
                **{"endpoints-0": [_endpoint("self", origin="https://gw-eu.example.com", composed=True, ready=False)]},
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(len(got.desired.resources), 0)
        self.assertEqual(next(iter(got.conditions)).reason, fn.CONDITION_REASON_NO_ENDPOINTS)

    async def test_composed_endpoint_dropped_without_cluster_ca(self) -> None:
        """A composed endpoint whose cluster has withdrawn its CA is left out
        rather than composing a backend that can't complete a handshake."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr([_entry("d")])))),
            required_resources=_required(
                gateway=[_gateway()],
                clusters=[_cluster("gw-eu", ca=None)],
                **{"endpoints-0": [_endpoint("self", origin="https://gw-eu.example.com", composed=True)]},
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(len(got.desired.resources), 0)
        self.assertEqual(next(iter(got.conditions)).reason, fn.CONDITION_REASON_NO_ENDPOINTS)

    async def test_compose(self) -> None:
        """A composed self-hosted endpoint at priority 0 and a third-party
        provider at priority 1: backends, credential, cluster CA and route."""
        entries = [_entry("d", priority=0), _entry("together", priority=1)]
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr(entries)))),
            required_resources=_required(
                gateway=[_gateway()],
                clusters=[_cluster("gw-eu")],
                **{
                    "endpoints-0": [
                        _endpoint("self", origin="https://gw-eu.example.com", model="d", composed=True),
                    ],
                    "endpoints-1": [
                        _endpoint(
                            "together",
                            origin="https://api.together.xyz",
                            model="Qwen/Qwen2.5",
                            credential="together-key",
                        ),
                    ],
                    "credential-together": [_secret("together-key", {"apiKey": "sk-tog"})],
                },
            ),
        )
        got = await self.runner.RunFunction(req, None)

        self.assertEqual(
            set(got.desired.resources),
            {
                "backend-self",
                "aibackend-self",
                "backend-together",
                "aibackend-together",
                "credential-together",
                "credpolicy-together",
                "cluster-ca-gw-eu",
                "route",
            },
        )

        route = _manifest(got, "route")
        rule = route["spec"]["rules"][0]
        self.assertEqual(
            rule["backendRefs"],
            [
                {"name": "ml-team-assistant-self", "weight": 1, "priority": 0, "modelNameOverride": "d"},
                {"name": "ml-team-assistant-together", "weight": 1, "priority": 1, "modelNameOverride": "Qwen/Qwen2.5"},
            ],
        )
        self.assertEqual(
            route["spec"]["rules"][0]["matches"][0]["headers"][0],
            {"type": "Exact", "name": "x-ai-eg-model", "value": _MODEL},
        )

        # The composed backend pins its cluster's CA and presents the client
        # certificate; the third-party one uses the system trust store.
        self.assertEqual(
            _manifest(got, "backend-self")["spec"]["tls"],
            {
                "caCertificateRefs": [{"kind": "ConfigMap", "group": "", "name": "cluster-ca-gw-eu"}],
                "sni": "gw-eu.example.com",
                "clientCertificateRef": {"kind": "Secret", "group": "", "name": "fleet-gateway-client"},
            },
        )
        self.assertEqual(
            _manifest(got, "backend-together")["spec"]["tls"],
            {"wellKnownCACertificates": "System", "sni": "api.together.xyz"},
        )

        # The caller header is stripped only for the backend we don't operate.
        self.assertNotIn("headerMutation", _manifest(got, "aibackend-self")["spec"])
        self.assertEqual(
            _manifest(got, "aibackend-together")["spec"]["headerMutation"],
            {"remove": ["x-modelplane-caller"]},
        )

        # The credential is republished under the fixed apiKey key.
        self.assertEqual(
            _manifest(got, "credential-together")["data"],
            {"apiKey": base64.b64encode(b"sk-tog").decode()},
        )
        self.assertEqual(_manifest(got, "cluster-ca-gw-eu")["data"], {"ca.crt": _CLUSTER_CA})

    async def test_status_reports_address_and_counts(self) -> None:
        entries = [_entry("d")]
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr(entries)))),
            required_resources=_required(
                gateway=[_gateway(address="203.0.113.9")],
                clusters=[_cluster("gw-eu")],
                **{"endpoints-0": [_endpoint("self", origin="https://gw-eu.example.com", composed=True)]},
            ),
        )
        got = await self.runner.RunFunction(req, None)
        status = resource.struct_to_dict(got.desired.composite.resource)["status"]
        self.assertEqual(
            status,
            {
                "model": _MODEL,
                "address": "203.0.113.9",
                "hostname": "eu.example.com",
                "endpoints": {"total": 1, "ready": 1},
            },
        )

    async def test_weights_spread_within_a_tier(self) -> None:
        """An entry's weight is written once but applied per backend, so it is
        spread over however many endpoints the entry matched, and the ratio
        between entries survives."""
        entries = [_entry("big", weight=90), _entry("small", weight=10)]
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr(entries)))),
            required_resources=_required(
                gateway=[_gateway()],
                clusters=[_cluster("gw-eu")],
                **{
                    "endpoints-0": [_endpoint(f"big-{i}", origin=f"https://big-{i}.example.com") for i in range(3)],
                    "endpoints-1": [_endpoint("small-0", origin="https://small-0.example.com")],
                },
            ),
        )
        got = await self.runner.RunFunction(req, None)
        weights = [r["weight"] for r in _manifest(got, "route")["spec"]["rules"][0]["backendRefs"]]
        # 90 over three endpoints is 30 each, 10 over one is 10, reduced by the
        # greatest common divisor to the smallest equivalent integers.
        self.assertEqual(weights, [3, 3, 3, 1])
        self.assertEqual(sum(weights[:3]) / sum(weights), 0.9)

    async def test_a_weight_below_its_endpoint_count_floors_no_endpoint(self) -> None:
        """Weight 1 over five endpoints must not floor any of them to 0, which
        would drop them from the load assignment rather than sharing traffic."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr([_entry("many", weight=1)])))
            ),
            required_resources=_required(
                gateway=[_gateway()],
                clusters=[_cluster("gw-eu")],
                **{"endpoints-0": [_endpoint(f"many-{i}", origin=f"https://many-{i}.example.com") for i in range(5)]},
            ),
        )
        got = await self.runner.RunFunction(req, None)
        weights = [r["weight"] for r in _manifest(got, "route")["spec"]["rules"][0]["backendRefs"]]
        self.assertEqual(weights, [1, 1, 1, 1, 1])

    async def test_an_endpoint_matched_twice_belongs_to_the_first_entry(self) -> None:
        """A canary entry and a catch-all entry must not both weight one
        endpoint; the first that matches it wins."""
        entries = [_entry("kimi", priority=0), _entry("kimi", priority=1)]
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr(entries)))),
            required_resources=_required(
                gateway=[_gateway()],
                clusters=[_cluster("gw-eu")],
                **{
                    "endpoints-0": [_endpoint("kimi-a", origin="https://a.example.com")],
                    "endpoints-1": [_endpoint("kimi-a", origin="https://a.example.com")],
                },
            ),
        )
        got = await self.runner.RunFunction(req, None)
        refs = _manifest(got, "route")["spec"]["rules"][0]["backendRefs"]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["priority"], 0)
        self.assertEqual(
            resource.struct_to_dict(got.desired.composite.resource)["status"]["endpoints"],
            {"total": 1, "ready": 1},
        )

    async def test_priorities_are_renumbered_without_gaps(self) -> None:
        """A ModelService's priorities are an ordering; Envoy's are levels it
        walks from 0. A user writing 0 and 5, or a tier gone unready during a
        roll, would otherwise leave gaps in what Envoy gets."""
        entries = [_entry("a", priority=0), _entry("b", priority=5), _entry("c", priority=9)]
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr(entries)))),
            required_resources=_required(
                gateway=[_gateway()],
                clusters=[_cluster("gw-eu")],
                **{
                    # The middle tier has no ready endpoint, so it drops out and
                    # must not leave a hole behind it.
                    "endpoints-0": [_endpoint("a-0", origin="https://a.example.com")],
                    "endpoints-1": [_endpoint("b-0", origin="https://b.example.com", ready=False)],
                    "endpoints-2": [_endpoint("c-0", origin="https://c.example.com")],
                },
            ),
        )
        got = await self.runner.RunFunction(req, None)
        refs = _manifest(got, "route")["spec"]["rules"][0]["backendRefs"]
        self.assertEqual([r["priority"] for r in refs], [0, 1], "two tiers survive, renumbered 0 and 1")

    async def test_a_credential_secret_missing_its_key_is_dropped(self) -> None:
        """A Secret that exists but lacks the named key would otherwise reach the
        provider as an empty credential, so the endpoint is left out."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr([_entry("a")])))),
            required_resources=_required(
                gateway=[_gateway()],
                clusters=[_cluster("gw-eu")],
                **{
                    "endpoints-0": [_endpoint("wrongkey", origin="https://a.example.com", credential="k")],
                    "credential-wrongkey": [_secret("k", {"token": "sk-1"})],
                },
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(next(iter(got.conditions)).reason, fn.CONDITION_REASON_NO_ENDPOINTS)
        self.assertEqual(len(got.desired.resources), 0)


class TestNames(unittest.TestCase):
    def test_long_names_stay_within_the_limit_and_stay_unique(self) -> None:
        """A namespace, a service and an endpoint name can each be 253
        characters, so a joined name can overflow. Truncating alone would map
        two names onto one object."""
        long = "e" * 250
        a = names.backend("n" * 250, "s" * 250, long + "a")
        b = names.backend("n" * 250, "s" * 250, long + "b")
        self.assertLessEqual(len(a), 253)
        self.assertLessEqual(len(b), 253)
        self.assertNotEqual(a, b)
        self.assertEqual(a, names.backend("n" * 250, "s" * 250, long + "a"), "stable across calls")

    def test_the_model_a_caller_names_is_never_hashed(self) -> None:
        """It's a value in a request body, not an object name. Shortening it
        would make what a caller types depend on a hash."""
        self.assertEqual(names.model("n" * 250, "s" * 250), f"{'n' * 250}/{'s' * 250}")
