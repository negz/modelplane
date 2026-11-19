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

"""The names of the ProvisionedCluster contract. See design/cluster-providers.md.

Modelplane composes a ProvisionedCluster, and selects the provider's
Composition for it by the two labels, which name the cluster config kind the
provider provisions from. It reads the two annotations off that Composition:
the optional parts of the contract the provider implements, and the DRA
drivers whose device stacks it installs, each a comma-separated list.
The provider reports the cluster's secrets in status.secrets.
"""

from typing import Final

API_VERSION: Final = "infrastructure.modelplane.ai/v1alpha1"
KIND: Final = "ProvisionedCluster"

LABEL_CONFIG_GROUP: Final = "infrastructure.modelplane.ai/config-group"
LABEL_CONFIG_KIND: Final = "infrastructure.modelplane.ai/config-kind"
ANNOTATION_CAPABILITIES: Final = "infrastructure.modelplane.ai/capabilities"
ANNOTATION_DRA_DRIVERS: Final = "infrastructure.modelplane.ai/dra-drivers"

# The optional parts of the contract. A pool's zones need Zones, and its
# reservationRef needs CapacityReservations.
CAPABILITY_ZONES: Final = "Zones"
CAPABILITY_CAPACITY_RESERVATIONS: Final = "CapacityReservations"
CAPABILITIES: Final = frozenset({CAPABILITY_ZONES, CAPABILITY_CAPACITY_RESERVATIONS})

# The type of the status.secrets entry naming the cluster's kubeconfig. Any
# other type is a provider-kubernetes identity type, which the ProviderConfigs
# for the cluster stamp verbatim as their identity.type.
SECRET_TYPE_KUBECONFIG: Final = "Kubeconfig"

# A node pool's role.
ROLE_ACCELERATOR: Final = "Accelerator"
ROLE_CPU: Final = "CPU"

# The provider labels every pool's nodes LABEL_POOL=<pool name>.
LABEL_POOL: Final = "modelplane.ai/pool"
