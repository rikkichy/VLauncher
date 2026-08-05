{

  description = "vhelper - make VTubing suck less on Linux";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ];
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonEnv = pkgs.python3.withPackages (ps: [ ps.pygobject3 ]);
        in
        rec {
          vhelper = pkgs.stdenv.mkDerivation {
            pname = "vhelper";
            version = "0.2.0";
            src = self;

            nativeBuildInputs = with pkgs; [
              makeWrapper
              wrapGAppsHook4
              gobject-introspection
            ];
            buildInputs = with pkgs; [
              gtk4
              libadwaita
            ];

            dontBuild = true;
            installFlags = [ "PREFIX=${placeholder "out"}" ];

            dontWrapGApps = true;
            preFixup = ''
              rm $out/bin/vhelper
              makeWrapper ${pythonEnv}/bin/python3 $out/bin/vhelper \
                --add-flags "$out/share/vhelper/vhelper.py" \
                --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.protontricks ]} \
                "''${gappsWrapperArgs[@]}"
            '';

            meta = {
              description = "Make VTubing suck less on Linux";
              homepage = "https://github.com/rikkichy/vhelper";
              license = pkgs.lib.licenses.gpl3Plus;
              mainProgram = "vhelper";
              platforms = pkgs.lib.platforms.linux;
            };
          };
          default = vhelper;
        }
      );
    };
}
