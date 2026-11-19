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

"""Provision a CKS cluster for a ProvisionedCluster.

A ProvisionedCluster is the contract Modelplane composes for every
InferenceCluster whose source is Provisioned, and this function is the
CoreWeave provider's implementation of it: it composes a CoreWeaveCluster
from the CoreWeaveClusterConfig and CoreWeaveNodeClasses the contract
references, a CoreWeaveDeviceStack once the cluster is reachable, and
reports back what Modelplane needs to use the cluster. See
design/cluster-providers.md.

It also serves the Compositions of CoreWeaveClusterConfig and
CoreWeaveNodeClass, which compose nothing.
"""

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from modelplane_sdk import provisioning
from models.ai.modelplane.coreweave.coreweavecluster import v1alpha1 as cwv1alpha1
from models.ai.modelplane.coreweave.coreweaveclusterconfig import v1alpha1 as configv1alpha1
from models.ai.modelplane.coreweave.coreweavenodeclass import v1alpha1 as ncv1alpha1
from models.ai.modelplane.infrastructure.provisionedcluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The kinds this provider owns, as <kind>.<group>.
_CONFIG = "CoreWeaveClusterConfig.coreweave.modelplane.ai"
_NODE_CLASS = "CoreWeaveNodeClass.coreweave.modelplane.ai"

_CLUSTER = "coreweave-cluster"

# The managed resource kinds compose-coreweave-cluster composes, which
# provisioning.compose_activation activates. provider-helm and
# provider-kubernetes are omitted: this package's own policy keeps those
# active.
_ACTIVATE = [
    "clusters.cks.coreweave.m.upbound.io",
    "vpcs.networking.coreweave.m.upbound.io",
]

# What the cluster's gateways need on their LoadBalancer Service. CKS gives a
# LoadBalancer Service a private address without this annotation.
_GATEWAY_SERVICE_ANNOTATIONS = {"service.beta.kubernetes.io/coreweave-load-balancer-type": "public"}

# CKS runs its own DCGM exporter in cw-exporters, which can't attribute
# DRA-allocated GPUs to pods. The device stack installs one that can, and
# collecting both would collect every GPU series twice.
_IGNORED_NAMESPACES = ["cw-exporters"]


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self) -> None:
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext | None
    ) -> fnv1.RunFunctionResponse:  # ty: ignore[invalid-method-override]  # the generated grpc servicer base is untyped
        """Run the function."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")

        rsp = response.to(req)
        if provisioning.is_contract(req):
            Composer(req, rsp).compose()
        return rsp


class Composer:
    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ProvisionedCluster.model_validate(resource.struct_to_dict(req.observed.composite.resource))

    def compose(self) -> None:
        provisioning.require_inputs(self.req, self.rsp)
        inputs = provisioning.resolve_inputs(self.req, self.rsp, config=_CONFIG, node_class=_NODE_CLASS)
        if inputs is None:
            return
        provisioning.compose_input_usages(self.req, self.rsp, inputs)

        config = configv1alpha1.CoreWeaveClusterConfig.model_validate(inputs.config)
        node_classes = {
            name: ncv1alpha1.CoreWeaveNodeClass.model_validate(nc) for name, nc in inputs.node_classes.items()
        }
        accelerated = {name: nc.spec.accelerator is not None for name, nc in node_classes.items()}
        if not provisioning.check_roles(self.req, self.rsp, accelerated):
            return
        pools = self.node_pools(node_classes)

        if provisioning.compose_activation(self.req, self.rsp, _ACTIVATE) or _CLUSTER in self.req.observed.resources:
            self.compose_cluster(config, pools)

        secrets = provisioning.cluster_secrets(self.req, self.rsp, _CLUSTER)
        provisioning.compose_device_stack(
            self.req,
            self.rsp,
            api_version="coreweave.modelplane.ai/v1alpha1",
            kind="CoreWeaveDeviceStack",
            cluster_secrets=secrets,
        )
        provisioning.write_status(
            self.req,
            self.rsp,
            cluster=_CLUSTER,
            cluster_secrets=secrets,
            extra={
                "gateway": {"service": {"annotations": _GATEWAY_SERVICE_ANNOTATIONS}},
                "telemetry": {"ignoredNamespaces": _IGNORED_NAMESPACES},
            },
        )

    def node_pools(self, node_classes: dict[str, ncv1alpha1.CoreWeaveNodeClass]) -> list[cwv1alpha1.NodePool]:
        """The contract's node pools as CoreWeaveCluster pools.

        An Accelerator pool's node class names its GPU, which labels the
        pool's nodes, and a CPU pool's names none.
        """
        pools: list[cwv1alpha1.NodePool] = []
        for pool in self.xr.spec.nodePools:
            nc = node_classes[pool.nodeClassRef.name]
            # Fabric isn't mapped, because CKS instance types with InfiniBand
            # include it.
            node_pool = cwv1alpha1.NodePool(
                name=pool.name,
                role="GPU" if pool.role == "Accelerator" else "CPU",
                instanceType=nc.spec.instanceType,
                nodeCount=pool.nodeCount,
            )
            if nc.spec.accelerator:
                node_pool.gpu = cwv1alpha1.Gpu(acceleratorType=nc.spec.accelerator.type)
            # Set only when given. resource.update serializes with
            # exclude_unset, and assigning None would emit maxNodeCount: null.
            if pool.maxNodeCount is not None:
                node_pool.maxNodeCount = pool.maxNodeCount
            if pool.minNodeCount is not None:
                node_pool.minNodeCount = pool.minNodeCount
            pools.append(node_pool)
        return pools

    def compose_cluster(self, config: configv1alpha1.CoreWeaveClusterConfig, pools: list[cwv1alpha1.NodePool]) -> None:
        """Compose the CoreWeaveCluster. compose-coreweave-cluster adds the system pool."""
        spec = cwv1alpha1.Spec(zone=config.spec.zone, nodePools=pools)
        # Left unset unless specified, so the CoreWeaveCluster's default
        # version applies.
        if config.spec.kubernetesVersion is not None:
            spec.kubernetesVersion = config.spec.kubernetesVersion
        if config.spec.credentials:
            spec.credentials = cwv1alpha1.Credentials(
                type=config.spec.credentials.type,
                name=config.spec.credentials.name,
            )
        resource.update(
            self.rsp.desired.resources[_CLUSTER],
            cwv1alpha1.CoreWeaveCluster(
                metadata=metav1.ObjectMeta(name=provisioning.name(self.req), namespace=provisioning.NAMESPACE),
                spec=spec,
            ),
        )
