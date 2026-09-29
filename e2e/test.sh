#!/usr/bin/env bash
# Runs e2e scenarios against an environment run.sh brought up. See README.md.
#
#   e2e/test.sh                     # serving, lifecycle and placement
#   e2e/test.sh placement serving   # just these, in this order
#
# Each scenario is a script in tests/, and runs in its own process, so one that
# fails can't stop another running. clouds provisions a real EKS cluster, so it
# runs only when named. See tests/clouds.sh.
set -uo pipefail

cd "$(dirname "$0")" || exit
[ $# -gt 0 ] || set -- serving lifecycle placement

E2E_RESULTS="$(mktemp)"
export E2E_RESULTS
trap 'rm -f "$E2E_RESULTS"' EXIT

failed=()
for scenario in "$@"; do
	printf '\n##### %s\n' "$scenario"
	bash "tests/$scenario.sh" || failed+=("$scenario")
done

printf '\n##### Summary\n'
cat "$E2E_RESULTS"
if [ ${#failed[@]} -gt 0 ]; then
	printf 'FAILED: %s\n' "${failed[*]}"
	exit 1
fi
printf 'PASSED: %s\n' "$*"
