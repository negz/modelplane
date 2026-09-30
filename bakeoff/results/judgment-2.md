# E2E bake-off: judgment 2

Paths are relative to each option's worktree unless they start with `/tmp`.
Chainsaw source references are kyverno/chainsaw at v0.2.15. "(prototype)"
marks a weakness a better implementation would fix. "(option)" marks one that
would persist.

## 1. Scores

| Option | Legibility | Failure output | Cost of next test | Code we'd own | Toolchain | Coverage | Robustness |
|---|---|---|---|---|---|---|---|
| chainsaw | 3 | 3 | 3 | 5 | 2 | 3 | 4 |
| pytest | 4 | 4 | 5 | 3 | 4 | 4 | 5 |
| unittest | 4 | 5 | 4 | 4 | 5 | 4 | 3 |
| shell | 4 | 5 | 3 | 4 | 5 | 4 | 4 |

Unweighted sums are 23, 29, 29 and 29. The ranking in section 3 isn't a sum.

## 2. Evidence

### chainsaw

- **Legibility 3.** Each step is named and described for its check
  (`e2e/chainsaw/local/serving/chainsaw-test.yaml:90-93`), and condition asserts
  read declaratively (`local/lifecycle/chainsaw-test.yaml:64-75`). But C1 and C2
  are nested JMESPath over `x_k8s_list` (`local/placement/chainsaw-test.yaml:51-58,87-89`),
  and a passing run logs `b3-delete-model-deployment | ERROR | RUN` and
  `=== ERROR modelservices.modelplane.ai "mock-chainsaw" not found`
  (chainsaw-healthy1.log). (option)
- **Failure output 3.** Every independent failure was reported, and C2 named the
  pod: `($offenders): Invalid value: ["cert-manager/bakeoff-rogue"]: lengths of slices don't match`.
  But A3 and B1 surfaced as `Internal error: unexpected end of JSON input`, with
  the 401 only in curl's stderr. B2 and B3 vanished without a skip, and the
  summary is just `Failed tests 3`, interleaved across parallel tests.
- **Cost of next test 3.** A resource-state check takes a few lines. Every
  request check, though, repeats its bindings and a `kubectl exec curl` script:
  the same `x_k8s_get` binding appears at `serving/chainsaw-test.yaml:96,124,166,194,222`.
  "Eventually" checks are hand-rolled shell loops
  (`lifecycle/chainsaw-test.yaml:127-140`) (option). Some of the duplication is
  prototype: v0.2.15 has test-level `spec.bindings` and `StepTemplate`/`use`
  (`pkg/apis/v1alpha1/test.go`, `step.go`), and neither is used.
- **Code we'd own 5.** Only `test.sh` (24 lines), `.chainsaw.yaml` (20) and
  `curl.yaml` (16) sit outside the tests. Running, cleanup and reporting are
  upstream.
- **Toolchain 2.** It adds a pre-1.0 Go tool (`Version: v0.2.15`,
  `chainsaw.kyverno.io/v1alpha1`) and a YAML-and-JMESPath DSL, while the tests
  still need bash, kubectl and awk inside scripts (`serving/chainsaw-test.yaml:262-271`).
  It's in the dev shell only (`flake.nix` +1), not the e2e app, and
  `nix flake check` doesn't lint it (`chainsaw lint test` passes when run by
  hand).
- **Coverage 3.** A to D are all written, and lifecycle suits Chainsaw
  (apply, assert, delete and `error`, with automatic cleanup). Serving, though,
  is bash wrapped in YAML (option). Placement can't use a resource assert across
  namespaces, because the namespacer fills in the test namespace when none is
  set (`pkg/engine/namespacer/namespacer.go`, `if resource.GetNamespace() == ""`).
  run.sh's other `--verify` checks stay behind, duplicating A (BAKEOFF.md:38-39)
  (prototype).
- **Robustness 4.** Cleanup belongs to the framework. It runs on failure and
  waits for each deletion (`pkg/engine/operations/delete/operation.go`,
  `waitForDeletion`). The faulted run deleted the MS, the MD and the pod, then
  the namespace. Against that, A2 to A4 are single-shot (prototype). A JMESPath
  evaluation error ends an assert instead of retrying
  (`pkg/engine/operations/assert/operation.go`, `return false, err`) (option).
  Cleanup deletes the MD with Background propagation before the namespace (log
  header `DeletionPropagationPolicy Background`), and the three tests run in
  parallel against one gateway. That this could flake is my inference, not
  something the logs show.

### pytest

- **Legibility 4.** Each test is named for its check and has a docstring
  (`e2e/test_serving.py:67-76`), with plain `assert`s. A newcomer has to follow
  session fixtures, a report hookwrapper (`e2e/conftest.py:56-75`), and a
  closure handed to `wait.until` in every test.
- **Failure output 4.** Every failure came with its source, its values and a
  `Modelplane resources in ml-team` section. C2 printed
  `"cert-manager/bakeoff-rogue tolerates [{'operator': 'Exists'}]"`. But the run
  took 715s, because each failing request retries for 120s (`test_serving.py:39`).
  B2 *passed* on a 401 (`The gateway answered 401: Client authentication failed.`),
  because its guard checks only RoutingReady (`test_lifecycle.py:95-96`). A5's
  message (`the last few for ml-team/mock were []`) hides the 401s. All
  prototype.
- **Cost of next test 5.** A new scenario is a module that reuses the existing
  fixtures (`client`, `endpoints`, `control_plane`), `wait.until`, and a YAML
  directory a fixture applies and tears down (`test_lifecycle.py:50-63`).
  Selection is by a marker registered in `pyproject.toml`.
- **Code we'd own 3.** About 446 lines of harness sit beyond the tests:
  `conftest.py` 170, `kube.py` 123, `gateway.py` 111 and `wait.py` 42. On top of
  that are 300 lines of bring-up in `environment.py`, which replace run.sh's
  496, plus `nix/python.nix`. The harness uses pytest hooks, markers and
  command-line options.
- **Toolchain 4.** It adds pytest, with pluggy, iniconfig, packaging and
  pygments, as a uv dependency group (`uv.lock` +64), plus a uv2nix venv. The
  `ty-e2e` check builds and passes. It ties e2e to the functions' uv workspace,
  and `uv run` gets Python 3.13 (log: `Python 3.13.12`) where the nix venv is
  3.12.
- **Coverage 4.** A to D read naturally. Lifecycle is a fixture with a
  `finally` teardown, and D is gated by a marker plus `--cloud`
  (`conftest.py:78-85`). The orchestrator verified bring-up on a fresh
  environment (`pytest-bringup.log`: `11 passed, 1 skipped in 928.03s`). But
  deleting run.sh drops its unknown-model, mTLS, port-80 and `/v1/models`
  checks (BAKEOFF.md:39-41) (prototype).
- **Robustness 5.** Every wait has a timeout and says what it waited for
  (`wait.py:24-42`), every kubectl call is bounded (`kube.py:32`), request
  checks retry, and B2 won't count a missing response as stopped
  (`test_lifecycle.py:106`). Teardown deletes the MS and then the MD
  (foreground), waits, and only then deletes the namespace
  (`test_lifecycle.py:58-63`), which avoids the wedge in findings.md. One hazard
  outside A to C: D1's teardown deletes `ClusterProviderConfig default` and
  `crossplane-system/aws-creds` (`test_clouds.py:56-58`), which may be a
  maintainer's own (prototype).

### unittest

- **Legibility 4.** `-v` prints each docstring beside its result (log:
  `A1: An OpenAI chat completion with the caller's key returns 200. ... FAIL`),
  and the tests are plain `assertEqual`s. B1 to B3 depend on alphabetical method
  order (`e2e/tests/test_lifecycle.py:64-69`), which a reader has to know.
- **Failure output 5.** Each failure is direct:
  `401 != 200 : HTTP 401: Client authentication failed.`, followed by the MS and
  IG conditions (`test_serving.py:73-79`). A5 listed the records it saw, all
  `"status": 401`. B2 skipped `(HTTP 401)`, C2 named the pod, and the run took
  244s.
- **Cost of next test 4.** A new scenario is a TestCase that uses the `kube` and
  `gateway` helpers. But each class rewrites its namespace, curl pod and cleanup
  in `setUpClass` (`test_serving.py:59-71`, `test_lifecycle.py:73-83`), and
  there are no fixtures, markers or parametrization beyond `subTest` (option).
- **Code we'd own 4.** 205 lines of plain helpers (`kube.py` 105, `gateway.py`
  100), and no framework code. Discovery and reporting come from the stdlib.
- **Toolchain 5.** Stdlib only, run with the dev shell's `python3` and
  `kubectl`. It adds a `ty-e2e` check, which builds and passes. The e2e app would
  need only `pkgs.python3` (not done).
- **Coverage 4.** A to D are done, with D gated by `skipUnless` on an
  environment variable (`test_clouds.py:46`). Lifecycle leans on method order
  and skips. run.sh's other checks stay in run.sh, duplicating A
  (BAKEOFF.md:42-44). D1 applies the docs platform without renaming it, so it
  can collide with a maintainer's (`test_clouds.py` docstring).
- **Robustness 3.** Every A check is a single request with no retry
  (`test_serving.py:81-107`), and kubectl runs with no timeout (`kube.py:35`).
  B2 counts a `000` no-response as stopped (`test_lifecycle.py:122`,
  `gateway.py:77`). Teardown only deletes the namespace
  (`test_lifecycle.py:55-61`), the pattern findings.md saw wedge. All prototype.
  The orchestrator's quiet serial runs exercised none of these.

### shell

- **Legibility 4.** Each check states what must hold (`e2e/tests/serving.sh:38`),
  and the output reads `=== A1: ...` then `--- FAIL A1: ...`. The code leans on
  bash idioms and on globals set by side effect: `request` sets `code`, `body`
  and `curl_exit` (`e2e/lib.sh:108-117`). C2 is a dense jsonpath template
  (`tests/placement.sh:48`).
- **Failure output 5.** The log names every failure and its cause:
  `--- FAIL A1: after 2 minutes of retries, ... got HTTP 401: Client authentication failed.`
  It attaches `crossplane resource trace` output, skips A3, A5 and B2 as
  consequences, names `cert-manager/bakeoff-rogue`, and ends with a summary of
  every check.
- **Cost of next test 3.** A scenario is a script plus an entry in the default
  list (`e2e/test.sh:13`). But each check writes its own pass and fail branches,
  and without jq, JSON is matched as text (`lib.sh:126`, `serving.sh:104-115`).
  That cost is the option's, and it grows with the behavioural tests #368 asks
  for.
- **Code we'd own 4.** `lib.sh` (128 lines) and `test.sh` (31) are a small
  homegrown test runner: check, pass, fail and skip, with results aggregated
  across processes through a temp file. It's little code, but it's a framework
  we'd maintain.
- **Toolchain 5.** It adds nothing: bash, kubectl, the crossplane CLI and the
  pinned curl image, all already in the e2e app. CI is wired, because
  `run.sh --verify` now ends with `bash "$ROOT/e2e/test.sh"`. It needs bash 4 or
  later (BAKEOFF.md:64-65).
- **Coverage 4.** A to D are done, and A6 to A9 moved from run.sh, so nothing is
  lost (log: `PASS A6` to `PASS A9`). D runs only when named, with
  `E2E_CP_CONTEXT` set (`tests/clouds.sh:17-20`). The placement and JSON checks
  are awkward text processing (option).
- **Robustness 4.** `eventually` retries A1, A4, A5 and B1 (`lib.sh:64`).
  EXIT, INT and TERM traps delete the namespaces and report teardown as a check
  (`tests/lifecycle.sh:16-27`). But teardown only deletes the namespace, which
  risks the wedge if B3 is skipped. A2 and A6 to A9 are single-shot, and text
  matching breaks if the log's format changes (BAKEOFF.md:56-57).

## 3. Ranking

1. **pytest**
2. **unittest**
3. **shell**
4. **chainsaw**

Three options tie on raw sums, so I weighed option-level weaknesses over
prototype ones. pytest's low marks are mostly the prototype's: slow 120-second
retries, B2's guard, and the dropped run.sh checks. What the option itself costs
is one dependency and a larger harness. In return it has the cheapest next
test, and fixtures that make ordered teardown natural. unittest's weaknesses (no
retries, no kubectl timeouts, namespace-only teardown) are fixable too, but with
no fixtures each fix gets rewritten per class. Shell built the most complete
prototype and the clearest report, but its weakness belongs to the option:
there's no structured data, which the behavioural tests in #368 need most.
Chainsaw owns the least code, but it pushes serving and placement into
bash-in-YAML and JMESPath on a pre-1.0 tool, and its failure messages are the
least direct.

## 4. Biggest risk if adopted

- **chainsaw.** Most checks this repo needs are HTTP, log or cross-namespace
  checks, and they become bash inside YAML plus JMESPath. The project would
  maintain two languages on a v0.2 tool, and a failure such as
  `unexpected end of JSON input` has to be decoded from the raw log.
- **pytest.** It replaces proven shell bring-up with 300 lines of Python as the
  CI gate, and that gate's actual path has never run: `nix run .#e2e` into the
  uv2nix venv into `pytest --bring-up`. findings.md verified only `uv run`, and
  notes the nix app wasn't exercised.
- **unittest.** Flaky CI and wedged namespaces. The A checks are single-shot and
  teardown only deletes the namespace. Without fixtures, making every class
  robust means rebuilding pytest's fixture machinery by hand.
- **shell.** Behavioural serving tests (#368) written in bash with no JSON
  parser. Assertions work by text pattern (`lib.sh:126`, `serving.sh:104-115`),
  so a reformatted body or log can make them misread silently.

## 5. Where the experiment is unfair or misleading

- **Flakes can't show.** Serial runs on a quiet environment, and only two
  healthy runs each, can't expose flakes. That favours the options with few or
  no retries (unittest's single-shot A checks, chainsaw's A2 to A4), and makes
  pytest's two-minute retries look like pure cost (715s faulted). The builders
  added retries because they tested under interference.
- **One root cause, and a fast one.** F1 is a single fault that breaks every
  authenticated request with an immediate 401. It rewards consequence skips and
  fast failure, which favours unittest (244s) and chainsaw (150s, in parallel).
  It never tests diagnostics for a slow failure, such as a ModelService that
  never goes RoutingReady, and that's where pytest's diagnose hook, chainsaw's
  `catch` and shell's `trace` differ most.
- **Teardown wasn't stressed.** Nothing failed before B3, so namespace-only
  teardown was never tested. The orchestrator hit the wedge by accident. This
  favours shell and unittest on robustness.
- **The diff isn't all the code.** Only pytest had to write bring-up, and it
  wasn't allowed to run it. Chainsaw and unittest left run.sh untouched and
  didn't wire CI, so neither their integration cost nor their duplication of
  run.sh's `--verify` checks shows in their diffs. Measured on the diff, "code
  we'd own" favours chainsaw and unittest, while pytest's count includes
  bring-up the others inherit as run.sh's 496 lines.
- **Requests go from a pod.** The pod-only HTTP rule makes every option shell
  out to `kubectl exec curl`. That removes Python's advantage in sending
  requests and pushes chainsaw's serving checks into bash, which slightly
  favours shell. JSON handling (A3, A5) still favours Python.
- **Wall times.** Chainsaw runs its tests in parallel by default and the others
  run serially, so its times (107 to 150s) aren't comparable. Any of the others
  could be run in parallel.
- **Untested either way.** D1 never ran and nobody ran on macOS, so gating and
  portability are scored on design alone. That hides shell's bash-4 dependency
  and each option's D1 teardown hazards.
