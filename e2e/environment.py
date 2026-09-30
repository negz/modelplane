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

"""Bring up, and tear down, the kind clusters the end-to-end tests run on.

There are two clusters. The control plane runs Crossplane and the Configuration,
and `crossplane project run` manages it. The workload cluster runs the serving
stack, both gateways and the models, and the control plane registers it as
InferenceCluster local with source: Existing.

pytest --bring-up calls up_control_plane and up_workload before the first test
that needs each. Run this module to do either without running any tests:

    python -m e2e.environment up [--no-apply]
    python -m e2e.environment down
"""

import argparse
import ipaddress
import json
import logging
import os
import pathlib
import shlex
import subprocess
import tempfile

log = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).resolve().parent.parent

CONTROL_PLANE = "modelplane-e2e-local"
WORKLOAD = "modelplane-e2e-workload"
CONTROL_PLANE_CONTEXT = f"kind-{CONTROL_PLANE}"
WORKLOAD_CONTEXT = f"kind-{WORKLOAD}"

# Pinned so the workload cluster has the DRA APIs the serving stack's NVIDIA DRA
# driver needs (resource.k8s.io, GA in k8s 1.34). The control plane needs no DRA,
# so its image doesn't matter. v1.34.2 or newer: older kubelets deadlock on an
# idle DRA connection (k/k#133934).
WORKLOAD_NODE_IMAGE = "kindest/node:v1.34.8@sha256:02722c2dedddcfc00febf5d27fbeb9b7b2c14294c82109ff4a85d89ac9ba3256"
DEADLOCKING_KUBELETS = ("v1.34.0", "v1.34.1")

METALLB_URL = "https://raw.githubusercontent.com/metallb/metallb/v0.14.8/config/manifests/metallb-native.yaml"
CROSSPLANE_VERSION = "2.4.0"

PLATFORM_MANIFESTS = ROOT / "e2e" / "manifests" / "platform"


class BringUpError(Exception):
    """The environment can't be brought up as asked."""


def up_control_plane() -> None:
    """Build and install Modelplane on a kind control plane, and finish its setup."""
    log.info("Building and running the control plane %s", CONTROL_PLANE)
    # No system PATH in the nix app, so a Docker config using credsStore
    # "desktop" would break package resolution. The provider packages are public.
    with tempfile.TemporaryDirectory() as docker_config:
        pathlib.Path(docker_config, "config.json").write_text("{}")
        # The lean control plane's narrowed MRAP goes in with --init-resources,
        # ahead of the providers, so the cloud providers stay dormant (safe-start
        # scales them to zero). prerequisites.yaml can't go the same way: it opens
        # with a comment-only YAML document, which `crossplane project run`
        # rejects and kubectl skips.
        run(
            "crossplane", "project", "run",
            f"--control-plane-name={CONTROL_PLANE}", "--cluster-admin", "--timeout=25m",
            f"--init-resources={ROOT / 'e2e' / 'lean-control-plane.yaml'}",
            f"--crossplane-version={CROSSPLANE_VERSION}",
            env={"DOCKER_CONFIG": docker_config},
        )  # fmt: skip

    # Finish the setup the install guide does by hand. The providers install
    # before prerequisites.yaml, and an ImageConfig binds only when a
    # ProviderRevision is created, so provider-helm would otherwise come up
    # without the RBAC it grants, and provider-kubernetes without
    # --sanitize-secrets. Point each at its DeploymentRuntimeConfig.
    log.info("Applying the prerequisites and provider runtime configs")
    kubectl(
        CONTROL_PLANE_CONTEXT, "apply", f"--filename={ROOT / 'docs' / 'manifests' / 'install' / 'prerequisites.yaml'}"
    )
    for provider, runtime_config in (
        ("upbound-provider-helm", "provider-helm-modelplane"),
        ("upbound-provider-kubernetes", "provider-kubernetes-modelplane"),
    ):
        patch = {
            "spec": {
                "runtimeConfigRef": {
                    "apiVersion": "pkg.crossplane.io/v1beta1",
                    "kind": "DeploymentRuntimeConfig",
                    "name": runtime_config,
                }
            }
        }
        kubectl(
            CONTROL_PLANE_CONTEXT,
            "patch", f"provider.pkg.crossplane.io/{provider}", "--type=merge", f"--patch={json.dumps(patch)}",
        )  # fmt: skip


def up_workload(*, apply_platform: bool = True) -> None:
    """Create the workload cluster, register it with the control plane, and apply the platform manifests."""
    create_workload_cluster()

    # Both kind clusters share one Docker network. MetalLB hands out
    # LoadBalancer addresses from it, and the control plane must route to them,
    # so the pool has to sit inside the network's actual subnet. That's usually
    # 172.18.0.0/16, but kind moves to 172.19 and beyond when another Docker
    # network holds 172.18. The pool needs room for two Services, one per
    # gateway.
    prefix = kind_subnet_prefix()
    log.info("Installing MetalLB on the workload cluster, with pool %s.255.100-149", prefix)
    kubectl(WORKLOAD_CONTEXT, "apply", f"--filename={METALLB_URL}")
    kubectl(WORKLOAD_CONTEXT, "rollout", "status", "--namespace=metallb-system", "deploy/controller", "--timeout=180s")
    pool = [
        {
            "apiVersion": "metallb.io/v1beta1",
            "kind": "IPAddressPool",
            "metadata": {"name": "kind-pool", "namespace": "metallb-system"},
            "spec": {"addresses": [f"{prefix}.255.100-{prefix}.255.149"]},
        },
        {
            "apiVersion": "metallb.io/v1beta1",
            "kind": "L2Advertisement",
            "metadata": {"name": "kind-l2", "namespace": "metallb-system"},
            "spec": {"ipAddressPools": ["kind-pool"]},
        },
    ]
    kubectl(
        WORKLOAD_CONTEXT, "apply", "--filename=-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": pool})
    )

    # Fake DRA GPUs, so a `claim: DRA` engine's ResourceClaim binds on this
    # GPU-less node. Without a DRA driver the claim stays Pending and the engine
    # never schedules, and the fleet scheduler rejects an engine whose only
    # device is Synthetic.
    log.info("Installing dra-example-driver (fake GPUs) on the workload cluster")
    kubectl(WORKLOAD_CONTEXT, "apply", f"--filename={ROOT / 'e2e' / 'dra-example-driver.yaml'}")
    kubectl(
        WORKLOAD_CONTEXT,
        "rollout", "status", "--namespace=dra-example-driver", "ds/dra-example-driver-kubeletplugin", "--timeout=120s",
    )  # fmt: skip

    # Modelplane doesn't label a BYO cluster's nodes. The gpu-synthetic pool
    # selects on this.
    log.info("Labelling the workload node for pool gpu-synthetic")
    kubectl(
        WORKLOAD_CONTEXT,
        "label",
        "node",
        f"{WORKLOAD}-control-plane",
        "modelplane.ai/pool=gpu-synthetic",
        "--overwrite",
    )

    # InferenceCluster local reads this kubeconfig to reach the workload
    # cluster. --internal gives the address on the kind network, which the
    # control plane's provider pods can reach and 127.0.0.1 isn't.
    log.info("Registering the workload cluster's kubeconfig with the control plane")
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "local-cluster-kubeconfig", "namespace": "modelplane-system"},
        "stringData": {"kubeconfig": output("kind", "get", "kubeconfig", "--internal", f"--name={WORKLOAD}")},
    }
    kubectl(CONTROL_PLANE_CONTEXT, "apply", "--filename=-", stdin=json.dumps(secret))

    if not apply_platform:
        log.info("Skipping the platform manifests; apply them from %s", PLATFORM_MANIFESTS)
        return
    log.info("Applying the platform manifests")
    kubectl(CONTROL_PLANE_CONTEXT, "apply", f"--filename={PLATFORM_MANIFESTS}")


def create_workload_cluster() -> None:
    """Create the workload cluster, or reuse one running the pinned Kubernetes minor version."""
    if WORKLOAD not in output("kind", "get", "clusters").split():
        log.info("Creating workload cluster %s", WORKLOAD)
        run("kind", "create", "cluster", f"--name={WORKLOAD}", f"--image={WORKLOAD_NODE_IMAGE}")
        return

    # An older cluster lacks the DRA APIs, and would fail the run confusingly
    # later.
    try:
        version = output(
            "kubectl", f"--context={WORKLOAD_CONTEXT}",
            "get", "nodes", "--output=jsonpath={.items[0].status.nodeInfo.kubeletVersion}",
        )  # fmt: skip
    except subprocess.CalledProcessError:
        version = "unreachable"
    if version in DEADLOCKING_KUBELETS:
        raise BringUpError(
            f"workload cluster {WORKLOAD} is {version}, whose kubelet deadlocks on an idle DRA connection "
            "(fixed in v1.34.2); recreate it with: nix run .#e2e -- --clean"
        )
    if not version.startswith("v1.34."):
        raise BringUpError(
            f"workload cluster {WORKLOAD} is {version}, but v1.34 is required for the DRA APIs; "
            "recreate it with: nix run .#e2e -- --clean"
        )
    log.info("Reusing workload cluster %s (%s)", WORKLOAD, version)


def kind_subnet_prefix() -> str:
    """Return the first two octets of the kind Docker network's IPv4 subnet."""
    subnets = output(
        "docker", "network", "inspect", "kind", "--format={{range .IPAM.Config}}{{println .Subnet}}{{end}}"
    )
    for subnet in subnets.split():
        network = ipaddress.ip_network(subnet)
        if isinstance(network, ipaddress.IPv4Network):
            log.info("kind Docker subnet is %s", network)
            return ".".join(str(network.network_address).split(".")[:2])
    raise BringUpError(f"could not find an IPv4 subnet on the kind Docker network: {subnets!r}")


def down() -> None:
    """Delete both clusters, and the control plane's local registry."""
    # Delete both clusters whatever project stop returns: it can exit 0 without
    # removing the cluster. It's here for the local registry it also manages.
    for cmd in (
        ("crossplane", "project", "stop", f"--control-plane-name={CONTROL_PLANE}"),
        ("kind", "delete", "cluster", f"--name={CONTROL_PLANE}"),
        ("kind", "delete", "cluster", f"--name={WORKLOAD}"),
        ("docker", "rm", "--force", f"{CONTROL_PLANE}-registry"),
    ):
        try:
            run(*cmd)
        except subprocess.CalledProcessError as e:
            log.warning("%s", e)


def kubectl(context: str, *args: str, stdin: str | None = None) -> None:
    """Run kubectl against a cluster, logging what it prints."""
    run("kubectl", f"--context={context}", *args, stdin=stdin)


def run(*cmd: str, stdin: str | None = None, env: dict[str, str] | None = None) -> None:
    """Run a command from the repository root, logging what it prints as it prints it.

    Bring-up takes most of a CI run, so its progress is logged live rather than
    captured and shown only if it fails.
    """
    log.info("$ %s", shlex.join(cmd))
    with subprocess.Popen(
        cmd,
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as proc:
        # Popen opens every pipe it's asked for, so neither of these is None.
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(stdin or "")
        proc.stdin.close()
        for line in proc.stdout:
            log.info("  %s", line.rstrip())
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def output(*cmd: str) -> str:
    """Run a command, and return what it wrote to stdout."""
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def main() -> None:
    """Bring the environment up or down without running any tests."""
    parser = argparse.ArgumentParser(prog="python -m e2e.environment", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up", help="bring up both clusters and install Modelplane")
    up.add_argument("--no-apply", action="store_true", help="skip the platform manifests, to apply them by hand")
    commands.add_parser("down", help="delete both clusters")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.command == "down":
        down()
        return
    up_control_plane()
    up_workload(apply_platform=not args.no_apply)


if __name__ == "__main__":
    main()
