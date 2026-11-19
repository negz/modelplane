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

"""Provision an AKS cluster for a ProvisionedCluster.

A ProvisionedCluster is the contract Modelplane composes for every
InferenceCluster whose source is Provisioned, and this function is the
AKS provider's implementation of it: it composes an AKSCluster from the
AKSClusterConfig and AKSNodeClasses the contract references, an
AKSDeviceStack once the cluster is reachable, and reports back what
Modelplane needs to use the cluster. See design/cluster-providers.md.

It also serves the Compositions of AKSClusterConfig and AKSNodeClass,
which compose nothing.
"""

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from modelplane_sdk import provisioning
from models.ai.modelplane.aks.akscluster import v1alpha1 as aksv1alpha1
from models.ai.modelplane.aks.aksclusterconfig import v1alpha1 as configv1alpha1
from models.ai.modelplane.aks.aksnodeclass import v1alpha1 as ncv1alpha1
from models.ai.modelplane.infrastructure.provisionedcluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The kinds this provider owns, as <kind>.<group>.
_CONFIG = "AKSClusterConfig.aks.modelplane.ai"
_NODE_CLASS = "AKSNodeClass.aks.modelplane.ai"

_CLUSTER = "aks-cluster"

# The node class fabric that puts a pool on InfiniBand.
_FABRIC_INFINIBAND = "InfiniBand"

# The managed resource kinds compose-aks-cluster composes, which
# provisioning.compose_activation activates. provider-helm and
# provider-kubernetes are omitted: this package's own policy keeps those
# active.
_ACTIVATE = [
    "kubernetesclusters.containerservice.azure.m.upbound.io",
    "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
    "subnets.network.azure.m.upbound.io",
    "virtualnetworks.network.azure.m.upbound.io",
    "resourcegroups.azure.m.upbound.io",
]


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

        config = configv1alpha1.AKSClusterConfig.model_validate(inputs.config)
        node_classes = {name: ncv1alpha1.AKSNodeClass.model_validate(nc) for name, nc in inputs.node_classes.items()}
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
            api_version="aks.modelplane.ai/v1alpha1",
            kind="AKSDeviceStack",
            cluster_secrets=secrets,
        )
        provisioning.write_status(self.req, self.rsp, cluster=_CLUSTER, cluster_secrets=secrets)

    def node_pools(self, node_classes: dict[str, ncv1alpha1.AKSNodeClass]) -> list[aksv1alpha1.NodePool]:
        """The contract's node pools as AKSCluster pools.

        An Accelerator pool's node class names its GPU, which labels the
        pool's nodes, and a CPU pool's names none.
        """
        pools: list[aksv1alpha1.NodePool] = []
        for pool in self.xr.spec.nodePools:
            nc = node_classes[pool.nodeClassRef.name]
            node_pool = aksv1alpha1.NodePool(
                name=pool.name,
                role="GPU" if pool.role == "Accelerator" else "CPU",
                vmSize=nc.spec.vmSize,
                diskSizeGb=nc.spec.diskSizeGb,
                nodeCount=pool.nodeCount,
            )
            if nc.spec.accelerator:
                node_pool.gpu = aksv1alpha1.Gpu(acceleratorType=nc.spec.accelerator.type)
            # Set only when given. resource.update serializes with
            # exclude_unset, and assigning None would emit maxNodeCount: null.
            if pool.maxNodeCount is not None:
                node_pool.maxNodeCount = pool.maxNodeCount
            if pool.minNodeCount is not None:
                node_pool.minNodeCount = pool.minNodeCount
            # Only set zones when the pool names some: the AKSCluster XRD
            # requires a non-empty list, and many GPU VM sizes aren't zonal.
            if pool.zones:
                node_pool.zones = [aksv1alpha1.Zone(z.root) for z in pool.zones]
            # Only set fabric when the class opts into one, so an on-demand
            # pool's AKSCluster spec stays free of fabric: None. Azure has no
            # user-selectable fabric identifier: a pool's VM Scale Set
            # placement group lands its nodes on one physical fabric.
            if nc.spec.fabric == _FABRIC_INFINIBAND:
                node_pool.fabric = _FABRIC_INFINIBAND
            pools.append(node_pool)
        return pools

    def compose_cluster(self, config: configv1alpha1.AKSClusterConfig, pools: list[aksv1alpha1.NodePool]) -> None:
        """Compose the AKSCluster. compose-aks-cluster adds the system pool."""
        spec = aksv1alpha1.Spec(location=config.spec.location, nodePools=pools)
        # Left unset unless specified, so the AKSCluster's default version
        # applies.
        if config.spec.kubernetesVersion is not None:
            spec.kubernetesVersion = config.spec.kubernetesVersion
        if config.spec.credentials:
            spec.credentials = aksv1alpha1.Credentials(
                type=config.spec.credentials.type,
                name=config.spec.credentials.name,
            )
        resource.update(
            self.rsp.desired.resources[_CLUSTER],
            aksv1alpha1.AKSCluster(
                metadata=metav1.ObjectMeta(name=provisioning.name(self.req), namespace=provisioning.NAMESPACE),
                spec=spec,
            ),
        )
