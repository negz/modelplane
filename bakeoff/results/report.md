I ran a bake-off between the four options, to see how each one handles the tests we want.

A coding agent built each option on its own branch, from one [brief](https://github.com/negz/modelplane/blob/soggy-bottom/bakeoff/BRIEF.md). Every option had to cover the same checks:

- **Serving.** Authentication, model rewriting, Anthropic translation and the usage record, as `run.sh --verify` checks them today.
- **Lifecycle.** Deploy a second model and check it serves. Delete its ModelService and check routing stops, then foreground-delete its ModelDeployment and check its replicas, endpoints and engine pods go.
- **Placement.** Engine pods run on the GPU pool with a DRA device allocated, and no pod tolerates every taint.
- **Clouds.** An EKS InferenceCluster test that runs only when a maintainer asks for it. Nobody ran it.

The pytest option also ported bring-up to Python. The others kept it in `run.sh`.

I ran each prototype three times against one shared kind environment: healthy, then with two faults injected, then healthy again. The faults changed the caller's API key and added a pod that tolerates every taint. Three more agents then scored each prototype from 1 to 5 on seven criteria, each seeing the options in a different order. All the agents ran on the same model, and I went in leaning toward pytest, so treat the scores as one input among several.

All four prototypes passed both healthy runs, and failed the right checks under the faults.

| Option | Helper lines | Faulted run | Judges' totals, out of 35 |
|---|---|---|---|
| [shell](https://github.com/modelplaneai/modelplane/compare/662c2b9f3600f9cc9a836ebbd32d02573f2c3733...negz:modelplane:shell-shocked) | 159 | 461s | 26, 29, 28 |
| [unittest](https://github.com/modelplaneai/modelplane/compare/662c2b9f3600f9cc9a836ebbd32d02573f2c3733...negz:modelplane:unit-of-measure) | 205 | 244s | 30, 29, 28 |
| [pytest](https://github.com/modelplaneai/modelplane/compare/662c2b9f3600f9cc9a836ebbd32d02573f2c3733...negz:modelplane:py-in-the-sky) | 446 | 715s | 28, 29, 28 |
| [Chainsaw](https://github.com/modelplaneai/modelplane/compare/662c2b9f3600f9cc9a836ebbd32d02573f2c3733...negz:modelplane:saw-point) | 60 | 150s | 22, 23, 21 |

Helper lines are code beyond the tests and bring-up, including license headers. Healthy runs took 107 to 153 seconds. Chainsaw runs its tests in parallel and the others run them serially, so the times aren't like for like. Judges 2 and 3 tied shell, unittest and pytest on points, and broke the tie for pytest. Judge 1 ranked unittest first. All three put shell third and Chainsaw last.

**Shell.** Its summary lists the result of each check, skips included, and some failing checks print a `crossplane resource trace` of the relevant resources. It also ported the `run.sh` checks the brief didn't list. It has no jq, so it matches JSON as text, and the response-shape and streaming checks #368 asks for would lean on that further.

**unittest.** All three judges gave it 5 of 5 for failure output, and it doesn't add dependencies. Its serving requests aren't retried, which a better prototype would fix, and its lifecycle checks rely on unittest running test methods in name order. The judges' concern was that without fixtures, each new scenario grows its own setup and cleanup until the helpers become a homemade pytest.

**pytest.** Fixtures made ordered teardown straightforward. A report hook attaches the conditions of every Modelplane resource in a failed test's namespace, and a marker and a command-line option gate the cloud test. It has the most helper code, plus 300 lines of bring-up. It also adds pytest beside the unittest the function tests use. The bring-up worked on a fresh environment, bringing up both clusters and passing the suite in 15.5 minutes. That run used `uv run` rather than the nix app CI would use, with function images already built and `nodeCount: 5`. Every failing request check retried for 2 minutes, so its faulted run took 12 minutes, and it dropped the `run.sh` checks the brief didn't list. The judges put those down to the prototype rather than the option.

**Chainsaw.** It has the least helper code. Resource checks, such as waiting for a ModelService to go `RoutingReady`, were declarative asserts, and pod-list checks were JMESPath over the cluster's pods. Serving and log checks became shell scripts inside YAML that run curl in a pod, with their output checked by JMESPath. Chainsaw doesn't retry scripts, and [outputs don't carry from one step to the next](https://github.com/kyverno/chainsaw/blob/v0.2.15/pkg/runner/runner.go#L185-L193), so waits became shell loops. A failed operation ends the test unless it's marked `continueOnError`. Under the faults the lifecycle test stopped at its first check, and the other two neither ran nor appeared in the report, though it still described the failing ModelService and cleaned up in order. Two 401s came out as JSON parse errors, such as `invalid character 'C' looking for beginning of value`.

These apply whichever option we pick:

- **HTTP from a pod.** All four prototypes settled on one long-lived curl pod and `kubectl exec`. That worked on Linux. Nobody tried macOS or a CI runner.
- **Capacity for test models.** The fleet scheduler [charges each replica against its pool's declared `nodeCount`](https://github.com/modelplaneai/modelplane/blob/662c2b9f3600f9cc9a836ebbd32d02573f2c3733/functions/compose-model-deployment/function/scheduling.py#L557-L572). With the committed `nodeCount: 1`, a test can't deploy a second model. The shared environment declared 5.
- **Negative checks.** Under the faults, two checks passed because their request got a 401: shell's check that an unclaimed model isn't served, and pytest's check that routing stops. A check that something doesn't serve has to assert the specific refusal, such as a 404.
- **Usage records.** They carry no request ID, so a test can't pick out its own record when anything else calls as the same caller. Every prototype hit this, with four suites sharing the environment.
- **Teardown order.** Deleting a namespace that still holds a ModelDeployment gets stuck. Its provider-kubernetes Objects never finish deleting, because they fail with `cannot apply ProviderConfigUsage ... unable to create new content in namespace ... because it is being terminated`. So teardown has to delete Modelplane resources before their namespace. The pytest and Chainsaw prototypes do, while shell and unittest delete only the namespace. This probably deserves its own issue.
- **Leftover namespaces.** Modelplane never deletes a team namespace's mirror, `mp-<namespace>-<hash>`, on the workload cluster, so every prototype left one behind.

Nothing measured flakiness. There were only two healthy runs, and both faults fail fast and every time, which favours prototypes without retries. Only pytest ported bring-up, and the unittest and Chainsaw prototypes left `run.sh`'s own checks running beside theirs, so the scope was uneven.

The brief, the run logs and each judge's full scoring are in [`bakeoff/`](https://github.com/negz/modelplane/tree/soggy-bottom/bakeoff) on the `soggy-bottom` branch.
