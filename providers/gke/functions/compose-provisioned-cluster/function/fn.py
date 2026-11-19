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

"""Provision a GKE cluster for a ProvisionedCluster.

A ProvisionedCluster is the contract Modelplane composes for every
InferenceCluster whose source is Provisioned, and this function is the
GKE provider's implementation of it: it composes a GKECluster from the
GKEClusterConfig, GKENodeClasses and GKECapacityReservations the
contract references, a GKEDeviceStack once the cluster is reachable, and
reports back what Modelplane needs to use the cluster. See
design/cluster-providers.md.

It also serves the Compositions of GKEClusterConfig, GKENodeClass and
GKECapacityReservation, which compose nothing.
"""

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from modelplane_sdk import contract, provisioning
from models.ai.modelplane.gke.gkecapacityreservation import v1alpha1 as crv1alpha1
from models.ai.modelplane.gke.gkecluster import v1alpha1 as gkev1alpha1
from models.ai.modelplane.gke.gkeclusterconfig import v1alpha1 as configv1alpha1
from models.ai.modelplane.gke.gkenodeclass import v1alpha1 as ncv1alpha1
from models.ai.modelplane.infrastructure.provisionedcluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The kinds this provider owns, as <kind>.<group>.
_CONFIG = "GKEClusterConfig.gke.modelplane.ai"
_NODE_CLASS = "GKENodeClass.gke.modelplane.ai"
_RESERVATION = "GKECapacityReservation.gke.modelplane.ai"

_CLUSTER = "gke-cluster"

# The managed resource kinds compose-gke-cluster composes, which
# provisioning.compose_activation activates. provider-helm and
# provider-kubernetes are omitted: this package's own policy keeps those
# active.
_ACTIVATE = [
    "projectiammembers.cloudplatform.gcp.m.upbound.io",
    "projectservices.cloudplatform.gcp.m.upbound.io",
    "serviceaccounts.cloudplatform.gcp.m.upbound.io",
    "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
    "networks.compute.gcp.m.upbound.io",
    "subnetworks.compute.gcp.m.upbound.io",
    "clusters.container.gcp.m.upbound.io",
    "nodepools.container.gcp.m.upbound.io",
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

        config = configv1alpha1.GKEClusterConfig.model_validate(inputs.config)
        node_classes = {name: ncv1alpha1.GKENodeClass.model_validate(nc) for name, nc in inputs.node_classes.items()}
        reservations = {
            name: crv1alpha1.GKECapacityReservation.model_validate(r) for name, r in inputs.reservations.items()
        }
        accelerated = {name: nc.spec.accelerator is not None for name, nc in node_classes.items()}
        if not provisioning.check_roles(self.req, self.rsp, accelerated):
            return
        pools = self.node_pools(node_classes, reservations)
        if pools is None:
            return

        if provisioning.compose_activation(self.req, self.rsp, _ACTIVATE) or _CLUSTER in self.req.observed.resources:
            self.compose_cluster(config, pools)

        secrets = provisioning.cluster_secrets(self.req, self.rsp, _CLUSTER)
        provisioning.compose_device_stack(
            self.req,
            self.rsp,
            api_version="gke.modelplane.ai/v1alpha1",
            kind="GKEDeviceStack",
            cluster_secrets=secrets,
        )
        provisioning.write_status(self.req, self.rsp, cluster=_CLUSTER, cluster_secrets=secrets)

    def node_pools(
        self,
        node_classes: dict[str, ncv1alpha1.GKENodeClass],
        reservations: dict[str, crv1alpha1.GKECapacityReservation],
    ) -> list[gkev1alpha1.NodePool] | None:
        """The contract's node pools as GKECluster pools.

        An Accelerator pool's node class names the GPU to attach, and a CPU
        pool's names none. An Accelerator pool must name its zones, because
        not every zone in a region has every GPU. A pool backed by a
        reservation must be restricted to exactly the reservation's zone,
        because a specific reservation only has capacity there. Returns None,
        having failed the reconcile naming every offending pool, if a pool
        breaks either rule.
        """
        pools: list[gkev1alpha1.NodePool] = []
        invalid: list[str] = []
        for pool in self.xr.spec.nodePools:
            nc = node_classes[pool.nodeClassRef.name]
            zones = [z.root for z in pool.zones or []]
            if pool.role == contract.ROLE_ACCELERATOR and not zones:
                invalid.append(f"node pool {pool.name} must name its zones, because GKE GPU pools need them")
                continue
            node_pool = gkev1alpha1.NodePool(
                name=pool.name,
                role="GPU" if pool.role == "Accelerator" else "CPU",
                machineType=nc.spec.machineType,
                diskSizeGb=nc.spec.diskSizeGb,
                nodeCount=pool.nodeCount,
            )
            if nc.spec.accelerator:
                node_pool.gpu = gkev1alpha1.Gpu(
                    acceleratorType=nc.spec.accelerator.type,
                    acceleratorCount=nc.spec.accelerator.count,
                )
            # Set only when given. resource.update serializes with
            # exclude_unset, and assigning None would emit maxNodeCount: null,
            # where the GKECluster's own defaults should apply.
            if pool.maxNodeCount is not None:
                node_pool.maxNodeCount = pool.maxNodeCount
            if pool.minNodeCount is not None:
                node_pool.minNodeCount = pool.minNodeCount
            # GKE requires a non-empty list when zones are set, so an empty
            # list stays out of the GKECluster spec.
            if zones:
                node_pool.zones = [gkev1alpha1.Zone(z) for z in zones]
            if pool.reservationRef:
                r = reservations[pool.reservationRef.name]
                if zones != [r.spec.zone]:
                    invalid.append(
                        f"node pool {pool.name} must set zones to [{r.spec.zone}] "
                        f"to use GKECapacityReservation {pool.reservationRef.name}"
                    )
                    continue
                node_pool.reservation = gkev1alpha1.Reservation(name=r.spec.name)
                if r.spec.project:
                    node_pool.reservation.project = r.spec.project
            pools.append(node_pool)
        if invalid:
            response.fatal(self.rsp, "; ".join(invalid))
            return None
        return pools

    def compose_cluster(self, config: configv1alpha1.GKEClusterConfig, pools: list[gkev1alpha1.NodePool]) -> None:
        """Compose the GKECluster. compose-gke-cluster adds the system pool and derives the project."""
        spec = gkev1alpha1.Spec(region=config.spec.region, nodePools=pools)
        # Left unset unless specified, so the GKECluster's default version
        # applies.
        if config.spec.kubernetesVersion is not None:
            spec.kubernetesVersion = config.spec.kubernetesVersion
        if config.spec.credentials:
            spec.credentials = gkev1alpha1.Credentials(
                type=config.spec.credentials.type,
                name=config.spec.credentials.name,
            )
        resource.update(
            self.rsp.desired.resources[_CLUSTER],
            gkev1alpha1.GKECluster(
                metadata=metav1.ObjectMeta(name=provisioning.name(self.req), namespace=provisioning.NAMESPACE),
                spec=spec,
            ),
        )
