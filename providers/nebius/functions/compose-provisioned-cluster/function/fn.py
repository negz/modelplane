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

"""Provision a Nebius mk8s cluster for a ProvisionedCluster.

A ProvisionedCluster is the contract Modelplane composes for every
InferenceCluster whose source is Provisioned, and this function is the
Nebius provider's implementation of it: it composes a NebiusCluster from
the NebiusClusterConfig and NebiusNodeClasses the contract references, a
NebiusDeviceStack once the cluster is reachable, and reports back what
Modelplane needs to use the cluster. See design/cluster-providers.md.

It also serves the Compositions of NebiusClusterConfig and
NebiusNodeClass, which compose nothing.
"""

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from modelplane_sdk import provisioning
from models.ai.modelplane.infrastructure.provisionedcluster import v1alpha1
from models.ai.modelplane.nebius.nebiuscluster import v1alpha1 as nebiusv1alpha1
from models.ai.modelplane.nebius.nebiusclusterconfig import v1alpha1 as configv1alpha1
from models.ai.modelplane.nebius.nebiusnodeclass import v1alpha1 as ncv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The kinds this provider owns, as <kind>.<group>.
_CONFIG = "NebiusClusterConfig.nebius.modelplane.ai"
_NODE_CLASS = "NebiusNodeClass.nebius.modelplane.ai"

_CLUSTER = "nebius-cluster"

# The managed resource kinds compose-nebius-cluster composes, which
# provisioning.compose_activation activates. provider-helm and
# provider-kubernetes are omitted: this package's own policy keeps those
# active.
_ACTIVATE = [
    "filesystems.compute.nebius.m.upbound.io",
    "gpuclusters.compute.nebius.m.upbound.io",
    "clusters.mk8s.nebius.m.upbound.io",
    "nodegroups.mk8s.nebius.m.upbound.io",
    "networks.vpc.nebius.m.upbound.io",
    "subnets.vpc.nebius.m.upbound.io",
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

        config = configv1alpha1.NebiusClusterConfig.model_validate(inputs.config)
        node_classes = {name: ncv1alpha1.NebiusNodeClass.model_validate(nc) for name, nc in inputs.node_classes.items()}
        accelerated = {name: nc.spec.accelerator is not None for name, nc in node_classes.items()}
        if not provisioning.check_roles(self.req, self.rsp, accelerated):
            return
        pools = self.node_pools(config, node_classes)
        if pools is None:
            return

        if provisioning.compose_activation(self.req, self.rsp, _ACTIVATE) or _CLUSTER in self.req.observed.resources:
            self.compose_cluster(config, pools)

        # The kubeconfig carries only the cluster's endpoint and CA
        # certificate, because Nebius authenticates every client through
        # Nebius IAM. The NebiusCluster also reports the credentials Secret
        # of its Nebius ProviderConfig, in that Secret's own namespace, which
        # the device stack and Modelplane authenticate as.
        secrets = provisioning.cluster_secrets(self.req, self.rsp, _CLUSTER)
        provisioning.compose_device_stack(
            self.req,
            self.rsp,
            api_version="nebius.modelplane.ai/v1alpha1",
            kind="NebiusDeviceStack",
            cluster_secrets=secrets,
        )
        provisioning.write_status(self.req, self.rsp, cluster=_CLUSTER, cluster_secrets=secrets)

    def node_pools(
        self, config: configv1alpha1.NebiusClusterConfig, node_classes: dict[str, ncv1alpha1.NebiusNodeClass]
    ) -> list[nebiusv1alpha1.NodePool] | None:
        """The contract's node pools as NebiusCluster pools.

        An Accelerator pool's node class names its GPU, which labels the
        pool's nodes, and a CPU pool's names none. A pool whose node class
        sets fabric InfiniBand joins the fabric the config names. Returns
        None, having failed the reconcile naming every offending pool, if a
        node class wants InfiniBand from a config that names no fabric.
        """
        pools: list[nebiusv1alpha1.NodePool] = []
        invalid: list[str] = []
        for pool in self.xr.spec.nodePools:
            nc = node_classes[pool.nodeClassRef.name]
            if nc.spec.fabric == "InfiniBand" and not config.spec.infinibandFabric:
                invalid.append(
                    f"node pool {pool.name}'s NebiusNodeClass {pool.nodeClassRef.name} wants InfiniBand, "
                    f"but NebiusClusterConfig {self.xr.spec.configRef.name} names no infinibandFabric"
                )
                continue
            node_pool = nebiusv1alpha1.NodePool(
                name=pool.name,
                role="GPU" if pool.role == "Accelerator" else "CPU",
                platform=nc.spec.platform,
                preset=nc.spec.preset,
                diskSizeGb=nc.spec.diskSizeGb,
                nodeCount=pool.nodeCount,
            )
            # driversPreset rides the gpu block: a CPU platform preinstalls no
            # NVIDIA stack.
            if nc.spec.accelerator:
                node_pool.gpu = nebiusv1alpha1.Gpu(
                    acceleratorType=nc.spec.accelerator.type,
                    driversPreset=nc.spec.driversPreset,
                )
            # mk8s node groups are either fixed size or autoscaled, never
            # both, so the bounds are set only when given. resource.update
            # serializes with exclude_unset, and assigning None would emit
            # maxNodeCount: null.
            if pool.maxNodeCount is not None:
                node_pool.maxNodeCount = pool.maxNodeCount
            if pool.minNodeCount is not None:
                node_pool.minNodeCount = pool.minNodeCount
            # Likewise set only for a pool on a fabric, so a pool on standard
            # VPC networking stays free of fabric: None.
            if nc.spec.fabric == "InfiniBand":
                node_pool.fabric = nebiusv1alpha1.Fabric(
                    type="InfiniBand",
                    infiniband=nebiusv1alpha1.Infiniband(fabric=config.spec.infinibandFabric),
                )
            pools.append(node_pool)
        if invalid:
            response.fatal(self.rsp, "; ".join(invalid))
            return None
        return pools

    def compose_cluster(self, config: configv1alpha1.NebiusClusterConfig, pools: list[nebiusv1alpha1.NodePool]) -> None:
        """Compose the NebiusCluster. compose-nebius-cluster adds the system pool."""
        spec = nebiusv1alpha1.Spec(nodePools=pools)
        # Left unset unless specified, so the NebiusCluster's default version
        # applies.
        if config.spec.kubernetesVersion is not None:
            spec.kubernetesVersion = config.spec.kubernetesVersion
        if config.spec.credentials:
            spec.credentials = nebiusv1alpha1.Credentials(
                type=config.spec.credentials.type,
                name=config.spec.credentials.name,
            )
        resource.update(
            self.rsp.desired.resources[_CLUSTER],
            nebiusv1alpha1.NebiusCluster(
                metadata=metav1.ObjectMeta(name=provisioning.name(self.req), namespace=provisioning.NAMESPACE),
                spec=spec,
            ),
        )
