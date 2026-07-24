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

"""NVIDIA Dynamo backend: one DynamoGraphDeployment for a delegated replica.

Unlike native and llm-d, this backend doesn't implement the per-engine
Backend protocol (backends/base.py): Dynamo's operator owns the whole
replica's serving shape from a single object, a DynamoGraphDeployment (DGD,
nvidia.com/v1beta1). build_replica composes one DGD spanning every engine of a
delegated replica - a synthesized frontend component (Dynamo's OpenAI entry +
KV-aware router) plus one component per engine - and fn.py calls it directly
for a delegated replica, bypassing the per-engine dispatch entirely.

Modelplane passes each engine's image/command/args through verbatim into its
component's podTemplate, same as every other backend: it injects no engine
flags (no --disaggregation-mode, no --kv-transfer-config). The user writes
those, exactly as they write a Leader's and Worker's commands today. Modelplane
only sets the component's type (from the engine's phase), its multinode gang
size (from the member's dynamo.nodes), GPU binding (a DRA ResourceClaimTemplate,
the same one every other backend uses), and the cache mount.
"""

from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

# DynamoGraphDeployment API version and kind. v1beta1 is served (converted to
# the operator's v1alpha1 storage version by its webhook); Modelplane authors
# v1beta1, the current schema.
_API_VERSION = "nvidia.com/v1beta1"
_KIND = "DynamoGraphDeployment"

# The operator merges its defaults (image, command, env, ports, probes,
# resources, volume mounts) into the podTemplate container named "main",
# auto-generating one if absent. Every component's engine container must use
# this name - unlike the "engine" name every other backend's containers use.
_CONTAINER_MAIN = "main"

# Component name and type for the synthesized frontend: Dynamo's OpenAI-
# compatible entrypoint and KV-aware router. One per replica, regardless of
# engine count.
_FRONTEND_NAME = "frontend"
_FRONTEND_TYPE = "frontend"
_FRONTEND_COMMAND = ["python3", "-m", "dynamo.frontend"]

# Component type per engine phase; an engine with no phase (Unified serving)
# gets the generic "worker" type.
_TYPE_WORKER = "worker"
_PHASE_TYPE = {"Prefill": "prefill", "Decode": "decode"}

# dynamo.<backend> module names recognized for spec.backendFramework, read
# from the engine's own command. Best-effort: backendFramework is optional, so
# a command Modelplane doesn't recognize just omits it.
_BACKEND_FRAMEWORKS = ("vllm", "sglang", "trtllm")


def _dynamo_module_framework(command: list[str] | None) -> str | None:
    """The dynamo.<backend> framework named in a command, or None.

    Peeks for a "dynamo.<framework>" token (e.g. "dynamo.vllm") the way the
    frontend and worker components' commands both use to select their runtime.
    Best-effort, like every other engine-command inspection in Modelplane
    (e.g. routing.py's KV block size peek): the command is the user's, so a
    shape Modelplane doesn't recognize just skips backendFramework.
    """
    for token in command or []:
        if not token.startswith("dynamo."):
            continue
        framework = token.removeprefix("dynamo.")
        if framework in _BACKEND_FRAMEWORKS:
            return framework
    return None


def _component_type(engine: v1alpha1.Engine) -> str:
    """A component's Dynamo type: prefill/decode from the engine's phase, or
    the generic worker type for Unified serving (no phase)."""
    return _PHASE_TYPE.get(engine.phase or "", _TYPE_WORKER)


class DynamoBackend:
    def build_replica(
        self,
        replica: v1alpha1.ModelReplica,
        provider_config: str,
        serving_label: str,
    ) -> dict[str, k8sobjv1alpha1.Object]:
        """Compose one DynamoGraphDeployment spanning every engine of a
        delegated replica, plus a ResourceClaimTemplate per claiming member.

        Named after the replica (serving_label), so the operator's frontend
        Service - "{dgd-name}-frontend" - lines up with the name
        routing.apply_delegated's HTTPRoute targets.
        """
        name = serving_label
        # The frontend's image and spec.backendFramework are both derived from
        # the SAME engine (the first), not independently from whichever engine
        # happens to match first - every Dynamo runtime image ships every
        # backend's entrypoint, so a single representative engine speaks for
        # the whole replica for both.
        first_member = base.engine_member(replica.spec.engines[0], base.ROLE_DELEGATED)
        assert first_member is not None
        components = [self._frontend_component(first_member)]
        backend_framework = _dynamo_module_framework(base.engine_container(first_member).command)

        composed: dict[str, k8sobjv1alpha1.Object] = {}
        for engine in replica.spec.engines:
            member = base.engine_member(engine, base.ROLE_DELEGATED)
            # select_backend/is_delegated route only Delegated engines here,
            # and the XRD requires exactly one Delegated member per such
            # engine, so it's always present.
            assert member is not None
            components.append(self._engine_component(replica, engine, member))
            if member.deviceRequests:
                composed[base.claim_key(engine, member)] = base.resource_claim_template(
                    replica, engine, member, provider_config
                )

        spec: dict = {"components": components}
        if backend_framework:
            spec["backendFramework"] = backend_framework

        dgd = {
            "apiVersion": _API_VERSION,
            "kind": _KIND,
            "metadata": {"name": name, "namespace": base.REMOTE_NAMESPACE},
            "spec": spec,
        }
        composed[base.DGD_KEY] = base.wrap_object(provider_config, dgd, cel_query=base.DYNAMO_READY_CEL)
        return composed

    def _frontend_component(self, first_member: v1alpha1.Member) -> dict:
        """The synthesized frontend component: Dynamo's OpenAI entry + router.

        Reuses the first engine's runtime image - the frontend ships in the
        same Dynamo runtime image as every backend (vLLM, SGLang, TensorRT-LLM)
        - so no separate image is required from the user. It fronts every
        engine of the replica, not just the first; the image only needs to
        contain the dynamo.frontend entrypoint, which every Dynamo runtime
        image does. Every Delegated engine of a replica is expected to run the
        same runtime image (Modelplane doesn't validate this, the same way it
        doesn't validate cross-engine consistency for any other backend); a
        replica that genuinely mixes runtime images gets the first engine's.

        No pinning, GPU toleration, or resources: unlike an engine component,
        the frontend is CPU-only and doesn't need to land on a GPU pool.
        """
        image = base.engine_container(first_member).image
        return {
            "name": _FRONTEND_NAME,
            "type": _FRONTEND_TYPE,
            "replicas": 1,
            "podTemplate": {
                "spec": {
                    "containers": [
                        {
                            "name": _CONTAINER_MAIN,
                            "image": image,
                            "command": _FRONTEND_COMMAND,
                            "args": ["--http-port", str(base.ENGINE_PORT)],
                        }
                    ],
                },
            },
        }

    def _engine_component(
        self,
        replica: v1alpha1.ModelReplica,
        engine: v1alpha1.Engine,
        member: v1alpha1.Member,
    ) -> dict:
        """One engine's component: its container, cache mount, GPU claim,
        placement, and (if dynamo.nodes > 1) multinode gang size.

        The member's image/command/args are passed through verbatim - Dynamo
        (not Modelplane) synthesizes the per-node distributed launch for a
        multinode component from this single command, the same way it
        synthesizes the frontend's KV-aware routing from the engine's phase.
        """
        engine_container = base.engine_container(member)
        cache_volumes, cache_volume_mounts = base.cache_mounts(replica)
        args = base.apply_cache_args(list(engine_container.args or []), replica, engine_container)

        container: dict = {
            "name": _CONTAINER_MAIN,
            "image": engine_container.image,
            # vLLM tensor parallelism needs a large /dev/shm, same as every
            # other backend.
            "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}, *cache_volume_mounts],
        }
        if engine_container.command:
            container["command"] = list(engine_container.command)
        if args:
            container["args"] = args
        if engine_container.env:
            container["env"] = [e.model_dump(exclude_none=True) for e in engine_container.env]
        # GPUs bind via DRA: the container references the pod-level claim
        # backed by the member's ResourceClaimTemplate, same as every other
        # backend. No resources.limits nvidia.com/gpu - Dynamo's podTemplate is
        # a plain PodTemplateSpec, so DRA resourceClaims/resources.claims pass
        # through unmodified.
        if member.deviceRequests:
            container["resources"] = base.engine_resources()

        pod_spec: dict = {
            "containers": [container],
            "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}, *cache_volumes],
        }
        base.place_pod(pod_spec, replica, engine, member)
        tmpl = member.template
        assert tmpl.spec is not None
        if tmpl.spec.imagePullSecrets:
            pod_spec["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in tmpl.spec.imagePullSecrets]

        component: dict = {
            "name": engine.name,
            "type": _component_type(engine),
            "replicas": int(engine.copies or 1),
            "podTemplate": {"spec": pod_spec},
        }
        nodes = int(member.dynamo.nodes) if member.dynamo and member.dynamo.nodes else 1
        # multinode.nodeCount has a CRD minimum of 2; omit the field entirely
        # for a single-node component rather than send nodeCount: 1.
        if nodes > 1:
            component["multinode"] = {"nodeCount": nodes}
        return component
