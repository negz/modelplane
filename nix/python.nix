# A uv workspace's Python packages, built by uv2nix from its uv.lock, for
# virtualenvs that run on the build host. The function images build their own,
# per target architecture (see functions.nix). Modelplane's workspace is the
# repository root, and each cluster provider's is its directory under
# providers/, which workspaceRoot names.
{
  pkgs,
  self,
  pyproject-nix,
  uv2nix,
  pyproject-build-systems,
  workspaceRoot ? self,
}:
let
  workspace = uv2nix.lib.workspace.loadWorkspace { inherit workspaceRoot; };
in
(pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python312; }).overrideScope (
  pkgs.lib.composeManyExtensions [
    pyproject-build-systems.overlays.wheel
    (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
  ]
)
