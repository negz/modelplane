I ran a bake-off between the four options, to see how each one handles the tests we want.

A coding agent built each option on its own branch, from one [brief](https://github.com/negz/modelplane/blob/soggy-bottom/bakeoff/BRIEF.md). Every option had to cover the same checks:

- **Serving.** Authentication, model rewriting, Anthropic translation and the usage record, as `run.sh --verify` checks them today.
- **Lifecycle.** Deploy a second model and check it serves. Delete its ModelService and check routing stops, then foreground-delete its ModelDeployment and check its replicas, endpoints and engine pods go.
- **Placement.** Engine pods run on the GPU pool with a DRA device allocated, and no pod tolerates every taint.
- **Clouds.** An EKS InferenceCluster test that runs only when a maintainer asks for it. Nobody ran it.

The pytest option also ported bring-up to Python. The others kept it in `run.sh`.

I ran each prototype three times against one shared kind environment: healthy, then with two faults injected, then healthy again. The faults changed the caller's API key and added a pod that tolerates every taint. Three more agents then scored each prototype from 1 to 5, each seeing the options in a different order. All the agents ran on the same model, and I went in leaning toward pytest, so treat the scores as one input among several.

They scored these criteria, which count equally in the totals:

- Legibility
- Failure output
- Cost of adding the next test
- How much code we'd own
- Toolchain fit
- Coverage
- Robustness

All four prototypes passed both healthy runs, and failed the right checks under the faults.

| Option | Helper lines | Faulted run | Judges' totals, out of 35 |
|---|---|---|---|
| [shell](https://github.com/modelplaneai/modelplane/compare/662c2b9f3600f9cc9a836ebbd32d02573f2c3733...negz:modelplane:shell-shocked) | 159 | 461s | 26, 29, 28 |
| [unittest](https://github.com/modelplaneai/modelplane/compare/662c2b9f3600f9cc9a836ebbd32d02573f2c3733...negz:modelplane:unit-of-measure) | 205 | 244s | 30, 29, 28 |
| [pytest](https://github.com/modelplaneai/modelplane/compare/662c2b9f3600f9cc9a836ebbd32d02573f2c3733...negz:modelplane:py-in-the-sky) | 446 | 715s | 28, 29, 28 |
| [Chainsaw](https://github.com/modelplaneai/modelplane/compare/662c2b9f3600f9cc9a836ebbd32d02573f2c3733...negz:modelplane:saw-point) | 60 | 150s | 22, 23, 21 |

Helper lines are code beyond the tests and bring-up, including license headers. Healthy runs took 107 to 153 seconds. Chainsaw runs its tests in parallel and the others run them serially, so the times aren't like for like. Judges 2 and 3 tied shell, unittest and pytest on points, and broke the tie for pytest. Judge 1 ranked unittest first. All three put shell third and Chainsaw last.

In short:

- **shell** extends `run.sh` with a small helper library. It adds nothing new to the repo, but it checks JSON by matching text, which gets harder as checks look deeper into responses.
- **unittest** writes the checks in Python's standard library, and leaves bring-up in `run.sh`. It doesn't add dependencies, but it has nothing like pytest's fixtures, so each scenario writes its own setup and cleanup.
- **pytest** writes bring-up and checks in Python, with fixtures for shared setup and teardown. It was the cheapest to add a test to, but it has the most helper code and adds pytest as a dependency.
- **Chainsaw** writes tests as YAML. Checks on Kubernetes resources are declarative, but requests to the gateways and log checks become shell scripts inside the YAML.

The brief, the run logs and each judge's full scoring are in [`bakeoff/`](https://github.com/negz/modelplane/tree/soggy-bottom/bakeoff) on the `soggy-bottom` branch.
