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

"""Names for the ModelRoutes a ModelService composes.

One ModelRoute per gateway serving the service, so a name carries both the
service and the gateway. A service's name and a gateway's name can each be 253
characters, so a joined name can overflow and has to be shortened
deterministically.
"""

import hashlib

# Kubernetes object names are limited to 253 characters.
_MAX_NAME = 253

# Enough of a SHA-256 to make a collision between two truncated names
# implausible, while leaving most of the readable prefix intact.
_HASH_LEN = 8


def _fit(name: str) -> str:
    """Shorten a name to fit Kubernetes' limit, keeping it unique and stable.

    Truncating alone would map two long names onto one object, so a hash of the
    whole name replaces the tail. The hash is of the untruncated name, so the
    result is stable across reconciles.
    """
    if len(name) <= _MAX_NAME:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:_HASH_LEN]
    return f"{name[: _MAX_NAME - _HASH_LEN - 1]}-{digest}"


def model_route(service: str, gateway: str) -> str:
    """The ModelRoute a ModelService composes for one gateway serving it."""
    return _fit(f"{service}-{gateway}")


def model(namespace: str, service: str) -> str:
    """The name a caller passes as the request's model.

    A contract with compose-model-route, which matches an AIGatewayRoute on this
    value; this function only reports it in status. Not fitted: it's a request
    body value, not an object name.
    """
    return f"{namespace}/{service}"
