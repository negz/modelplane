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
                hypervisor = "qemu";
                vcpu = 3;
                mem = 12288;

                # User networking gives the VM outbound access through the
                # host, where the host's loopback is 10.0.2.2.
                interfaces = [
                  {
                    type = "user";
                    id = "usernet";
                    mac = "02:00:00:00:00:01";
                  }
                ];

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
                ];

                # The work directory: the job's input in, its output out.
                shares = [
                  {
                    proto = "9p";
                    tag = "work";
                    source = "/mnt/vm/work";
                    mountPoint = "/work";
                  }
                ];
              };

              nix.settings.experimental-features = [
                "nix-command"
                "flakes"
              ];
              virtualisation.docker.enable = true;
              environment.systemPackages = with pkgs; [
                git
                kind
                kubectl
              ];

              users.users.root.password = "";
              services.getty.autologinUser = "root";

              # Run the job, then power off so the runner's qemu exits.
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
                  bash /work/job.sh > /work/job.log 2>&1
                  echo $? > /work/job.exit
                  ${lib.getExe' pkgs.systemd "systemctl"} poweroff
                '';
              };
            }
          )
        ];
      };
    };
}
