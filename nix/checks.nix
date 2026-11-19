# CI checks (nix flake check).
#
# All checks run inside the Nix sandbox without network or filesystem access.
# Unit tests run against uv2nix-built venvs with test sources copied from the
# flake source tree.
#
# Each cluster provider under providers/ is a uv workspace of its own, so its
# functions' tests and type checks run against venvs from its own package set,
# and its uv.lock is checked on its own.
{
  pkgs,
  self,
  functionNames,
  pythonSet,
  providers,
}:
let
  vale = import ./vale.nix { inherit pkgs self; };

  # Each function exports a 'function' Python module, so tests must run from
  # a directory where that module is importable via the venv, and one pytest
  # session can't hold two functions' tests. We copy tests/ from the source
  # tree and run pytest against the venv's Python. We also copy pyproject.toml
  # for its [tool.pytest] config, which pytest finds in its rootdir.
  mkFunctionTest =
    {
      name,
      set ? pythonSet,
      dir ? "functions",
    }:
    let
      venv = set.mkVirtualEnv "${name}-test-env" {
        ${name} = [ ];
        pytest = [ ];
      };
    in
    pkgs.runCommand "modelplane-test-${name}" { } ''
      cp -r ${self}/${dir}/${name}/tests tests
      cp ${self}/pyproject.toml pyproject.toml
      ${venv}/bin/python -m pytest tests
      mkdir -p $out
      touch $out/.tests-passed
    '';
in
{
  # Lint docs prose with Vale. The site that renders this content is the
  # docs-site repo, which builds and link-checks it where it deploys.
  docs-vale = vale.lint "docs" [ "docs/content" ];

  # Lint the skills and agent prompts agents load.
  agents-vale = vale.lint "agents" [
    ".agents"
    ".opencode/agents"
  ];

  # Validate the example manifests the docs show against the generated Pydantic
  # models, so an example that drifts from the live API schema fails CI. Covers
  # everything under docs/manifests/, including the API-reference examples under
  # docs/manifests/reference/. The venv names what the validator imports:
  # crossplane-models, pyyaml and pydantic.
  docs-manifests =
    let
      venv = pythonSet.mkVirtualEnv "docs-manifests-validate-env" {
        crossplane-models = [ ];
        pyyaml = [ ];
        pydantic = [ ];
      };
    in
    pkgs.runCommand "modelplane-docs-manifests" { } ''
      cp -r ${self} src
      chmod -R u+w src
      cd src
      ${venv}/bin/python docs/utils/validate/validate_manifests.py
      mkdir -p $out
      touch $out/.docs-manifests-validated
    '';

  # Each provider's leg in its conformance.yaml copies the GPU taint its
  # cluster composition applies, and the copy can drift. e2e/test_legs.py pins
  # each leg to its composition's source. It needs no cluster, and the E2E
  # Clouds workflow doesn't trigger on the cluster compositions, so it runs
  # here, where a composition-only change does. It imports only e2e.clouds
  # (the standard library and pyyaml) and reads the legs and the
  # compositions' fn.py sources from the tree.
  e2e-legs =
    let
      venv = pythonSet.mkVirtualEnv "e2e-legs-test-env" {
        pytest = [ ];
        pyyaml = [ ];
      };
    in
    pkgs.runCommand "modelplane-e2e-legs" { } ''
      cp -r ${self}/e2e e2e
      cp -r ${self}/providers providers
      cp ${self}/pyproject.toml pyproject.toml
      ${venv}/bin/python -m pytest e2e/test_legs.py
      mkdir -p $out
      touch $out/.e2e-legs-passed
    '';

  # compose-inference-cluster keeps a copy of the DRA drivers each cloud's
  # serving stack supports, to reject an unsupported class before composing a
  # ServingStack. The checker reads the copy from source and compares it with
  # compose-serving-stack's, which it imports from that function's venv.
  supported-drivers-mirror =
    let
      venv = pythonSet.mkVirtualEnv "supported-drivers-mirror-env" {
        compose-serving-stack = [ ];
      };
    in
    pkgs.runCommand "modelplane-supported-drivers-mirror" { } ''
      cd ${self}
      ${venv}/bin/python hack/check_supported_drivers.py
      mkdir -p $out
      touch $out/.supported-drivers-mirror-checked
    '';

  # The provider SDK's own tests.
  test-sdk =
    let
      venv = pythonSet.mkVirtualEnv "sdk-test-env" {
        modelplane-sdk = [ ];
        pytest = [ ];
      };
    in
    pkgs.runCommand "modelplane-test-sdk" { } ''
      cp -r ${self}/sdk/python/tests tests
      cp ${self}/pyproject.toml pyproject.toml
      ${venv}/bin/python -m pytest tests
      mkdir -p $out
      touch $out/.tests-passed
    '';

  # Check that Modelplane names no cluster provider, and that each provider
  # reaches nothing of Modelplane's but the SDK and the contract's models. The
  # checker uses only the standard library, so it runs on the plain
  # interpreter.
  boundaries =
    pkgs.runCommand "modelplane-boundaries"
      {
        nativeBuildInputs = [ pkgs.python3 ];
      }
      ''
        cd ${self}
        python3 hack/check_boundaries.py
        mkdir -p $out
        touch $out/.boundaries-checked
      '';

  # Check each provider against what Modelplane relies on that its schemas
  # can't say: its Composition for a ProvisionedCluster declares what
  # Modelplane selects and gates it on, and exactly the DRA drivers its device
  # stack carries; its package objects are named for it; it has a
  # conformance.yaml; and its device stack passes the SDK's component-list
  # checks and carries the SDK's NVIDIA pins. The checker imports the device
  # stack, so it runs in that function's venv, from the provider's package
  # set. It checks every provider before failing.
  provider-declarations =
    let
      venvs = pkgs.lib.mapAttrsToList (name: p: {
        inherit name;
        venv = p.pythonSet.mkVirtualEnv "${name}-declarations-env" { compose-device-stack = [ ]; };
      }) providers;
    in
    pkgs.runCommand "modelplane-provider-declarations" { } ''
      failed=()
      ${pkgs.lib.concatMapStrings (v: ''
        ${v.venv}/bin/python -m modelplane_sdk.check ${self}/providers/${v.name} || failed+=(${v.name})
      '') venvs}
      if [ ''${#failed[@]} -gt 0 ]; then
        echo "providers failing their declarations: ''${failed[*]}" >&2
        exit 1
      fi
      mkdir -p $out
      touch $out/.provider-declarations-checked
    '';

  # Check the function unit tests against the rules in CONTRIBUTING.md's Tests
  # section that an AST can decide, such as how a table and its test are laid
  # out and what a case's name and reason look like. The checker uses only the
  # standard library, so it runs on the plain interpreter.
  function-test-style =
    pkgs.runCommand "modelplane-function-test-style"
      {
        nativeBuildInputs = [ pkgs.python3 ];
      }
      ''
        cd ${self}
        python3 hack/check_function_tests.py
        mkdir -p $out
        touch $out/.function-test-style-checked
      '';

  # Check that no composition function renders a matchControllerRef selector
  # inside a list. The managed resource CRDs' lists are atomic under
  # server-side apply, so a selector in a list item makes the composition and
  # the reference resolver replace each other's items and the object never
  # settles (#517). The checker uses only the standard library, so it runs on
  # the plain interpreter.
  list-selectors =
    pkgs.runCommand "modelplane-list-selectors"
      {
        nativeBuildInputs = [ pkgs.python3 ];
      }
      ''
        cd ${self}
        python3 hack/check_list_selectors.py
        mkdir -p $out
        touch $out/.list-selectors-checked
      '';

  # Type-check our Python with ty. Every function's package is named
  # `function`, so in one ty run one function's `from function import fn` could
  # resolve to another function's package. ty instead runs once per function,
  # against a venv that provides the function's dependencies, pytest, which the
  # tests import, and the protobuf type stubs ty needs to resolve the SDK's
  # generated Struct and Duration. It then runs on e2e/, against the packages
  # the e2e app runs the tests with (see apps.nix), and on hack/ with no venv:
  # its checkers use the standard library, apart from the supported drivers
  # checker's import of compose-serving-stack, which ty can't resolve there.
  #
  # Unlike mkFunctionTest, which runs the function module from the venv, ty
  # checks the source, so we copy it from the tree: each function's function/
  # and tests/ to a directory of its own that ty takes as the project, and e2e/
  # and hack/ to the build directory. Each of those gets a copy of
  # pyproject.toml for the [tool.ty] config, where the target Python version is
  # set. ty also resolves first-party imports from the directory holding that
  # config, so without a copy in a function's directory `function` would
  # resolve to the package installed in the venv instead of the copied source.
  #
  # A failed run doesn't stop the rest, so one build reports every failure, and
  # the log names each directory that failed.
  ty =
    let
      tyVenv =
        set: name:
        set.mkVirtualEnv "${name}-ty-env" {
          ${name} = [ ];
          pytest = [ ];
          types-protobuf = [ ];
        };
      functionVenvs =
        map (name: {
          dir = "functions/${name}";
          venv = tyVenv pythonSet name;
        }) functionNames
        ++ pkgs.lib.concatLists (
          pkgs.lib.mapAttrsToList (
            provider: p:
            map (name: {
              dir = "providers/${provider}/functions/${name}";
              venv = tyVenv p.pythonSet name;
            }) p.functionNames
          ) providers
        );
      sdkVenv = pythonSet.mkVirtualEnv "sdk-ty-env" {
        modelplane-sdk = [ ];
        pytest = [ ];
        types-protobuf = [ ];
      };
      e2eVenv = pythonSet.mkVirtualEnv "e2e-ty-env" {
        pytest = [ ];
        kubernetes = [ ];
        crossplane-models = [ ];
        modelplane-sdk = [ ];
        pydantic = [ ];
        pyyaml = [ ];
      };
    in
    pkgs.runCommand "modelplane-ty"
      {
        nativeBuildInputs = [ pkgs.unstable.ty ];
      }
      ''
        cp -r ${self}/e2e ${self}/hack ${self}/pyproject.toml .

        failed=()
        check() {
          local name="$1"
          shift
          echo "ty check $name"
          ty check --no-progress "$@" || failed+=("$name")
        }

        check_function() {
          local dir="$1" venv="$2"
          mkdir -p "$dir"
          cp -r "${self}/$dir/function" "${self}/$dir/tests" pyproject.toml "$dir"
          check "$dir" --project "$dir" --python "$venv" "$dir"
        }

        ${pkgs.lib.concatMapStrings (f: ''
          check_function ${f.dir} ${f.venv}
        '') functionVenvs}
        cp -r ${self}/sdk sdk
        check sdk --python ${sdkVenv} sdk
        check e2e --python ${e2eVenv} e2e
        check hack --python ${sdkVenv} hack

        if [ ''${#failed[@]} -gt 0 ]; then
          echo "ty found errors in: ''${failed[*]}" >&2
          exit 1
        fi
        mkdir -p $out
        touch $out/.ty-passed
      '';

  python =
    pkgs.runCommand "modelplane-python-checks"
      {
        nativeBuildInputs = [ pkgs.unstable.ruff ];
      }
      ''
        cp -r ${self} src
        chmod -R u+w src
        cd src
        ruff format --check functions/ providers/ sdk/ docs/utils/validate/ hack/ e2e/
        ruff check functions/ providers/ sdk/ docs/utils/validate/ hack/ e2e/
        mkdir -p $out
        touch $out/.python-checks-passed
      '';

  # Fail if the AICR-derived device stacks are stale: regenerate each AICR
  # provider's from the embedded catalog with the pinned aicr (fully offline
  # - the recipes resolve from the catalog the binary embeds) and diff
  # against what's checked in. Catches a generator change committed without
  # `nix run .#stacks`, a hand-edit to a generated file, and any
  # non-determinism in the pipeline.
  stacks-current =
    pkgs.runCommand "modelplane-stacks-current"
      {
        nativeBuildInputs = [
          pkgs.aicr
          (pkgs.python312.withPackages (ps: [ ps.pyyaml ]))
          pkgs.unstable.ruff
        ];
      }
      ''
        cp -r ${self} src
        chmod -R u+w src
        cd src
        export HOME=$TMPDIR
        for generate in providers/*/functions/compose-device-stack/generate.py; do
          dir=$(dirname "$generate")
          PYTHONPATH=sdk/python python3 "$dir/generate.py"
          ruff format "$dir/function/generated/"
          diff -ru "${self}/$dir/function/generated" "$dir/function/generated"
        done
        mkdir -p $out
        touch $out/.stacks-current
      '';

  # Fail if any hand-written source file is missing its Apache 2.0 license
  # header. Scoped to the files we author: the composition functions, the
  # providers, the SDK, the docs manifest validator, the scripts in hack/, and
  # the end-to-end tests.
  # Generated models under schemas/python carry their own codegen banner, and
  # config (*.toml) and vendored upstream CRDs (*.yaml) are excluded.
  # addlicense -check only reads, so it runs against the store path directly.
  # Run 'nix run .#fix' to add any missing headers.
  license =
    pkgs.runCommand "modelplane-license-check"
      {
        nativeBuildInputs = [ pkgs.addlicense ];
      }
      ''
        cd ${self}
        addlicense -check \
          -ignore '**/*.toml' \
          -ignore '**/*.yaml' \
          -ignore '**/*.yml' \
          -ignore 'providers/*/schemas/**' \
          functions/ providers/ sdk/ docs/utils/validate/ hack/ e2e/ nix.sh
        mkdir -p $out
        touch $out/.license-check-passed
      '';

  shell-lint =
    pkgs.runCommand "modelplane-shell-lint"
      {
        nativeBuildInputs = [
          pkgs.findutils
          pkgs.shellcheck
          pkgs.shfmt
        ];
      }
      ''
        cd ${self}
        find . -name '*.sh' -type f | while read -r script; do
          shellcheck "$script"
          shfmt -d "$script"
        done
        mkdir -p $out
        touch $out/.shell-lint-passed
      '';

  nix-lint =
    pkgs.runCommand "modelplane-nix-lint"
      {
        nativeBuildInputs = [
          pkgs.findutils
          pkgs.statix
          pkgs.deadnix
          pkgs.nixfmt
        ];
      }
      ''
        statix check ${self}
        find ${self} -name '*.nix' -type f -exec deadnix --fail {} +
        find ${self} -name '*.nix' -type f -exec nixfmt --check {} +
        mkdir -p $out
        touch $out/.nix-lint-passed
      '';

  # Fail if uv.lock is out of sync with any pyproject.toml in the workspace.
  # uv lock --check resolves the workspace against the lockfile without
  # writing it. The sandbox has no network, no writable HOME, and no
  # /bin/sh for uv's interpreter discovery, so:
  #
  #   --offline                  skip the network
  #   UV_CACHE_DIR=...           write cache into the build dir
  #   --no-managed-python        don't try to download a Python
  #   --python ${pkgs.python3}/bin/python   use the nix-provided interpreter
  uv-lock =
    pkgs.runCommand "modelplane-uv-lock"
      {
        nativeBuildInputs = [
          pkgs.unstable.uv
          pkgs.python3
        ];
        env.UV_CACHE_DIR = "uv-cache";
      }
      ''
        cp -r ${self} src
        chmod -R u+w src
        cd src
        for workspace in . ${
          pkgs.lib.concatMapStringsSep " " (p: "providers/${p}") (builtins.attrNames providers)
        }; do
          (cd "$workspace" && uv lock --check --offline \
            --no-managed-python \
            --python ${pkgs.python3}/bin/python)
        done
        mkdir -p $out
        touch $out/.uv-lock-passed
      '';
}
// builtins.listToAttrs (
  map (name: {
    name = "test-${name}";
    value = mkFunctionTest { inherit name; };
  }) functionNames
)
// builtins.listToAttrs (
  pkgs.lib.concatLists (
    pkgs.lib.mapAttrsToList (
      provider: p:
      map (name: {
        name = "test-${provider}-${name}";
        value = mkFunctionTest {
          inherit name;
          set = p.pythonSet;
          dir = "providers/${provider}/functions";
        };
      }) p.functionNames
    ) providers
  )
)
