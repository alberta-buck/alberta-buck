{
  description = "Alberta Buck — Ethereum smart contract development (Foundry/Anvil)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        python3Env = pkgs.python3.withPackages (ps: with ps; [
          ipykernel
          ipython
          matplotlib
          numpy
          scipy               # LQR / Riccati solvers (WP-16 gain derivation); numpy iteration is the fallback
          pandas
          tabulate
          pytest
          pip
          web3                # Ethereum JSON-RPC client
          eth-abi             # ABI encoding/decoding
          eth-account         # Account/key management
          matplotlib          # Plotting / visualization
          python-dotenv       # Load .env files
          pyyaml
          requests
        ]);

        commonInputs = with pkgs; [
          # Common tools
          cacert
          git
          gnumake
          gnused
          openssh
          bash
          bash-completion
          which
          jq
          curl
          rsync               # moving simulation vectors between the grid machines

          # Foundry: forge (compiler/test), anvil (local node), cast (CLI), chisel (REPL)
          foundry

          # Solidity compiler (for IDE/LSP support; Forge also bundles solc)
          solc

          # Node.js (for OpenZeppelin npm deps, Hardhat interop, snarkjs/circomlib)
          nodejs_22

          # SNARK toolchain:
          #   circom  - compiles .circom -> R1CS / WASM witness generator
          #   snarkjs - Groth16/PLONK setup, proving, verifier-Solidity export
          #             (installed via npm; see package.json)
          #   circomlib - Poseidon/Merkle/Schnorr gadget library (npm)
          circom

          # Rust: the core/rust kernel workspace (buck-math, buck-identity,
          # buck-registry, buck-wallet) and its PyO3 / wasm-bindgen bindings.
          # Previously absent, which meant `nix develop` silently borrowed
          # cargo from whatever the developer happened to have in
          # ~/.nix-profile -- so `make nix-core-build` worked here and
          # nowhere else.
          rustc
          cargo
          # wasm32-unknown-unknown links with lld; nixpkgs rustc ships the
          # target's std but not a linker for it, and cargo fails with
          # "linker `lld` not found" without this.
          lld
          # binaryen = wasm-opt, the size pass wasm-pack shells out to for
          # the published alberta-buck-kernel package.
          binaryen

          # C++ toolchain for SNARK verifier testing
          gcc14
          pkg-config
          nlohmann_json
          gmp
          nasm
        ];
      in {
        devShells.default = pkgs.mkShell {
          buildInputs = commonInputs ++ [ python3Env ];
          shellHook = ''
            export SOLC_PATH="${pkgs.solc}/bin/solc"

            echo "Alberta Buck — Ethereum Development Environment"
            echo ""
            printf "  %-12s %s\n" "forge"  "$(forge --version 2>/dev/null | head -1)"
            printf "  %-12s %s\n" "anvil"  "$(anvil --version 2>/dev/null | head -1)"
            printf "  %-12s %s\n" "cast"   "$(cast --version 2>/dev/null | head -1)"
            printf "  %-12s %s\n" "solc"   "$(solc --version 2>/dev/null | tail -1)"
            printf "  %-12s %s\n" "python" "$(python3 --version 2>/dev/null)"
            printf "  %-12s %s\n" "node"   "$(node --version 2>/dev/null)"
            printf "  %-12s %s\n" "circom" "$(circom --version 2>/dev/null | head -1)"
            printf "  %-12s %s\n" "cargo"  "$(cargo --version 2>/dev/null)"
            printf "  %-12s %s\n" "wasm-opt" "$(wasm-opt --version 2>/dev/null)"
            # snarkjs and circomlib come from npm; bootstrap on first entry.
            if [ ! -d node_modules ] && [ -f package.json ]; then
              echo ""
              echo "[flake] installing npm dev deps (snarkjs, circomlib) ..."
              npm install --no-audit --no-fund --loglevel=error
            fi
            if [ -x node_modules/.bin/snarkjs ]; then
              export PATH="$PWD/node_modules/.bin:$PATH"
              printf "  %-12s %s\n" "snarkjs" "$(snarkjs --version 2>/dev/null | head -1 || echo 'installed')"
            fi
            echo ""
            echo "Commands:  make build | make test | make fork-sepolia | make fork-mainnet"
          '';
        };
      });
}
