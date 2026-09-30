# E2E bake-off: judgment 3

## What I checked

- Every prototype's diff against `upstream/main`, all its test and helper code,
  and its `BAKEOFF.md`.
- All 12 run logs, `pytest-bringup.log`, `pytest-down.log`, `clean.log` and
  `summary.txt`. I cite chainsaw log lines with the colour codes stripped. That
  doesn't change the line numbers.
- Lint: `nix build --no-link .#checks.aarch64-linux.{python,license,shell-lint}`
  passes in all four worktrees, and so does `ty-e2e` in pytest and unittest.
  `chainsaw lint test` (0.2.15) reports all four Chainsaw tests valid.
- Chainsaw 0.2.15 source: `pkg/runner/runner.go`,
  `pkg/cleanup/cleaner/cleaner.go`, `pkg/engine/namespacer/namespacer.go`,
  `pkg/runner/operations/assert.go` and `pkg/apis/v1alpha1/step.go`.
- The orchestrator's namespace wedge. I couldn't reproduce it, because no
  clusters are running. The mechanism matches crossplane-runtime
  `pkg/resource/providerconfig.go:174-207`: before it connects, a managed
  resource applies its ProviderConfigUsage in its own namespace. So once a
  terminating namespace has deleted that usage, the managed resource can't
  recreate it, and its finalizer stays.
- The orchestrator's claim that pytest's `uv.lock` change rebuilds the function
  images. I evaluated `compose-usages-arm64`'s site-packages derivation in all
  five worktrees, and each one is different. So any source change rebuilds the
  function images. That cost isn't specific to pytest.

## Scores

| Option | Legibility | Failure output | Cost of next test | Code we'd own | Toolchain | Coverage | Robustness | Total |
|---|---|---|---|---|---|---|---|---|
| pytest | 4 | 4 | 5 | 3 | 4 | 4 | 4 | 28 |
| shell | 4 | 5 | 3 | 4 | 5 | 4 | 3 | 28 |
| chainsaw | 3 | 2 | 3 | 4 | 3 | 3 | 3 | 21 |
| unittest | 4 | 5 | 4 | 4 | 5 | 4 | 2 | 28 |

I mark a weakness **[option]** if it would persist however well the option was
built, and **[prototype]** if a better implementation would fix it.

## Evidence

### pytest

- **Legibility 4.** Each check is a function named for its ID, with a
  docstring, and a retry closure whose `what=` appears in the failure
  (`test_serving.py:67-76`, `wait.py:24-38`). The log shows the assertion, the
  body and "Still failing after waiting 120s…" (`pytest-faulted.log:116-119`).
  Reading the diagnostics needs pytest internals: a report hookwrapper and
  custom markers (`conftest.py:56-85`). **[option]**
- **Failure output 4.**
  - The results match the fault. A1, A3, A4, A5 and B1 fail with
    `Response(status=401, body='Client authentication failed.')`, and A2 and
    C1 pass.
  - C2 prints `cert-manager/bakeoff-rogue tolerates [{'operator': 'Exists'}]`
    (log:163), and every failure lists the conditions of the Modelplane
    resources in its namespace (log:131-135).
  - B2 **passed** on the 401 (log:27-32). It skips only if RoutingReady is
    false, then accepts any non-200 (`test_lifecycle.py:95-96,106`), so it
    passed without testing anything.
  - Two-minute retries on every check stretch the failing run to 715s, against
    140s when healthy (`summary.txt`).

  Both problems are **[prototype]**.
- **Cost of next test 5.** Helpers already cover waiting for a condition,
  waiting for deletion, selector lists, requests from a pod and usage records
  (`kube.py`, `gateway.py`). Parametrize and markers are there too
  (`test_serving.py:79-85`), and the README gives a recipe for adding a test
  (`e2e/README.md:190-198`). Asserting on a response is `r.json()`, which suits
  #368's behavioural tests.
- **Code we'd own 3.**
  - About 460 lines of non-test Python: `conftest.py` 170, `kube.py` 123,
    `gateway.py` 111, `wait.py` 42 and `__init__.py` 14.
  - The 300-line `environment.py`, plus `nix/python.nix` and new wiring in
    `flake.nix`, `apps.nix` and `checks.nix`.
  - A 125-line copy of the platform ModelDeployment
    (`manifests/lifecycle/10-model-deployment.yaml`).

  `environment.py` replaces the 496-line `run.sh`, so part of this is moved
  rather than new.
- **Toolchain 4.** It adds pytest in a new `e2e` uv group, a uv2nix venv and a
  `ty` check, all native to Python and Nix. `ty-e2e` builds the venv and
  passes. The costs:
  - A second test runner. The function tests use `python -m unittest`
    (upstream `nix/checks.nix`).
  - Two interpreters. Dev runs use 3.13 through uv (`pytest-healthy1.log:9`),
    and the app uses 3.12 (`BAKEOFF.md:48-49`).

  The function-image rebuild isn't a pytest cost. See "What I checked".
- **Coverage 4.** A1 to D1 all exist. `-m cloud` with `--cloud=eks` is the
  most natural gating of the four (`conftest.py:78-85`). The Python bring-up
  passed on a fresh environment: 11 passed in 928s (`pytest-bringup.log`).
  Deleting `run.sh` drops five checks that main runs today: unknown model,
  mTLS, port 80, `/v1/models` and the usage `endpoint` (`BAKEOFF.md:39-41`).
  That's a regression if merged as is. **[prototype]**
- **Robustness 4.**
  - Every wait is bounded, and every kubectl call has a 60s timeout
    (`kube.py:32`).
  - Teardown deletes the ModelService, then deletes the ModelDeployment in the
    foreground and waits before it deletes the namespace
    (`test_lifecycle.py:49-63`). It's the only teardown that avoids the
    namespace wedge on every path.
  - The two healthy runs were identical.
  - Nothing handles SIGTERM, and failures are slow.

### shell

- **Legibility 4.** Each `check A1 "…"` states the property and prints it
  (`serving.sh:38`), and the output is PASS, FAIL and SKIP lines with a
  summary. The code needs bash fluency, such as a jsonpath template
  (`placement.sh:48`) and glob matching on JSON (`serving.sh:104-115`).
- **Failure output 5.**
  - Every check reports a result.
  - Checks that depend on a failed one skip, with a reason: "SKIP A3: A1 got no
    200 response to read" (`shell-faulted.log:52`), and "SKIP B2: B1 never got
    a 200" (:95).
  - A1's failure includes `crossplane resource trace` output.
  - C2 names the pod (:117), though as "(owned by /)".
  - A6 is an extra check, and it passed on the 401 (:61), so it tested nothing
    under F1.
- **Cost of next test 3.** Each check is a hand-written block that calls
  pass, fail or skip (`lifecycle.sh:58-87`). There's no jq, so any structured
  assertion is sed or glob matching (`lib.sh:126-128`, `BAKEOFF.md:44-45,56-57`).
  That's cheap for status codes and expensive for response shapes. **[option]**
- **Code we'd own 4.** The helpers are 159 lines of simple bash (`lib.sh` 128
  and `test.sh` 31), and `run.sh` shrinks by 262 lines.
- **Toolchain 5.** It adds nothing (`BAKEOFF.md:44`). Everything it calls is
  already in the e2e app's runtimeInputs, and `shell-lint` passes.
- **Coverage 4.** It has A to D, plus A6 to A9, which keep `run.sh`'s extra
  checks. `--verify` runs it, so CI needs nothing more (`run.sh` ends with
  `bash "$ROOT/e2e/test.sh"`). It's the only prototype that loses no existing
  check. D1 runs only when named, with a context set (`clouds.sh:17-20`).
  Placement and JSON checks are awkward in shell.
- **Robustness 3.**
  - Strengths: `eventually` retries A1, A4, A5 and B1 to B3; EXIT, INT and
    TERM traps run the teardown (`lifecycle.sh:26-27`, `serving.sh:15-16`);
    and B2 demands a real HTTP status (`lifecycle.sh:92-95`).
  - The wedge path. Teardown only deletes the namespace
    (`lifecycle.sh:16-25`), and B3 skips if there are no ModelReplicas
    (:118-120). So a model that never schedules leaves a live ModelDeployment
    in a terminating namespace. **[prototype]**
  - A2 and A6 to A9 each send a single request.

### chainsaw

- **Legibility 3.** Steps are named for their checks and carry descriptions
  (`serving/chainsaw-test.yaml:90-93`), and resource asserts read cleanly
  (`lifecycle:64-75`). But serving is shell folded into YAML strings, and
  placement is JMESPath (`placement:53-64,87-89`). A 401 in B1 reads as
  "Internal error: unexpected end of JSON input" (`chainsaw-faulted.log:238`).
- **Failure output 2.**
  - A1, A4 and A5 report `Invalid value: "401": Expected value: "200"`
    (log:150,191,209).
  - C2 names the pod, but says "lengths of slices don't match" (log:126).
  - A3 and B1 fail on JSON parsing, not on the 401 (log:180,238).
  - B2 and B3 never ran and don't appear. Chainsaw ends a test at the first
    failing operation that lacks `continueOnError` (`runner.go` runStep:
    `if !continueOnError { return true }`). **[option]**
  - The console summary counts tests, not checks ("Failed tests 3",
    log:345). `--report-format JUNIT-STEP` exists, but the prototype doesn't
    use it.
- **Cost of next test 3.** A Kubernetes state check takes a few lines. Each
  HTTP check takes 25 to 30, with the same bindings in every step
  (`serving:94-98,122-126,164-168,…`), because bindings and outputs don't cross
  steps (`runner.go` runStep). StepTemplates (`v1alpha1/step.go:55-65`) could
  remove most of the repetition. **[prototype]**
- **Code we'd own 4.** The glue is 60 lines (`test.sh`, `.chainsaw.yaml` and
  `curl.yaml`). The logic still lives in inline shell and awk, repeated across
  steps (`serving:261-271`). D1's `cluster.yaml` copies `platform.yaml`.
- **Toolchain 3.** It adds `kyverno-chainsaw` 0.2.15, a pre-1.0 Go binary, in
  one line of `flake.nix`. It also adds a YAML and JMESPath DSL that nothing
  else in the repo uses, and the tests still fall back to sh, kubectl, grep and
  awk. It isn't in the e2e app yet (`BAKEOFF.md:28`).
- **Coverage 3.**
  - A to D are all written. D1 is gated by living outside `local/`
    (`test.sh:24`).
  - Lifecycle is where Chainsaw fits best: B3 uses `error` asserts
    (`lifecycle:161-178`).
  - Serving needs scripts.
  - C1 and C2 need pods from every namespace, which a resource assert can't
    list: it gives any namespaced resource without a namespace the test's own
    (`namespacer.go`). So they're JMESPath over `x_k8s_list`. **[option]**
  - Nothing is wired into `--verify`.
- **Robustness 3.**
  - Cleanup is automatic, runs in reverse order and waits for each deletion
    (`cleaner.go`). In the faulted run it removed the ModelService,
    ModelDeployment, pod and namespace in 5s (log:330-337).
  - Cleanup deletes the ModelDeployment with Background propagation (log:26)
    before the namespace, which only narrows the wedge window.
  - Tests run in parallel by default, against one shared gateway.
  - A2 to A4 each send a single request, and the retries are counts (curl
    `--retry 10`), not deadlines.

### unittest

- **Legibility 4.** These are plain stdlib TestCases, with one method per check
  and docstrings that `-v` prints (`unittest-healthy1.log`). The helpers are
  the smallest here. B depends on method-name order, which the class docstring
  explains (`test_lifecycle.py:64-69`). **[option]**
- **Failure output 5.**
  - Every result matches the fault.
  - B2 skips with a reason (`unittest-faulted.log:13`).
  - A5 prints the records it saw, all `"status": 401` with a null caller
    (log:104-110), which points straight at auth.
  - A1, A3 and A4 include the ModelService and InferenceGateway conditions
    (log:64-67).
  - B1's message is bare: "401 != 200 : Client authentication failed."
    (log:39).
  - The failing run took 244s.
- **Cost of next test 4.** A new check is a method using `kube.poll`, `get` or
  `items` and `client.post` (`kube.py:58-69`, `gateway.py:65-81`). There are
  no fixtures or markers, and retries are written by hand in each check.
  **[option]**
- **Code we'd own 4.** The helpers are about 205 lines of stdlib Python,
  license headers included. `run.sh` is untouched, so A1 to A5 exist twice
  until its checks move.
- **Toolchain 5.** It uses only the stdlib, and the same runner as the function
  tests (upstream `nix/checks.nix` runs `python -m unittest discover`).
  `ty-e2e` passes.
- **Coverage 4.** A to D are all written. D1's gating is natural: `skipUnless`
  on an environment variable (`test_clouds.py:46`), and the reason shows in
  the output (log:9). `run.sh`'s extra checks aren't ported, and the suite
  isn't wired into `--verify` or CI (`BAKEOFF.md:40-47`).
- **Robustness 2.**
  - A sends one request per check and never retries (`test_serving.py:81-84`,
    `BAKEOFF.md:63-64`).
  - kubectl calls have no timeout (`kube.py:35-37`).
  - Teardown only deletes the namespace (`test_lifecycle.py:55-61`), and B3
    skips if there are no engine pods (:129-132).
  - When the harness killed the first faulted run mid-B1, the namespace was
    left holding a live ModelDeployment. Deleting it by hand wedged, as
    `findings.md` records.

  All of this is **[prototype]**, but the stdlib offers no scaffolding for it.

## Ranking

1. pytest, 2. unittest, 3. shell, 4. chainsaw.

The top three tie at 28, so I broke the tie on whether their low scores belong
to the option or the prototype. Pytest's weaknesses can be fixed:
B2's false pass, the slow retries and the dropped checks. Its strengths belong
to the option: fixtures, markers, a helper library, and a Python bring-up that
passed from scratch. They matter most once tests like #368's arrive. Unittest
is close behind. It needs no dependencies and uses the functions' test runner,
and its weak robustness can be fixed, but it has to hand-roll what pytest
provides. Shell has the best failure output, but assertions on JSON in bash
limit the option itself. Chainsaw suits checks on Kubernetes state. Here,
though, a failed step hid the later checks, serving became shell inside YAML,
and two of the 401s surfaced as JSON parse errors.

## Biggest risk of adopting each option

- **pytest.** Replacing `run.sh` in one change. The nix app that CI calls was
  never run by the builder or the orchestrator (`findings.md` caveat,
  `BAKEOFF.md:46-49`), and the port drops five checks main runs today.
- **shell.** Bash as the language for assertions. #368's response-shape and
  streaming tests would become sed and glob matching over JSON
  (`lib.sh:126-128`), which the builder already says breaks if the output is
  reformatted.
- **chainsaw.** Its step model. A failing step ends the test, so later
  independent checks such as B3 silently don't run. Every HTTP or log check
  becomes shell inside YAML, so the project gains a DSL without dropping shell.
- **unittest.** Each author has to build robustness by hand. The stdlib gives
  no retry or ordered-teardown scaffolding, and this prototype shipped with
  neither: single-shot requests, and a namespace-only teardown on the path
  that wedged.

## Where the experiment is unfair or misleading

1. **The faults only test fast, clean failures. Favours unittest and shell.**
   F1 and F2 fail at once and every time. Nothing is transient, nothing hangs,
   and nothing breaks B's deletion path. So designs without retries look good
   (unittest failed in 244s, pytest in 715s), and teardown after a failure is
   never exercised. The wedge only surfaced because the harness killed a run.
2. **Only pytest's checks ran on a fresh environment. Favours unittest and
   chainsaw.** The other three ran against an environment that had already
   settled, so unittest's retry-free A and chainsaw's in-test waits were never
   tried on fresh clusters.
3. **Two healthy runs can't show flakes. Favours options with little retry
   logic.**
4. **The shared build period shaped pytest's design. Disfavours pytest.** Its
   two-minute request retries exist because other builders' routes rolled the
   proxy pods (`test_serving.py:36-39`). In the orchestrator's serial runs
   they only cost time.
5. **The scope was uneven. Favours unittest and chainsaw.** The brief had
   pytest port the bring-up, while the others left `run.sh` and its checks
   alone and weren't asked to wire CI. So pytest shows the cost of integration,
   in code owned and dropped checks, while unittest and chainsaw hide it. They
   look complete only because `run.sh` still runs its own copy of the checks.
6. **The "`uv.lock` rebuild" finding. Disfavours pytest.** Any change rebuilds
   the function images (see "What I checked"). Counting it as a pytest
   toolchain cost would be wrong.
7. **The wall times aren't like for like. Favours chainsaw.** Chainsaw ran its
   three tests in parallel by default, and the others ran serially.
8. **F1 makes B2 untestable. Favours chainsaw.** With the caller's key broken,
   "stop returning 200" holds before anything is deleted. That fairly exposes
   pytest's B2, but chainsaw escapes only because its test stopped at B1.
9. **The capacity tweak.** The `nodeCount: 5` change lives only in the
   environment. With the committed `nodeCount: 1`, B would lack capacity in CI
   for every option. That's neutral between them, but none of the four has run
   under the configuration CI would use.
