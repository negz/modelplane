#!/usr/bin/env bash
# Scenario A: serving. The InferenceGateway authenticates, routes and meters
# OpenAI and Anthropic requests for ModelService ml-team/mock, which manifests/
# deploys.
# It changes nothing there: it only creates the probe pods it sends requests
# from, in a namespace of its own on each cluster.
#
# shellcheck disable=SC2329 # shellcheck can't see eventually calling a1, a4, a5.
# shellcheck source=../lib.sh
source "$(dirname "$0")/../lib.sh"

ns=bakeoff-shell-serving
proxy=gateway.envoyproxy.io/owning-gateway-name=inference-gateway

trap 'kcp delete namespace "$ns" --ignore-not-found --timeout=2m; kwl delete namespace "$ns" --ignore-not-found --timeout=2m' EXIT
trap 'exit 130' INT TERM

kcp delete namespace "$ns" --ignore-not-found --timeout=2m
kwl delete namespace "$ns" --ignore-not-found --timeout=2m
probe_up "$CP" "$ns"
probe_up "$WL" "$ns"

# A5 reads access log records written since the probe pod started. The kubelet
# stamps both with the kind nodes' clock, which on macOS is the Docker VM's
# rather than this host's.
since="$(kcp -n "$ns" get pod probe -o jsonpath='{.status.startTime}')"
model="$(kcp -n ml-team get modelservice mock -o jsonpath='{.status.model}')"
openai="$(kcp get inferencegateway local -o jsonpath='{.status.endpoints.openAI}')"
anthropic="$(kcp get inferencegateway local -o jsonpath='{.status.endpoints.anthropic}')"

# chat MODEL [CURL_ARGS...] sends an OpenAI chat completion naming MODEL.
chat() {
	local m="$1"
	shift
	request "$CP" "$ns" "$openai/chat/completions" -H 'content-type: application/json' -d "$(chat_body "$m")" "$@"
}

check A1 "An OpenAI chat completion naming ${model:-<no status.model>} with the caller key returns 200"
a1() {
	chat "$model" -H "authorization: Bearer $CALLER_KEY"
	[ "$code" = 200 ]
}
a1_body=""
if eventually 120 a1; then
	a1_body="$body"
	pass
else
	fail "after 2 minutes of retries, the last request to $openai got HTTP $code: $body"
	diag trace modelservice mock -n ml-team
	diag trace inferencegateway local
fi

check A2 "The same request returns 401 with no key, and with key sk-wrong"
chat "$model"
nokey="$code"
chat "$model" -H 'authorization: Bearer sk-wrong'
wrong="$code"
if [ "$nokey" = 401 ] && [ "$wrong" = 401 ]; then
	pass
else
	fail "no key got HTTP $nokey, sk-wrong got HTTP $wrong"
fi

# The engine answers only to the name Modelplane started it under, so A1's 200
# already implies the gateway rewrote the model. This is the visible half.
check A3 "A1's response reports served model ml-team/mock-demo, not $model"
if [ -z "$a1_body" ]; then
	skip "A1 got no 200 response to read"
elif [ "$(body_model "$a1_body")" = ml-team/mock-demo ]; then
	pass
else
	fail "response reports model '$(body_model "$a1_body")': $a1_body"
fi

check A4 "An Anthropic /v1/messages request at $anthropic, with the key in x-api-key, returns 200"
a4() {
	request "$CP" "$ns" "$anthropic/messages" -H "x-api-key: $CALLER_KEY" \
		-H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
		-d '{"model":"'"$model"'","max_tokens":16,"messages":[{"role":"user","content":"ping"}]}'
	[ "$code" = 200 ]
}
if eventually 60 a4; then
	pass
else
	fail "after 1 minute of retries, the last request got HTTP $code: $body"
fi

# The other bake-off agents send the same requests as the same caller, so a
# matching record may be theirs. Reading only records since this scenario
# started at least rules out records from before whatever broke.
check A5 "Since $since, the Envoy access log holds a usage record for ml-team/mock with caller e2e, served model ml-team/mock-demo, 12+9=21 tokens and status 200"
want=(
	'"caller":"e2e"'
	'"service":"ml-team/mock"'
	'"served_model":"ml-team/mock-demo"'
	'"input_tokens":12'
	'"output_tokens":9'
	'"total_tokens":21'
	'"status":200'
)
# Each field ends at a comma or the record's closing brace, which stops
# "input_tokens":12 matching 120. The log writes keys in alphabetical order, but
# nothing here depends on it.
a5() {
	local line f ok
	records="$(kwl -n envoy-gateway-system logs -l "$proxy" -c envoy --tail=-1 --since-time="$since" 2>&1)" || return 1
	while read -r line; do
		ok=1
		for f in "${want[@]}"; do
			case "$line" in *"$f,"* | *"$f}"*) ;; *) ok=0 ;; esac
		done
		[ "$ok" = 0 ] || return 0
	done <<<"$records"
	return 1
}
if [ -z "$a1_body" ]; then
	skip "A1 sent no request that was served"
elif eventually 30 a5; then
	pass
else
	fail "no record has all of ${want[*]}"
	# The records it did find for ml-team/mock, or failing any, the last
	# of whatever the logs held, which may be an error from kubectl.
	printf '    access log since %s:\n' "$since"
	{ grep -F '"service":"ml-team/mock"' <<<"$records" || tail -5 <<<"$records"; } | sed 's/^/    /'
	diag kwl -n envoy-gateway-system get pods -l "$proxy"
fi

check A6 "A model no ModelService claims isn't served"
chat ml-team/nope -H "authorization: Bearer $CALLER_KEY"
if [ "$code" != 200 ]; then
	pass "HTTP $code"
else
	fail "ml-team/nope got HTTP 200: $body"
fi

# Every check above goes through the InferenceGateway, which holds a client
# certificate, so none would notice the cluster gateway's mTLS lapsing. Probe it
# from the workload cluster, where its Service name resolves. The trailing dot
# skips the pod's search domains. A resolve failure (curl 6) fails the check.
gw="$(kcp get inferencecluster local -o jsonpath='{.status.gateway.hostname}')"

check A7 "The cluster gateway refuses a caller with no client certificate"
request "$WL" "$ns" -k -o /dev/null "https://${gw}./v1/models"
case "$curl_exit" in
35 | 52 | 55 | 56) pass "curl exit $curl_exit" ;;
0) fail "the cluster gateway answered HTTP $code without a client certificate" ;;
*) fail "curl exit $curl_exit isn't a refused handshake: $body" ;;
esac

# The serving HTTPRoutes carry no sectionName, so they'd attach to an HTTP
# listener and serve the engines without a certificate.
check A8 "The cluster gateway has no plaintext listener on port 80"
request "$WL" "$ns" -o /dev/null "http://${gw}./v1/models"
case "$curl_exit" in
7 | 28 | 35 | 52 | 56) pass "curl exit $curl_exit" ;;
0) fail "the cluster gateway answered HTTP $code over plaintext" ;;
*) fail "curl exit $curl_exit isn't a refused connection: $body" ;;
esac

check A9 "/v1/models lists $model"
request "$CP" "$ns" "$openai/models" -H "authorization: Bearer $CALLER_KEY"
case "$body" in
*"\"$model\""*) pass ;;
*) fail "HTTP $code: $body" ;;
esac

finish
