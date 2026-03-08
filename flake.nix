{
  description = "Alberta Buck — Ethereum smart contract development (Foundry/Anvil)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/16c7794d0a28b5a37904d55bcca36003b9109aaa";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        python3Env = pkgs.python3.withPackages (ps: with ps; [
          pytest
          pip
          web3               # Ethereum JSON-RPC client
          eth-abi             # ABI encoding/decoding
          eth-account         # Account/key management
          pyyaml
          requests
        ]);

        commonInputs = with pkgs; [
          # Common tools
          cacert
          git
          gnumake
          openssh
          bash
          bash-completion
          which
          jq
          curl

          # Foundry: forge (compiler/test), anvil (local node), cast (CLI), chisel (REPL)
          foundry

          # Solidity compiler (for IDE/LSP support; Forge also bundles solc)
          solc

          # Node.js (for OpenZeppelin npm deps, optional Hardhat interop)
          nodejs_20
        ];
      in {
        devShells.default = pkgs.mkShell {
          buildInputs = commonInputs ++ [ python3Env ];
          shellHook = ''
            echo "Alberta Buck — Ethereum Development Environment"
            echo ""
            printf "  %-12s %s\n" "forge"  "$(forge --version 2>/dev/null | head -1)"
            printf "  %-12s %s\n" "anvil"  "$(anvil --version 2>/dev/null | head -1)"
            printf "  %-12s %s\n" "cast"   "$(cast --version 2>/dev/null | head -1)"
            printf "  %-12s %s\n" "solc"   "$(solc --version 2>/dev/null | tail -1)"
            printf "  %-12s %s\n" "python" "$(python3 --version 2>/dev/null)"
            printf "  %-12s %s\n" "node"   "$(node --version 2>/dev/null)"
            echo ""
            echo "Commands:  make build | make test | make fork-sepolia | make fork-mainnet"
          '';
        };
      });
}
