#!/usr/bin/env bash
# Run one bake-off prototype's A-C suite and record its output, exit code and
# wall time. Usage: run-one.sh <option> <phase>
set -uo pipefail

opt="$1"
phase="$2"
root=/home/negz/control/modelplaneai
out=/tmp/bakeoff/runs
mkdir -p "$out"

case "$opt" in
shell) dir=modelplane-shell-shocked cmd=(e2e/test.sh) ;;
unittest) dir=modelplane-unit-of-measure cmd=(python3 -m unittest discover -c -s e2e/tests -v) ;;
pytest) dir=modelplane-py-in-the-sky cmd=(uv run --group e2e pytest) ;;
chainsaw) dir=modelplane-saw-point cmd=(e2e/chainsaw/test.sh) ;;
*)
	echo "unknown option $opt" >&2
	exit 2
	;;
esac

log="$out/$opt-$phase.log"
cd "$root/$dir" || exit 2
start=$SECONDS
nix develop -c "${cmd[@]}" >"$log" 2>&1
rc=$?
dur=$((SECONDS - start))
printf '%s %s exit=%s seconds=%s\n' "$opt" "$phase" "$rc" "$dur" | tee -a "$out/summary.txt"
