# External dependencies not available in nixpkgs.
#
# The Crossplane CLI comes from the master channel of the release CDN, not
# the crossplane/cli flake: we need `crossplane project run --no-default-mrap`,
# which no tagged release ships yet. To bump: pick a version from
# https://cli.crossplane.io/master/current/version and refresh the hashes:
#
#   for p in linux_amd64 linux_arm64 darwin_arm64; do
#     curl -sL "https://cli.crossplane.io/master/<version>/bin/$p/crossplane" | sha256sum
#   done
{ pkgs, ... }:
let
  version = "v2.6.0-rc.0.306.g763f9f2";
  channel = "master";

  hashes = {
    linux_amd64 = "eead6bf85f18d62e29e32489197f408b5612075a58e8733c3ac8321f55128fb1";
    linux_arm64 = "4a339e5e346da5032f67055aeddb39e0006f8f9ff6b0d0f364dedee16f106a21";
    darwin_arm64 = "f2fc76665b80c27d48c6abf40ffd531d46687c23e0af2fa5b2eccb2c3de0b9a6";
  };

  platforms = {
    x86_64-linux = "linux_amd64";
    aarch64-linux = "linux_arm64";
    aarch64-darwin = "darwin_arm64";
  };
in
{
  crossplane =
    { system }:
    let
      platform = platforms.${system};
    in
    pkgs.stdenvNoCC.mkDerivation {
      pname = "crossplane-cli";
      inherit version;

      src = pkgs.fetchurl {
        url = "https://cli.crossplane.io/${channel}/${version}/bin/${platform}/crossplane";
        sha256 = hashes.${platform};
      };

      dontUnpack = true;

      installPhase = ''
        runHook preInstall
        install -Dm755 $src $out/bin/crossplane
        runHook postInstall
      '';

      meta = {
        description = "CLI for interacting with Crossplane";
        homepage = "https://docs.crossplane.io/latest/cli/";
        license = pkgs.lib.licenses.asl20;
        mainProgram = "crossplane";
      };
    };
}
