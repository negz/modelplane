# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

{
  description = "A microVM for running the review agent, and a smoke test for it";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    microvm = {
      url = "github:microvm-nix/microvm.nix/b7bd556974fa8a171b5b639200e81d52244dfde4";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { nixpkgs, microvm, ... }:
    {
      # The VM shares nothing with the runner. Its job arrives as a tar on a
      # read-only disk, and it writes its results as a tar to a second disk,
      # which the runner reads without mounting. Its only network is a tap
      # device the runner firewalls.
      nixosConfigurations.agent = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          microvm.nixosModules.microvm
          (
            { pkgs, lib, ... }:
            {
              system.stateVersion = "26.05";
              networking.hostName = "agent";

              microvm = {
                hypervisor = "firecracker";
                vcpu = 3;
                mem = 12288;

                interfaces = [
                  {
                    type = "tap";
                    id = "vm0";
                    mac = "02:00:00:00:00:01";
                  }
                ];

                # Firecracker attaches the store disk first, then these in
                # order, so they're vdb through vde.
                writableStoreOverlay = "/nix/.rw-store";
                volumes = [
                  {
                    image = "/mnt/vm/store.img";
                    mountPoint = "/nix/.rw-store";
                    size = 40960;
                  }
                  {
                    image = "/mnt/vm/docker.img";
                    mountPoint = "/var/lib/docker";
                    size = 20480;
                  }
                  {
                    image = "/mnt/vm/in.tar";
                    mountPoint = null;
                    autoCreate = false;
                    readOnly = true;
                  }
                  {
                    image = "/mnt/vm/out.tar";
                    mountPoint = null;
                    autoCreate = false;
                  }
                ];
              };

              networking.useNetworkd = true;
              networking.useDHCP = false;
              networking.enableIPv6 = false;
              systemd.network.networks."10-vm0" = {
                matchConfig.MACAddress = "02:00:00:00:00:01";
                address = [ "192.168.100.2/30" ];
                routes = [ { Gateway = "192.168.100.1"; } ];
                dns = [
                  "1.1.1.1"
                  "8.8.8.8"
                ];
              };

              nix.settings.experimental-features = [
                "nix-command"
                "flakes"
              ];
              virtualisation.docker.enable = true;
              environment.systemPackages = with pkgs; [
                curl
                git
                kind
                kubectl
              ];

              users.users.root.password = "";
              services.getty.autologinUser = "root";

              # Unpack the job, run it, write /out back as a tar, then power
              # off so the runner's firecracker exits.
              systemd.services.job = {
                wantedBy = [ "multi-user.target" ];
                after = [
                  "network-online.target"
                  "docker.service"
                ];
                wants = [ "network-online.target" ];
                path = with pkgs; [
                  bash
                  coreutils
                  curl
                  procps
                  gnutar
                  git
                  nix
                  docker
                  kind
                  kubectl
                ];
                serviceConfig = {
                  Type = "oneshot";
                  StandardOutput = "journal+console";
                  StandardError = "journal+console";
                };
                script = ''
                  set +e
                  mkdir -p /work /out
                  tar -xf /dev/vdd -C /work
                  bash /work/job.sh > /out/job.log 2>&1
                  echo $? > /out/job.exit
                  tar -cf /dev/vde -C /out .
                  sync
                  ${lib.getExe' pkgs.systemd "systemctl"} poweroff
                '';
              };
            }
          )
        ];
      };
    };
}
