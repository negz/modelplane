#!/usr/bin/env bash
# Run the local/ Chainsaw e2e tests against the two kind clusters run.sh brings
# up. Arguments pass through to `chainsaw test`, e.g. --selector
# scenario=serving to run one test. The clouds/ tests need a different
# environment; see their chainsaw-test.yaml.
set -euo pipefail

CP=modelplane-e2e-local
WL=modelplane-e2e-workload
here="$(cd "$(dirname "$0")" && pwd)"

# Chainsaw reads each cluster from a kubeconfig file, so write one per cluster.
# The control plane's is also chainsaw's default cluster, so nothing here
# depends on the caller's current context.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
kind get kubeconfig --name "$CP" >"$work/controlplane"
kind get kubeconfig --name "$WL" >"$work/workload"

KUBECONFIG="$work/controlplane" chainsaw test \
	--config "$here/.chainsaw.yaml" \
	--cluster "controlplane=$work/controlplane" \
	--cluster "workload=$work/workload" \
	"$@" "$here/local"
