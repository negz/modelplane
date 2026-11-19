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

"""Provision a Civo Kubernetes cluster for a ProvisionedCluster.

A ProvisionedCluster is the contract Modelplane composes for every
InferenceCluster whose source is Provisioned, and this function is the
Civo provider's implementation of it: it composes a CivoCluster from the
CivoClusterConfig and CivoNodeClasses the contract references, a
CivoDeviceStack once the cluster is reachable, and reports back what
Modelplane needs to use the cluster. See design/cluster-providers.md.

It also serves the Compositions of CivoClusterConfig and CivoNodeClass,
which compose nothing.
"""

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from modelplane_sdk import provisioning
from models.ai.modelplane.civo.civocluster import v1alpha1 as civov1alpha1
from models.ai.modelplane.civo.civoclusterconfig import v1alpha1 as configv1alpha1
from models.ai.modelplane.civo.civonodeclass import v1alpha1 as ncv1alpha1
from models.ai.modelplane.infrastructure.provisionedcluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The kinds this provider owns, as <kind>.<group>.
_CONFIG = "CivoClusterConfig.civo.modelplane.ai"
_NODE_CLASS = "CivoNodeClass.civo.modelplane.ai"

_CLUSTER = "civo-cluster"

# The managed resource kinds compose-civo-cluster composes, which
# provisioning.compose_activation activates. provider-helm and
# provider-kubernetes are omitted: this package's own policy keeps those
# active.
_ACTIVATE = [
    "clusters.kubernetes.civo.m.upbound.io",
    "nodepools.kubernetes.civo.m.upbound.io",
    "networks.vpc.civo.m.upbound.io",
    "firewalls.vpc.civo.m.upbound.io",
]

# Civo's single-H100 sizes are lone H100 SXM modules whose NVLink links have
# no peer, so link bring-up fails and the driver never becomes ready unless
# it's loaded with NVLink disabled (per Civo's GPU configuration docs). A
# pool of one of these per node gets the CivoDeviceStack's NVLink-disabled
# driver. Multi-GPU pools keep NVLink between their GPUs.
_ACCELERATOR_H100 = "nvidia-h100"


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

        config = configv1alpha1.CivoClusterConfig.model_validate(inputs.config)
        node_classes = {name: ncv1alpha1.CivoNodeClass.model_validate(nc) for name, nc in inputs.node_classes.items()}
        accelerated = {name: nc.spec.accelerator is not None for name, nc in node_classes.items()}
        if not provisioning.check_roles(self.req, self.rsp, accelerated):
            return
        pools = self.node_pools(node_classes)

        if provisioning.compose_activation(self.req, self.rsp, _ACTIVATE) or _CLUSTER in self.req.observed.resources:
            self.compose_cluster(config, pools)

        secrets = provisioning.cluster_secrets(self.req, self.rsp, _CLUSTER)
        nvlink_disabled = self.nvlink_disabled_pools(node_classes)
        provisioning.compose_device_stack(
            self.req,
            self.rsp,
            api_version="civo.modelplane.ai/v1alpha1",
            kind="CivoDeviceStack",
            cluster_secrets=secrets,
            extra={"nvlinkDisabledPools": nvlink_disabled} if nvlink_disabled else None,
        )
        provisioning.write_status(self.req, self.rsp, cluster=_CLUSTER, cluster_secrets=secrets)

    def node_pools(self, node_classes: dict[str, ncv1alpha1.CivoNodeClass]) -> list[civov1alpha1.NodePool]:
        """The contract's node pools as CivoCluster pools.

        An Accelerator pool's node class names its GPU, which labels the
        pool's nodes, and a CPU pool's names none.
        """
        pools: list[civov1alpha1.NodePool] = []
        for pool in self.xr.spec.nodePools:
            nc = node_classes[pool.nodeClassRef.name]
            node_pool = civov1alpha1.NodePool(
                name=pool.name,
                role="GPU" if pool.role == "Accelerator" else "CPU",
                size=nc.spec.size,
                nodeCount=pool.nodeCount,
            )
            if nc.spec.accelerator:
                node_pool.gpu = civov1alpha1.Gpu(acceleratorType=nc.spec.accelerator.type)
            # Set only when given. resource.update serializes with
            # exclude_unset, and assigning None would emit maxNodeCount: null.
            if pool.maxNodeCount is not None:
                node_pool.maxNodeCount = pool.maxNodeCount
            if pool.minNodeCount is not None:
                node_pool.minNodeCount = pool.minNodeCount
            pools.append(node_pool)
        return pools

    def nvlink_disabled_pools(self, node_classes: dict[str, ncv1alpha1.CivoNodeClass]) -> list[str]:
        """The pools whose nodes each have one H100, which need NVLink disabled."""
        pools: list[str] = []
        for pool in self.xr.spec.nodePools:
            accelerator = node_classes[pool.nodeClassRef.name].spec.accelerator
            if accelerator and accelerator.type == _ACCELERATOR_H100 and accelerator.count == 1:
                pools.append(pool.name)
        return pools

    def compose_cluster(self, config: configv1alpha1.CivoClusterConfig, pools: list[civov1alpha1.NodePool]) -> None:
        """Compose the CivoCluster. compose-civo-cluster adds the system pool."""
        spec = civov1alpha1.Spec(region=config.spec.region, nodePools=pools)
        # Left unset unless specified, so compose-civo-cluster lets the
        # provider pick Civo's current default version.
        if config.spec.kubernetesVersion is not None:
            spec.kubernetesVersion = config.spec.kubernetesVersion
        if config.spec.credentials:
            spec.credentials = civov1alpha1.Credentials(
                type=config.spec.credentials.type,
                name=config.spec.credentials.name,
            )
        resource.update(
            self.rsp.desired.resources[_CLUSTER],
            civov1alpha1.CivoCluster(
                metadata=metav1.ObjectMeta(name=provisioning.name(self.req), namespace=provisioning.NAMESPACE),
                spec=spec,
            ),
        )
