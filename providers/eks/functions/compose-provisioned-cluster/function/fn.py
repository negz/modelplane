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

"""Provision an EKS cluster for a ProvisionedCluster.

A ProvisionedCluster is the contract Modelplane composes for every
InferenceCluster whose source is Provisioned, and this function is the
EKS provider's implementation of it: it composes an EKSCluster from the
EKSClusterConfig, EKSNodeClasses and EKSCapacityReservations the
contract references, an EKSDeviceStack once the cluster is reachable,
and reports back what Modelplane needs to use the cluster. See
design/cluster-providers.md.

It also serves the Compositions of EKSClusterConfig, EKSNodeClass and
EKSCapacityReservation, which compose nothing.
"""

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from modelplane_sdk import contract, provisioning
from models.ai.modelplane.eks.ekscapacityreservation import v1alpha1 as crv1alpha1
from models.ai.modelplane.eks.ekscluster import v1alpha1 as eksv1alpha1
from models.ai.modelplane.eks.eksclusterconfig import v1alpha1 as configv1alpha1
from models.ai.modelplane.eks.eksnodeclass import v1alpha1 as ncv1alpha1
from models.ai.modelplane.infrastructure.provisionedcluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The kinds this provider owns, as <kind>.<group>.
_CONFIG = "EKSClusterConfig.eks.modelplane.ai"
_NODE_CLASS = "EKSNodeClass.eks.modelplane.ai"
_RESERVATION = "EKSCapacityReservation.eks.modelplane.ai"

_CLUSTER = "eks-cluster"

# The node class fabric that attaches EFA interfaces to a pool's nodes.
_FABRIC_EFA = "EFA"

# The managed resource kinds compose-eks-cluster composes, which
# provisioning.compose_activation activates. provider-helm and
# provider-kubernetes are omitted: this package's own policy keeps those
# active.
_ACTIVATE = [
    "eips.ec2.aws.m.upbound.io",
    "internetgateways.ec2.aws.m.upbound.io",
    "launchtemplates.ec2.aws.m.upbound.io",
    "natgateways.ec2.aws.m.upbound.io",
    "routes.ec2.aws.m.upbound.io",
    "routetables.ec2.aws.m.upbound.io",
    "routetableassociations.ec2.aws.m.upbound.io",
    "securitygroups.ec2.aws.m.upbound.io",
    "securitygroupegressrules.ec2.aws.m.upbound.io",
    "securitygroupingressrules.ec2.aws.m.upbound.io",
    "subnets.ec2.aws.m.upbound.io",
    "vpcs.ec2.aws.m.upbound.io",
    "filesystems.efs.aws.m.upbound.io",
    "mounttargets.efs.aws.m.upbound.io",
    "addons.eks.aws.m.upbound.io",
    "clusters.eks.aws.m.upbound.io",
    "clusterauths.eks.aws.m.upbound.io",
    "nodegroups.eks.aws.m.upbound.io",
    "podidentityassociations.eks.aws.m.upbound.io",
    "policies.iam.aws.m.upbound.io",
    "roles.iam.aws.m.upbound.io",
    "rolepolicyattachments.iam.aws.m.upbound.io",
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
        inputs = provisioning.resolve_inputs(
            self.req, self.rsp, config=_CONFIG, node_class=_NODE_CLASS, reservation=_RESERVATION
        )
        if inputs is None:
            return
        provisioning.compose_input_usages(self.req, self.rsp, inputs)

        config = configv1alpha1.EKSClusterConfig.model_validate(inputs.config)
        node_classes = {name: ncv1alpha1.EKSNodeClass.model_validate(nc) for name, nc in inputs.node_classes.items()}
        reservations = {
            name: crv1alpha1.EKSCapacityReservation.model_validate(r) for name, r in inputs.reservations.items()
        }
        accelerated = {name: nc.spec.accelerator is not None for name, nc in node_classes.items()}
        if not provisioning.check_roles(self.req, self.rsp, accelerated):
            return
        pools = self.node_pools(config, node_classes, reservations)
        if pools is None:
            return

        if provisioning.compose_activation(self.req, self.rsp, _ACTIVATE) or _CLUSTER in self.req.observed.resources:
            self.compose_cluster(config, pools)

        secrets = provisioning.cluster_secrets(self.req, self.rsp, _CLUSTER)
        provisioning.compose_device_stack(
            self.req,
            self.rsp,
            api_version="eks.modelplane.ai/v1alpha1",
            kind="EKSDeviceStack",
            cluster_secrets=secrets,
        )
        provisioning.write_status(self.req, self.rsp, cluster=_CLUSTER, cluster_secrets=secrets)

    def node_pools(
        self,
        config: configv1alpha1.EKSClusterConfig,
        node_classes: dict[str, ncv1alpha1.EKSNodeClass],
        reservations: dict[str, crv1alpha1.EKSCapacityReservation],
    ) -> list[eksv1alpha1.NodePool] | None:
        """The contract's node pools as EKSCluster pools.

        An Accelerator pool's node class names its GPU, which labels the
        pool's nodes, and a CPU pool's names none. A pool's reservation
        must be in the cluster's region, and an Accelerator pool must name
        its zones, because not every zone has every instance type. Returns
        None, having failed the reconcile naming every offending pool, if a
        pool breaks either rule.
        """
        pools: list[eksv1alpha1.NodePool] = []
        invalid: list[str] = []
        for pool in self.xr.spec.nodePools:
            nc = node_classes[pool.nodeClassRef.name]
            if pool.role == contract.ROLE_ACCELERATOR and not pool.zones:
                invalid.append(f"node pool {pool.name} must name its zones, because EKS GPU pools need them")
                continue
            node_pool = eksv1alpha1.NodePool(
                name=pool.name,
                role="GPU" if pool.role == "Accelerator" else "CPU",
                instanceType=nc.spec.instanceType,
                diskSizeGb=nc.spec.diskSizeGb,
                nodeCount=pool.nodeCount,
            )
            if nc.spec.accelerator:
                node_pool.gpu = eksv1alpha1.Gpu(acceleratorType=nc.spec.accelerator.type)
            # Set only when given. resource.update serializes with
            # exclude_unset, and assigning None would emit maxNodeCount: null.
            if pool.maxNodeCount is not None:
                node_pool.maxNodeCount = pool.maxNodeCount
            if pool.minNodeCount is not None:
                node_pool.minNodeCount = pool.minNodeCount
            if pool.zones:
                node_pool.zones = [eksv1alpha1.Zone(z.root) for z in pool.zones]
            if pool.reservationRef:
                r = reservations[pool.reservationRef.name]
                if r.spec.region != config.spec.region:
                    invalid.append(
                        f"EKSCapacityReservation {pool.reservationRef.name} is in region {r.spec.region}, "
                        f"but the cluster is in {config.spec.region}"
                    )
                    continue
                node_pool.capacityReservation = eksv1alpha1.CapacityReservation(type=r.spec.type, id=r.spec.id)
            # Likewise set only for EFA, so a pool's EKSCluster spec stays
            # free of fabric: None.
            if nc.spec.fabric == _FABRIC_EFA:
                node_pool.fabric = nc.spec.fabric
            pools.append(node_pool)
        if invalid:
            response.fatal(self.rsp, "; ".join(invalid))
            return None
        return pools

    def compose_cluster(self, config: configv1alpha1.EKSClusterConfig, pools: list[eksv1alpha1.NodePool]) -> None:
        """Compose the EKSCluster. compose-eks-cluster adds the system pool."""
        spec = eksv1alpha1.Spec(region=config.spec.region, nodePools=pools)
        # Left unset unless specified, so the EKSCluster's default version
        # applies.
        if config.spec.kubernetesVersion is not None:
            spec.kubernetesVersion = config.spec.kubernetesVersion
        if config.spec.credentials:
            spec.credentials = eksv1alpha1.Credentials(
                type=config.spec.credentials.type,
                name=config.spec.credentials.name,
            )
        resource.update(
            self.rsp.desired.resources[_CLUSTER],
            eksv1alpha1.EKSCluster(
                metadata=metav1.ObjectMeta(name=provisioning.name(self.req), namespace=provisioning.NAMESPACE),
                spec=spec,
            ),
        )
