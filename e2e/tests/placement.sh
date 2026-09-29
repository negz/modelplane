#!/usr/bin/env bash
# Scenario C: placement. Pods on the workload cluster land where they should,
# and don't tolerate taints they have no business tolerating. Read-only.
# shellcheck source=../lib.sh
source "$(dirname "$0")/../lib.sh"

# Pods allowed a wildcard toleration, as namespace/owner-kind/owner-name. A
# DaemonSet's pods are named after it plus a random suffix, so match the owner.
wildcard_allowlist=(
	# node-exporter reports on every node, whatever its taints.
	monitoring/DaemonSet/mp-kube-prometheus-stack-prometheus-node-exporter
)

check C1 "Every engine pod for ml-team/mock-demo runs on a gpu-synthetic node, with a device allocated from gpu.example.com"
replicas="$(kcp -n ml-team get modelreplica -l modelplane.ai/deployment=mock-demo -o jsonpath='{.items[*].metadata.name}')"
pods=""
[ -z "$replicas" ] || pods="$(kwl get pods -A -l "modelplane.ai/serving in (${replicas// /,})" \
	-o jsonpath='{range .items[*]}{.metadata.namespace}|{.metadata.name}|{.spec.nodeName}|{.status.resourceClaimStatuses[*].resourceClaimName}{"\n"}{end}')"
problems=()
while IFS='|' read -r pns pod node claims; do
	[ -n "$pod" ] || continue
	pool="$(kwl get node "$node" -o jsonpath='{.metadata.labels.modelplane\.ai/pool}' 2>&1)"
	[ "$pool" = gpu-synthetic ] || problems+=("$pns/$pod is on node '$node', in pool '$pool'")
	[ -n "$claims" ] || problems+=("$pns/$pod has no ResourceClaims")
	for claim in $claims; do
		drivers="$(kwl -n "$pns" get resourceclaim "$claim" -o jsonpath='{.status.allocation.devices.results[*].driver}' 2>&1)"
		case " $drivers " in
		*" gpu.example.com "*) ;;
		*) problems+=("$pns/$pod's ResourceClaim $claim has devices from '$drivers'") ;;
		esac
	done
done <<<"$pods"
if [ -z "$replicas" ]; then
	fail "found no ModelReplicas for ModelDeployment ml-team/mock-demo"
	diag trace modeldeployment mock-demo -n ml-team
elif [ -z "$pods" ]; then
	fail "found no engine pods for ModelReplicas '$replicas'"
	diag kwl get pods -A -l modelplane.ai/serving
elif [ ${#problems[@]} -gt 0 ]; then
	fail "$(printf '\n    %s' "${problems[@]}")"
else
	pass "$(grep -c . <<<"$pods") pod(s)"
fi

check C2 "No pod outside kube-system carries a wildcard toleration (operator Exists, no key), except those allowlisted"
# Each line is namespace|name|owner-kind/owner-name|;operator=key;... so a
# wildcard shows as ;Exists=;.
if all="$(kwl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}|{.metadata.name}|{.metadata.ownerReferences[0].kind}/{.metadata.ownerReferences[0].name}|;{range .spec.tolerations[*]}{.operator}={.key};{end}{"\n"}{end}' 2>&1)"; then
	offenders=()
	while IFS='|' read -r pns pod owner tolerations; do
		[ "$pns" != kube-system ] || continue
		case "$tolerations" in *";Exists=;"*) ;; *) continue ;; esac
		case " ${wildcard_allowlist[*]} " in *" $pns/$owner "*) continue ;; esac
		offenders+=("$pns/$pod (owned by $owner)")
	done <<<"$all"
	if [ ${#offenders[@]} -eq 0 ]; then
		pass
	else
		fail "${#offenders[@]} pod(s) tolerate every taint:$(printf '\n    %s' "${offenders[@]}")"
	fi
else
	fail "couldn't list pods: $all"
fi

finish
