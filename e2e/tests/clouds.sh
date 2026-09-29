#!/usr/bin/env bash
# Scenario D: clouds. Modelplane provisions a real EKS cluster and deletes it.
# It takes about half an hour and costs money, so test.sh runs it only when
# asked by name:
#
#   E2E_CP_CONTEXT=<context> e2e/test.sh clouds
#
# The kind control plane can't provision a cloud cluster (see README.md), so
# E2E_CP_CONTEXT names one that can, such as one `nix run .#run` brought up. It
# needs AWS credentials, configured the way the getting-started guide does: a
# Secret and the ClusterProviderConfig in
# docs/manifests/getting-started/clusterproviderconfig-aws.yaml. There's no
# workload kind cluster: the EKS cluster is the workload cluster.
# shellcheck source=../lib.sh
source "$(dirname "$0")/../lib.sh"

[ -n "${E2E_CP_CONTEXT:-}" ] || {
	echo "clouds: set E2E_CP_CONTEXT to a control plane with AWS credentials" >&2
	exit 1
}

# The getting-started platform, renamed so it can't collide with a maintainer's
# own copy of it.
platform="$(sed -e 's/l4-1x-g6/e2e-l4-1x-g6/g' -e 's/eks-us-east/e2e-eks-us-east/g' \
	"$(dirname "$0")/../../docs/manifests/getting-started/eks/platform.yaml")"
cluster=inferencecluster/e2e-eks-us-east

# Deleting is idempotent, so the trap runs it whether or not D1 got that far.
# Waiting keeps a cluster from outliving the run unnoticed.
trap 'kcp delete --ignore-not-found --timeout=30m -f - <<<"$platform"' EXIT
trap 'fail interrupted; exit 130' INT TERM

check D1 "An EKS InferenceCluster becomes Ready, then is deleted"
if ! kcp get clusterproviderconfig.aws.m.upbound.io default >/dev/null; then
	fail "control plane $CP has no AWS ClusterProviderConfig named default, so it can't provision EKS"
elif ! kcp apply -f - <<<"$platform"; then
	fail "couldn't apply the EKS platform"
elif ! kcp wait "$cluster" --for=condition=Ready --timeout=45m; then
	fail "$cluster wasn't Ready within 45 minutes"
	diag trace "$cluster"
else
	ready=$SECONDS
	if ! kcp delete "$cluster" --timeout=30m; then
		fail "$cluster was Ready after ${ready}s, but wasn't deleted within 30 minutes"
		diag trace "$cluster"
	else
		pass "Ready after ${ready}s, deleted $((SECONDS - ready))s later"
	fi
fi

finish
