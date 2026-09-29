#!/usr/bin/env bash
# Scenario B: lifecycle. A ModelDeployment and ModelService in a namespace of
# their own serve, stop serving once the ModelService is deleted, and take their
# replicas, endpoints and engine pods with them once the ModelDeployment is.
# shellcheck source=../lib.sh
source "$(dirname "$0")/../lib.sh"

ns=bakeoff-shell
name=mock-shell

# Deleting the namespace deletes whatever the checks left in it, and waits for
# Crossplane to delete what that composed. It leaves the namespace on the
# workload cluster that Modelplane mirrors this one to, mp-bakeoff-shell-<hash>.
# The InferenceCluster composes that without the Delete management policy, so
# it's orphaned by design, and it's not this scenario's to delete.
teardown() {
	check teardown "Namespace $ns and everything in it are deleted"
	if kcp delete namespace "$ns" --ignore-not-found --timeout=5m; then
		pass
	else
		fail "namespace $ns is still terminating after 5 minutes"
		# Says what the namespace is still waiting for.
		diag kcp get namespace "$ns" -o jsonpath='{range .status.conditions[*]}{.type}={.status}: {.message}{"\n"}{end}'
	fi
}
trap 'teardown; finish' EXIT
trap 'fail interrupted; exit 130' INT TERM

# A run that was killed before its teardown finished leaves the namespace.
kcp delete namespace "$ns" --ignore-not-found --timeout=5m
probe_up "$CP" "$ns"

openai="$(kcp get inferencegateway local -o jsonpath='{.status.endpoints.openAI}')"
# The ModelDeployment is manifests/'s, renamed. apply -n refuses a manifest
# naming any other namespace, so this can't touch ml-team's.
kcp patch --local -f "$(dirname "$0")/../manifests/40-model-deployment.yaml" -o yaml --type merge \
	-p '{"metadata":{"name":"'"$name"'","namespace":"'"$ns"'"}}' | kcp apply -n "$ns" -f -
kcp apply -n "$ns" -f - <<EOF
apiVersion: modelplane.ai/v1alpha1
kind: ModelService
metadata:
  name: $name
  namespace: $ns
spec:
  endpoints:
  - name: $name
    selector:
      matchLabels:
        modelplane.ai/deployment: $name
EOF

# chat sends an OpenAI chat completion naming the ModelService.
chat() {
	request "$CP" "$ns" "$openai/chat/completions" -H "authorization: Bearer $CALLER_KEY" \
		-H 'content-type: application/json' -d "$(chat_body "$model")"
}

check B1 "ModelService $ns/$name becomes RoutingReady, then a request naming it returns 200 from served model $ns/$name"
routing_ready() {
	[ "$(kcp -n "$ns" get modelservice "$name" -o jsonpath='{.status.conditions[?(@.type=="RoutingReady")].status}')" = True ]
}
served() {
	chat
	[ "$code" = 200 ]
}
start=$SECONDS
model=""
serving=0
if ! eventually 360 routing_ready; then
	fail "not RoutingReady after 6 minutes"
	diag trace modelservice "$name" -n "$ns"
	diag trace modeldeployment "$name" -n "$ns"
else
	ready=$((SECONDS - start))
	model="$(kcp -n "$ns" get modelservice "$name" -o jsonpath='{.status.model}')"
	if ! eventually 120 served; then
		fail "RoutingReady after ${ready}s, but after 2 minutes of retries a request naming '$model' got HTTP $code: $body"
	else
		serving=1
		got="$(body_model "$body")"
		if [ "$got" = "$ns/$name" ]; then
			pass "RoutingReady after ${ready}s"
		else
			fail "a request naming '$model' was served by '$got': $body"
		fi
	fi
fi

# Any HTTP status but 200 passes. No status at all doesn't: curl failing to
# reach the gateway says nothing about the route.
check B2 "Requests naming ModelService $ns/$name stop returning 200 within 5 minutes of deleting it"
stopped() {
	chat
	[ "$code" != 200 ] && [ "$code" != none ]
}
if [ "$serving" = 0 ]; then
	skip "B1 never got a 200, so there's nothing to stop"
else
	start=$SECONDS
	kcp -n "$ns" delete modelservice "$name" --timeout=2m
	if eventually 300 stopped; then
		pass "HTTP $code after $((SECONDS - start))s"
	else
		fail "the last request, 5 minutes after the delete, got HTTP $code: $body"
	fi
fi

# A foreground delete returns only once every dependent that blocks the owner's
# deletion is gone, so the control plane has to be clean the moment it returns.
# The engine pods live on the other cluster, and go when their provider
# deletes them.
check B3 "Deleting ModelDeployment $ns/$name in the foreground removes its ModelReplicas and ModelEndpoints, then its engine pods"
replicas="$(kcp -n "$ns" get modelreplica -l "modelplane.ai/deployment=$name" -o jsonpath='{.items[*].metadata.name}')"
pods_gone() {
	pods="$(kwl get pods -A -l "modelplane.ai/serving in (${replicas// /,})" -o name 2>&1 | grep -v '^No resources found')"
	[ -z "$pods" ]
}
if [ -z "$replicas" ]; then
	skip "ModelDeployment $ns/$name has no ModelReplicas to delete"
	diag trace modeldeployment "$name" -n "$ns"
elif ! kcp -n "$ns" delete modeldeployment "$name" --cascade=foreground --timeout=3m; then
	fail "the foreground delete didn't finish within 3 minutes"
	diag trace modeldeployment "$name" -n "$ns"
else
	left="$(kcp -n "$ns" get modelreplica,modelendpoint -l "modelplane.ai/deployment=$name" -o name 2>&1 | grep -v '^No resources found')"
	start=$SECONDS
	if [ -n "$left" ]; then
		fail "the delete returned, but these remain: $left"
	elif ! eventually 180 pods_gone; then
		fail "engine pods for ModelReplicas '$replicas' remain 3 minutes after the delete: $pods"
		diag kwl get pods -A -l "modelplane.ai/serving in (${replicas// /,})" -o wide
	else
		pass "engine pods gone $((SECONDS - start))s after the delete returned"
	fi
fi

# The EXIT trap tears down and finishes.
