# shellcheck shell=bash
# The scripts that source this use the variables it sets.
# shellcheck disable=SC2034
#
# Helpers the scenario scripts in tests/ source. A scenario is a script of
# checks: check starts one, and pass, fail or skip ends it. A failed check
# doesn't stop the script, so one run reports every independent failure.
# finish prints the scenario's results and exits non-zero if any check failed.
#
# No set -e: a check fails by calling fail, not by a command failing, and a
# command that fails inside a check is usually what the check reports.
set -uo pipefail

# Point these elsewhere to test another environment, like a cloud control plane
# for tests/clouds.sh.
CP="${E2E_CP_CONTEXT:-kind-modelplane-e2e-local}"
WL="${E2E_WL_CONTEXT:-kind-modelplane-e2e-workload}"

# Pinned by digest (a multi-arch manifest list) so a moving :latest can't flake
# the probe pods.
CURL_IMAGE=curlimages/curl@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13

# The gateway authenticates callers against the key in
# manifests/10-inference-gateway.yaml.
CALLER_KEY=sk-e2e-caller

kcp() { kubectl --context "$CP" "$@"; }
kwl() { kubectl --context "$WL" "$@"; }

results=()
current=""

# check ID DESCRIPTION starts a check. The description says what must hold.
check() {
	current="$1"
	printf '\n=== %s: %s\n' "$1" "$2"
}

pass() {
	printf -- '--- PASS %s%s\n' "$current" "${1:+: $1}"
	results+=("PASS $current")
}

fail() {
	printf -- '--- FAIL %s: %s\n' "$current" "$1"
	results+=("FAIL $current")
}

# skip is for a check that can't run because one it depends on failed.
skip() {
	printf -- '--- SKIP %s: %s\n' "$current" "$1"
	results+=("SKIP $current")
}

# diag COMMAND... runs a command for diagnostics after a failure, indented under
# the check it explains.
diag() {
	printf '    $ %s\n' "$*"
	"$@" 2>&1 | sed 's/^/    /'
}

# eventually SECONDS COMMAND... runs a command every 5 seconds until it
# succeeds, or until SECONDS have passed.
eventually() {
	local deadline=$((SECONDS + $1))
	shift
	until "$@"; do
		[ "$SECONDS" -lt "$deadline" ] || return 1
		sleep 5
	done
}

# trace RESOURCE... prints the tree of what a control plane resource composed,
# with each one's status: the first thing to read when one isn't ready.
trace() { crossplane resource trace --context "$CP" -o wide "$@"; }

# finish prints the scenario's results and exits. With E2E_RESULTS set, as
# test.sh sets it, it also appends them there for test.sh's summary.
finish() {
	printf '\n'
	printf '%s\n' "${results[@]}"
	[ -z "${E2E_RESULTS:-}" ] || printf '%s\n' "${results[@]}" >>"$E2E_RESULTS"
	for r in "${results[@]}"; do
		case "$r" in FAIL*) exit 1 ;; esac
	done
	exit 0
}

# Gateway addresses are on the kind Docker network, which a macOS host can't
# route to, so requests go from a curl pod in a cluster. probe_up CONTEXT
# NAMESPACE creates the namespace and a pod named probe that idles in it, and
# request execs curl there. Delete the namespace to clean up.
probe_up() {
	local ctx="$1" ns="$2"
	kubectl --context "$ctx" create namespace "$ns" || return 1
	# A new namespace's default ServiceAccount arrives a moment after the
	# namespace, and pod admission rejects a pod until it has. The trap lets
	# sleep, as PID 1, exit on SIGTERM rather than hold up deleting the
	# namespace for the grace period.
	eventually 30 kubectl --context "$ctx" -n "$ns" run probe --image="$CURL_IMAGE" \
		--restart=Never --command -- sh -c 'trap "exit 0" TERM; sleep 86400 & wait' || return 1
	kubectl --context "$ctx" -n "$ns" wait pod/probe --for=condition=Ready --timeout=2m
}

# request CONTEXT NAMESPACE CURL_ARGS... sends a request from the probe pod in
# NAMESPACE. It sets code to the HTTP status, or none, body to the response body
# or curl's error, and curl_exit to curl's exit code.
request() {
	local ctx="$1" ns="$2" out
	shift 2
	out="$(kubectl --context "$ctx" -n "$ns" exec probe -- \
		curl -sS --max-time 15 -w '\n%{http_code}' "$@" 2>&1)"
	curl_exit=$?
	code="${out##*$'\n'}"
	body="${out%$'\n'*}"
	[[ "$code" =~ ^[1-9][0-9][0-9]$ ]] || code=none
}

# chat_body MODEL is an OpenAI chat completion body naming MODEL. The mock
# engine's token counts, which the serving scenario asserts, depend on it.
chat_body() {
	printf '{"model":"%s","messages":[{"role":"user","content":"ping"}]}' "$1"
}

# body_model prints the model a JSON response body reports.
body_model() {
	sed -n 's/.*"model": *"\([^"]*\)".*/\1/p' <<<"$1"
}
