# E2E bake-off brief

Modelplane's e2e test is `e2e/run.sh`. Issue #473 (`gh issue view 473`) asks
how the project should write e2e tests. Four options are being prototyped in
parallel, each by a different agent, against the same scenarios below. Once
they're done, each prototype is run the same way, against a healthy
environment and then with injected faults. Independent judges then score the
prototypes, their run logs and their notes against the rubric at the end of
this brief.

Your job is to build the best honest version of your option. It is not to win.
The judges run your code and check your claims, and overstatement counts
against you. Don't compare your option with the others.

## Options

1. **shell**: keep extending `e2e/run.sh`.
2. **unittest**: keep bring-up in `run.sh`, and write the checks with Python's
   stdlib `unittest`. PR #420 (`gh pr diff 420`) does this with the suite
   running in a pod. Where the suite runs is your call.
3. **pytest**: write bring-up and tests in Python with pytest, in the style of
   Crossplane's e2e tests (read `~/control/crossplane/crossplane/test/e2e`,
   especially its README): features with setup, assess and teardown phases,
   YAML fixtures, and a library of reusable helpers. Write the bring-up, but
   don't run it (see Rules).
4. **chainsaw**: write the tests with Chainsaw. Bring-up stays in `run.sh`. PR
   #472 (`gh pr diff 472`) adds a first Chainsaw test you can learn from.

## The shared environment

An environment is already running. It was brought up with
`nix run .#e2e -- --verify`, which passed, so `e2e/manifests/` is applied.

- **Clusters.** The control plane is kube context `kind-modelplane-e2e-local`.
  The workload cluster is `kind-modelplane-e2e-workload`.
- **Capacity.** For the bake-off only, InferenceCluster `local` declares
  `nodeCount: 5`, so there's room for the `ml-team` model plus one more model
  per option.
- **Gateway.** InferenceGateway `local` publishes its OpenAI base URL at
  `status.endpoints.openAI` and its Anthropic one at
  `status.endpoints.anthropic`. The caller key is `sk-e2e-caller`.
- **Networking.** Gateway addresses are on the kind Docker network. This host
  runs Linux and can route to them, but a macOS host can't, and the tests must
  work there. So HTTP requests to a gateway must come from a pod, as `run.sh`
  does. Kubernetes API calls from the host are fine.
- **Engine pods.** Modelplane mirrors namespace `ml-team` into a namespace on
  the workload cluster named `mp-ml-team-<hash>`. An engine pod carries the
  label `modelplane.ai/serving=<ModelReplica name>`, and the ModelReplicas for
  a ModelDeployment live in the ModelDeployment's namespace on the control
  plane. Don't hardcode hashes.
- **Tools.** `nix develop` in your worktree provides `kubectl`, `kind`,
  `crossplane`, Python and more. Get anything else with `nix run nixpkgs#...`
  or by adding it to the flake, and say which you did in your notes.

## Rules

- **Stay in your sandbox.** You may create, change and delete only:
  - namespaces named `bakeoff-<option>` or starting with `bakeoff-<option>-`,
    on either cluster, and anything inside them;
  - whatever Modelplane composes from your own ModelDeployment and
    ModelService.

  Reading anything is fine. Never change or delete the `ml-team` resources,
  the InferenceGateway, the InferenceCluster, providers, or Secrets outside
  your namespaces.
- **Don't rebuild the environment.** Never run `nix run .#e2e`,
  `crossplane project run` or `crossplane project stop`, or create or delete a
  kind cluster. Don't run bring-up code.
- **You're not alone.** The other three agents test against this environment
  at the same time. For example, the Envoy access log holds their requests
  too, and they all authenticate as caller `e2e`.
- **Where the work goes.** Work in your worktree, laid out as your option
  would be in a real PR, and commit with `git commit -s`. Don't push.
- **Quality.** Write it as you would for a PR, and keep it as small as the job
  allows. Lint it with the repo's tooling (`nix flake check` runs shellcheck
  and ruff), and comment where the code isn't obvious.
- **Time.** Aim to finish within about 90 minutes. Scenario B takes a few
  minutes a run, so don't run it in a tight loop.

## Deliverables

Commit your prototype and `e2e/BAKEOFF.md`. Keep the notes to about 60 lines
of plain facts:

- the one command that runs scenarios A to C against the running
  environment, and the command that runs D;
- what CI would need, such as changes to the nix app or the workflow (don't
  implement these unless it's trivial);
- what you implemented, and what you didn't;
- limitations you hit, each with evidence such as a doc link, source or log.

Your final message should be under 200 words. Give the run command, whether
A to C passed on your last run, and anything that doesn't work.

## Scenarios

Name each check as it's numbered here (A1, A2, and so on), so outputs can be
compared. Take request bodies from `run.sh`: the mock engine's token counts
depend on them.

### A. Serving

These checks use the existing ModelService `ml-team/mock` and are read-only.

- **A1.** An OpenAI chat completion naming the ModelService's `status.model`,
  with `Authorization: Bearer sk-e2e-caller`, returns 200.
- **A2.** The same request returns 401 with no key, and 401 with key
  `sk-wrong`.
- **A3.** The response body's `model` is `ml-team/mock-demo`, the served name,
  and not the name the caller asked for.
- **A4.** An Anthropic `/v1/messages` request at `status.endpoints.anthropic`
  returns 200. It sends the key in `x-api-key` and sets
  `anthropic-version: 2023-06-01`.
- **A5.** The InferenceGateway's Envoy access log holds a usage record with:
  - `caller` `e2e`, `service` `ml-team/mock` and `served_model`
    `ml-team/mock-demo`;
  - `input_tokens` 12, `output_tokens` 9 and `total_tokens` 21;
  - `status` 200.

  Its proxy pods are on the workload cluster, in namespace
  `envoy-gateway-system`, with label
  `gateway.envoyproxy.io/owning-gateway-name=inference-gateway`. Read container
  `envoy` from both pods.

### B. Lifecycle

This scenario runs in your own namespace, `bakeoff-<option>`, on the control
plane.

**Setup.** Create:
- namespace `bakeoff-<option>`;
- ModelDeployment `mock-<option>`, identical to
  `e2e/manifests/40-model-deployment.yaml` apart from its name and namespace;
- ModelService `mock-<option>`, selecting it the way
  `e2e/manifests/50-model-service.yaml` does.

**Checks.**
- **B1.** The ModelService becomes `RoutingReady`. A request naming its
  `status.model` returns 200 and reports served model
  `bakeoff-<option>/mock-<option>`.
- **B2.** Delete the ModelService. Within 5 minutes, requests naming it stop
  returning 200.
- **B3.** Delete the ModelDeployment with foreground propagation. Its
  ModelReplicas and ModelEndpoints disappear from the control plane, and its
  engine pods disappear from the workload cluster.

**Teardown.** Remove everything the scenario created, whether or not it
passed.

**Timings.** I ran B by hand on this environment with option name `smoke`:
- The ModelService went `RoutingReady` about 95 seconds after I applied the
  manifests. A request then returned 200 with `"model":
  "bakeoff-smoke/mock-smoke"`.
- After I deleted the ModelService, the next request, 6 seconds later,
  returned 404.
- A foreground delete of the ModelDeployment finished in 8 seconds. The engine
  pod was `Terminating` right after.
- The mirrored namespace on the workload cluster, `mp-bakeoff-smoke-<hash>`,
  stayed after I deleted `bakeoff-smoke`.

### C. Placement

These checks run against the workload cluster and are read-only.

- **C1.** Every engine pod for `ml-team/mock-demo` runs on a node labelled
  `modelplane.ai/pool=gpu-synthetic`. Each one's ResourceClaim has a device
  allocated from driver `gpu.example.com`.
- **C2.** No pod outside `kube-system` carries a wildcard toleration, meaning
  `operator: Exists` with no key, whatever the effect. The exception is an
  allowlist that holds only the `prometheus-node-exporter` DaemonSet's pods in
  namespace `monitoring`, which carry a wildcard today. Make the allowlist
  easy to edit, and report each offending pod by name.

### D. Clouds

D1 is gated and never run in the bake-off. It creates an EKS InferenceCluster
based on `docs/manifests/getting-started/eks/platform.yaml`, asserts that it
becomes Ready, then deletes it and asserts it's gone.

- It's skipped by default, and runs only when someone asks for it
  explicitly.
- Show how a maintainer would ask for it, and how its environment differs
  from the kind one: it has no workload kind cluster, and it needs AWS
  credentials.
- It doesn't have to work.

## Rubric

Judges score each criterion from 1 to 5, citing evidence.

1. **Legibility.** Can someone new to the repo tell what each test checks, and,
   when one fails, why?
2. **Failure output.** On the faulted runs, does the output point at the
   fault? Does it report every independent failure, with useful diagnostics?
3. **Cost of the next test.** How much work would adding another scenario
   take?
4. **Code we'd own.** How much framework and helper code sits beyond the tests
   themselves, and how complex is it?
5. **Toolchain.** Which languages, tools and dependencies does it add, and how
   well does it fit a repo that's Python, shell and Nix?
6. **Coverage.** Were A to D all done, and how naturally does the option
   express serving, lifecycle, placement and gating?
7. **Robustness.** Consider waits, retries and timeouts, isolation, cleanup
   on failure, and the risk of flakes.
