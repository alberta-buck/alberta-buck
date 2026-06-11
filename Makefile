#
# Alberta Buck — Ethereum Smart Contracts
#
# Foundry/Anvil-based build, test, and local fork environment.
#

SHELL		= /bin/bash

# RPC endpoints for forking.  Override via environment or .env file.
# Free tier: https://dashboard.alchemy.com/ or https://infura.io/
# Local node: http://localhost:8545 (Reth, Geth, Erigon)
-include .env
ALCHEMY_API_TOKEN	?=
SEPOLIA_RPC_URL		?= https://eth-sepolia.g.alchemy.com/v2/$(ALCHEMY_API_TOKEN)
MAINNET_RPC_URL		?= https://eth-mainnet.g.alchemy.com/v2/$(ALCHEMY_API_TOKEN)

# Anvil defaults
ANVIL_PORT		?= 8545
ANVIL_BLOCK_TIME	?= 0
FORK_BLOCK		?=

# Forge options.  --use 0.8.28 sidesteps a solc 0.8.31 IR codegen bug
# ("Modifiers not implemented yet"); the v2/v3 builds use their own
# pragmas (=0.5.16, =0.7.6) so we skip them here and they pick up via
# the FOUNDRY_PROFILE=v3 path / their own solc.
FORGE_OPTS		?= --optimize --optimizer-runs 200 --use 0.8.28 \
			   --skip 'src/uniswap_v2_build/**' \
			   --skip 'src/uniswap_v3_build/**'

# Fork block pinning (deterministic tests): set FORK_BLOCK=12345 to pin
ifdef FORK_BLOCK
  ANVIL_FORK_OPTS	= --fork-block-number $(FORK_BLOCK)
else
  ANVIL_FORK_OPTS	=
endif

# 
# Python alberta_buck venv; requires Nix environment
#
PYTHON			:= python3
PYTHON_V		= $(shell $(PYTHON) -c "import sys; print('-'.join((next(iter(filter(None,sys.executable.split('/')))),sys.platform,sys.implementation.cache_tag)))" 2>/dev/null )

BUCK_PYTHON		= $(CURDIR)
BUCK_VERSION		= $(shell sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$(BUCK_PYTHON)/pyproject.toml" | head -1)
VENV			= "$(BUCK_PYTHON).venv-$(BUCK_VERSION)-$(PYTHON_V)"
VENV_OPTS		=


.PHONY: all build test clean fmt snapshot
.PHONY: fork-sepolia fork-mainnet fork-mainnet-cache anvil stop-anvil
.PHONY: deploy-local deploy-sepolia
.PHONY: snark-g1tie snark-g1tie-clean snark-g1tie-regen snark-test-regression snark-update
.PHONY: install update
.PHONY: test-python venv-activate
.PHONY: golden-receipts
.PHONY: snark snark-setup snark-fixtures snark-ptau snark-clean snark-a2 snark-a2-setup snark-a2-fixtures
.PHONY: vectors plots images
.PHONY: vector-lifecycle vector-equilibrium vector-arb
.PHONY: plot-lifecycle plot-equilibrium plot-arb
.PHONY: sim sim-build sim-run sim-test sim-plot
.PHONY: sim-rebalancing sim-run-rebalancing sim-plot-rebalancing
.PHONY: prices-routing plot-routing


# ── Build ────────────────────────────────────────────────────────────

all:			build test

build:
	forge build $(FORGE_OPTS)

# Run nix-emacs to start an emacs inside the Nix-supplied environment
emacs:
	emacs -nw

test:
	forge test $(FORGE_OPTS) -vvv
unit-%:
	forge test $(FORGE_OPTS) --match-test $* -vvv
path-%:
	forge test $(FORGE_OPTS) --match-path $* -vvv
match-%:
	forge test $(FORGE_OPTS) --match-contract $* -vvv

# Run tests against a forked network (slow first run, cached after)
test-fork-sepolia:
	forge test $(FORGE_OPTS) -vvv --fork-url $(SEPOLIA_RPC_URL) $(ANVIL_FORK_OPTS)

test-fork-mainnet:
	forge test $(FORGE_OPTS) -vvv --fork-url $(MAINNET_RPC_URL) $(ANVIL_FORK_OPTS)

# Gas snapshots
snapshot:
	forge snapshot $(FORGE_OPTS)

# Formatting and linting
fmt:
	forge fmt

fmt-check:
	forge fmt --check


# ── Local Anvil Node ─────────────────────────────────────────────────

# Plain local chain (no fork)
anvil:
	anvil --port $(ANVIL_PORT) --block-time $(ANVIL_BLOCK_TIME)

# Fork Sepolia locally
fork-sepolia:
	anvil --port $(ANVIL_PORT) --fork-url $(SEPOLIA_RPC_URL) $(ANVIL_FORK_OPTS)

# Fork mainnet locally
fork-mainnet:
	anvil --port $(ANVIL_PORT) --fork-url $(MAINNET_RPC_URL) $(ANVIL_FORK_OPTS)

# Fork mainnet with persistent disk cache (for Python oracle history tests).
# First run fetches from remote; subsequent runs serve from cache.
ANVIL_CACHE		?= .anvil-cache
fork-mainnet-cache:
	anvil --port $(ANVIL_PORT) --fork-url $(MAINNET_RPC_URL) --cache-path $(ANVIL_CACHE) $(ANVIL_FORK_OPTS)

stop-anvil:
	-pkill -f "anvil --port $(ANVIL_PORT)" 2>/dev/null


# ── Deployment ───────────────────────────────────────────────────────

deploy-local:
	forge script script/Deploy.s.sol --broadcast --rpc-url http://localhost:$(ANVIL_PORT) -vvv

deploy-sepolia:
	forge script script/Deploy.s.sol --broadcast --rpc-url $(SEPOLIA_RPC_URL) --verify -vvv


# ── Python Tests ────────────────────────────────────────────────────

test-python:
	python -m pytest alberta_buck/test/ -v -s


# ── Worked-example vectors and plots ─────────────────────────────────
#
# `images` regenerates every artifact referenced by
# alberta-buck-ethereum-example.org from scratch:
#
#   1. Runs the three Forge tests that emit JSON snapshot vectors under
#      test/vectors/  (lifecycle, equilibrium, arb scenario).
#   2. Runs the matching Python plot scripts to produce PNGs under
#      images/.
#
# Individual vector-* and plot-* targets are also exposed for partial
# regeneration during iteration on a single example.

VECTORS_DIR	= test/vectors
IMAGES_DIR	= images

vector-lifecycle:
	forge test $(FORGE_OPTS) --match-test test_lifecycle -vv

vector-equilibrium:
	forge test $(FORGE_OPTS) --match-contract BuckEquilibriumScenarioTest -vv

vector-arb:
	forge test $(FORGE_OPTS) --match-contract BuckKArbScenarioTest -vv

vectors:		vector-lifecycle vector-equilibrium vector-arb

plot-lifecycle:
	python -m pytest alberta_buck/test/test_lifecycle_plot.py -v -s

plot-equilibrium:
	python -m pytest alberta_buck/test/test_equilibrium_plot.py -v -s

plot-arb:
	python -m pytest alberta_buck/test/test_arb_plot.py -v -s

plots:			plot-lifecycle plot-equilibrium plot-arb

# One-shot: regenerate vectors then plots in the right order.
images:			vectors plots

# ── AB-RCPT/1 receipt golden-text renders ────────────────────────────
#
# Regenerates test/vectors/receipt-*.golden.txt from the current renderer
# and the canonical identity.json vectors.  Run whenever the receipt layout
# changes intentionally; the golden-file tests in test_render.py will fail
# until these are re-generated.

# Regenerates test/vectors/receipt-*.golden.txt from the current renderer
# and the canonical identity.json vectors.  Run whenever the receipt layout
# changes intentionally; the golden-file tests in test_render.py will fail
# until these are re-generated.
#
# Prerequisite: identity.json must be up-to-date (emit-vectors runs first).
golden-receipts:  # requires nix-
	python -m alberta_buck.wallet.cli emit-vectors
	python -m alberta_buck.wallet.cli render-golden


# ── SNARK circuits + trusted setup ───────────────────────────────────
#
# The BUCK Notes mint/spend circuits (circuits/*.circom) compile to per-N
# Groth16 verifiers (src/MintBatchN*Groth16Verifier.sol, dispatched on-chain by
# cms.length).  A Groth16 setup has two phases:
#
#   1. Powers of Tau (build/snark/ptau/pot*.ptau) -- UNIVERSAL and circuit-
#      INDEPENDENT, and the slow part (the pot15..pot20 set is multi-GB and
#      takes ~hours).  It depends only on the FFT domain size, so a circuit edit
#      that does not cross a power-of-two boundary REUSES it untouched.
#   2. Per-circuit zkey + Solidity verifier -- embeds the R1CS / verification
#      key; regenerated on every circuit change (~minutes, reusing the ptau).
#
# So after editing a circuit you normally run `make snark` (phase 2 + the test
# fixtures); `make snark-ptau` (phase 1) is needed only to (re)build the ptau,
# or when a new larger pin crosses into a higher power of two.
#
#   make snark            # phase-2 verifiers + fixtures (usual circuit-change path)
#   make snark-setup      # phase-2 only: per-N zkeys + src/MintBatchN*Verifier.sol
#   make snark-fixtures   # regenerate build/snark/.../fixtures/*.json for the tests
#   make snark-ptau       # phase-1: rebuild the dev Powers of Tau from scratch (~hours)
#   make snark-clean      # drop per-N build dirs (forces a clean phase-2 rebuild)
#   make snark-a2         # private-issuer A2 mint family: per-N MintBatchA2N*Verifier
#                         #   + fixtures (reuses the mint_batch ptau; ~minutes)
#
# Override the pinned batch sizes (each gets its own circuit + verifier):
#   make snark SNARK_PINS="1 2 4 8 16"
#
# !! DEV ENTROPY !!  scripts/snark/setup.sh contributes FIXED dev-only entropy
# ("alberta-buck-dev-*"), so every artifact here is a REPRODUCIBLE DEV setup --
# green in tests, but NOT a secure production setup (the toxic waste is known).
# Generating production assets requires a real multi-party ceremony; see
# README.org "SNARK Circuits and Trusted Setup".
SNARK_PINS ?= 1 2 4 8 16 32
SNARK_DIRS  = $(addprefix build/snark/mint_batch_n,$(SNARK_PINS))
SNARK_A2_DIRS = $(addprefix build/snark/mint_batch_a2_n,$(SNARK_PINS))
# snarkjs lives in node_modules/.bin; prepend it so setup.sh finds it under nix.
SNARK_PATH  = PATH="$(CURDIR)/node_modules/.bin:$$PATH"

snark:		snark-setup snark-fixtures
	@echo "snark: verifiers + fixtures regenerated -- run 'make nix-test' to check on-chain parity"

snark-setup:  # requires nix-
	rm -rf $(SNARK_DIRS)
	$(SNARK_PATH) MINT_BATCH_PINS="$(SNARK_PINS)" bash scripts/snark/setup.sh

snark-fixtures:
	$(SNARK_PATH) bash scripts/snark/gen_mint_fixtures.sh

# Full from-scratch regen (phase 1 + phase 2): removes the dev ptau and every
# circuit build dir so setup.sh rebuilds the Powers of Tau and all verifiers.
# Hours, dev entropy only.
snark-ptau:
	rm -rf build/snark/ptau build/snark/mint build/snark/spend $(SNARK_DIRS)
	$(SNARK_PATH) MINT_BATCH_PINS="$(SNARK_PINS)" bash scripts/snark/setup.sh

snark-clean:
	rm -rf $(SNARK_DIRS) $(SNARK_A2_DIRS)

# Private-issuer A2 mint family (circuits/mint_batch_a2.circom).  Reuses the
# mint_batch ptau (DO_LEGACY=0 DO_MINT_BATCH=0), so this only runs phase-2 for
# the A2 per-N circuits + their fixtures -- the existing public/bearer verifiers
# stay byte-for-byte as deployed.
snark-a2:	snark-a2-setup snark-a2-fixtures
	@echo "snark-a2: A2 verifiers + fixtures regenerated -- run 'make nix-test' to check parity"

snark-a2-setup:
	rm -rf $(SNARK_A2_DIRS)
	$(SNARK_PATH) DO_LEGACY=0 DO_MINT_BATCH=0 MINT_BATCH_A2_PINS="$(SNARK_PINS)" bash scripts/snark/setup.sh

snark-a2-fixtures:
	$(SNARK_PATH) bash scripts/snark/gen_mint_fixtures_a2.sh

# G1-tie circuit (circuits/identity_membership_g1tie.circom).
#   make snark-g1tie       # FULL atomic rebuild (always cleans first)
#   make snark-g1tie-clean  # drop build dir
#
# IMPORTANT: snarkjs groth16 setup is non-deterministic (delta varies per run).
# The zkey, proof, verifier, and vectors are a MATCHED SET from a single run.
# Always use `make snark-g1tie` — never run individual steps manually.
# See alberta-buck-verifier-bug.org.
snark-g1tie:
	rm -rf build/snark/g1tie
	$(SNARK_PATH) bash scripts/snark/setup_g1tie.sh

snark-g1tie-regen: snark-g1tie

# Regression test: regenerates verifier artifacts atomically and tests both
# freshly-generated AND pre-existing (known-working) verifiers on forge.
# Designed to isolate ARM vs x86_64 WASM execution differences.
# See alberta-buck-verifier-bug.org.
snark-test-regression:
	$(SNARK_PATH) bash scripts/snark/test_verifier_regression.sh

snark-g1tie-clean:
	rm -rf build/snark/g1tie

# rapidsnark: prebuilt Groth16 prover/verifier binaries (iden3).  Used by
# setup_note_binding.sh for fast proving of the 2.4M-constraint circuit
# (snarkjs is the fallback, but takes many minutes per proof).
RAPIDSNARK_ZIP	= lib/rapidsnark-macOS-arm64-v0.0.8.zip
RAPIDSNARK_BIN	= lib/rapidsnark-macOS-arm64-v0.0.8/bin

$(RAPIDSNARK_ZIP):
	wget -O $@ https://github.com/iden3/rapidsnark/releases/download/v0.0.8/rapidsnark-macOS-arm64-v0.0.8.zip

$(RAPIDSNARK_BIN)/prover:	$(RAPIDSNARK_ZIP)
	cd lib && unzip -o $(notdir $(RAPIDSNARK_ZIP))
	touch $@

.PHONY: rapidsnark
rapidsnark:	$(RAPIDSNARK_BIN)/prover

# Note-binding circuit (circuits/note_binding.circom).
#   make snark-note-binding       # FULL atomic rebuild (always cleans first)
#   make snark-note-binding-clean  # drop build dir
#
# Witness generation uses the circom C++ calculator (--no_asm); the WASM
# calculator cannot handle the ~5.9M-wire circuit.  The C++ build REQUIRES
# -fno-strict-aliasing (uint64_t* vs mp_limb_t* aliasing UB in the generic
# fr.cpp silently corrupts field comparisons under gcc -O3) and a 64 MB
# stack (the G-powers table expansion lives in ~5.4 MB template stack
# frames); both are handled inside setup_note_binding.sh.  Groth16 setup
# needs pot22+ (bootstrapped with dev entropy if absent; see
# !! DEV ENTROPY !! above).
snark-note-binding:	rapidsnark
	rm -rf build/snark/note_binding
	$(SNARK_PATH) bash scripts/snark/setup_note_binding.sh

snark-note-binding-clean:
	rm -rf build/snark/note_binding

# End-to-end Notes fixtures: one mutually-consistent world per flavor (A1,
# A2, B1) with REAL proofs at every gate, consumed by test/NotesE2E.t.sol.
# Requires the mint/spend/g1tie/note-binding setups to exist (see the
# prerequisites comment in scripts/snark/gen_e2e_fixtures.sh).
snark-e2e-fixtures:
	rm -rf build/snark/e2e
	$(SNARK_PATH) bash scripts/snark/gen_e2e_fixtures.sh

snark-e2e-clean:
	rm -rf build/snark/e2e test/vectors/e2e

# BN254 G-generator stride-8 powers table for note_binding.circom.
# The circom-lib EC library lacks a precomputed power table for BN254's
# generator G=(1,2); without it the optimised scalar multiplication silently
# produces garbage.  This target regenerates circuits/ec/powers/bn254_g_pows.circom
# (8214 lines, 2.7 MB) from the Python wallet's EC primitives.
snark-g-pows:
	$(SNARK_PATH) python3 scripts/snark/gen_bn254_g_pows.py > circuits/ec/powers/bn254_g_pows.circom
	@echo "Generated circuits/ec/powers/bn254_g_pows.circom"

# Update npm dependencies (snarkjs, circomlib, etc.)
#   make snark-update       # npm install --save snarkjs@latest
snark-update:
	npm install snarkjs@latest --no-audit --no-fund --loglevel=error
	@echo "snarkjs: $$(node_modules/.bin/snarkjs --version 2>/dev/null)"


# ── Sim inputs: price CSVs + Universal Router artifact ────────────────
#
# Shared inputs for the web3-driven simulation (see "Externally-driven
# sim" below): generate the commodity price CSVs and build the Universal
# Router artifact.  The UR lives in a sub-project with its own
# foundry.toml (solc 0.8.26, via_ir), so we build it separately and stage
# the artifact under alberta_buck/sim/artifacts/.
#
#   make prices-routing       # (re)generate the commodity price CSVs
#   make plot-routing         # render images/routing-sim.png from the sim JSON

SIM_PRICES_DIR  = alberta_buck/sim/prices
SIM_PLOT_SCRIPT = alberta_buck/sim/plot_routing.py
SIM_GEN_PRICES  = alberta_buck/sim/gen_prices.py
SIM_ARTIFACTS   = alberta_buck/sim/artifacts

ROUTING_PRICES	= $(SIM_PRICES_DIR)/paxg.csv $(SIM_PRICES_DIR)/cbbtc.csv $(SIM_PRICES_DIR)/aoil.csv
ROUTING_ARTIFACT = $(SIM_ARTIFACTS)/UniversalRouter.json
ROUTING_VECTOR	= test/vectors/routing-sim.json
ROUTING_IMAGE	= images/routing-sim.png

prices-routing:	$(ROUTING_PRICES)

# Generate the commodity price CSVs in alberta_buck/sim/prices/.
$(ROUTING_PRICES): $(SIM_GEN_PRICES)
	python3 $(SIM_GEN_PRICES)

$(ROUTING_ARTIFACT):
	( cd lib/universal-router && FORK_URL=http://localhost forge build --skip test --skip script )
	mkdir -p $(SIM_ARTIFACTS)
	cp lib/universal-router/out/UniversalRouter.sol/UniversalRouter.json $@

plot-routing:	$(ROUTING_VECTOR)
	python -m pytest $(SIM_PLOT_SCRIPT) -v -s

$(ROUTING_IMAGE): $(ROUTING_VECTOR)
	python -m pytest $(SIM_PLOT_SCRIPT) -v -s



# ── Externally-driven sim (anvil + web3.py) ──────────────────────────
#
# The faithful org-doc architecture: a Python driver owns the timeline
# and the agents; anvil hosts the real BuckKControllerDirect / Buck /
# BuckBasket / IdentityRegistry stack + real Uniswap V3 + Universal
# Router.  EOA agents get REAL cryptographic IdentityRegistry identities.
#
#   make sim                # full pipeline: build -> run -> plot
#   make sim-build          # emit SimLP + stack artifacts (+ UR artifact)
#   make sim-run            # run the routing scenario (SIM_DAYS=120)
#   make sim-test           # the pytest smoke wrapper
#   make sim-plot           # render images/routing-sim.png from the JSON
#
# Override horizon:  make sim-run SIM_DAYS=365 SIM_TICKS=4

SIM_DAYS	?= 365
SIM_TICKS	?= 4
SIM_PKG		= alberta_buck.sim
SIM_TEST	= alberta_buck/test/test_routing_sim_web3.py

# Two-step Solidity build:
#  (1) v3 profile: compile 0.7.6 Uniswap V3 core contracts without via_ir.
#  (2) default profile: compile everything else with via_ir enabled
#      (required for BuckBasket's deep call stack).  Skips the 0.7.6
#      trigger to avoid the IR-incompatibility error.
# Both profiles share the same ``out/`` directory.
sim-build:	$(ROUTING_ARTIFACT) $(ROUTING_PRICES) v2-patch-init-code-hash
	FOUNDRY_PROFILE=v3 forge build --skip test --skip script
	forge build --skip test --skip script --skip 'src/uniswap_v3_build/*'

# ── Uniswap V2 init-code-hash patch ──────────────────────────────────────
#
# UniswapV2Library.pairFor hardcodes a CREATE2 init-code-hash constant
# (lib/v2-periphery/contracts/libraries/UniswapV2Library.sol).  The
# upstream value is for the mainnet-deployed UniswapV2Pair bytecode; when
# we compile UniswapV2Pair locally (0.5.16, default optimizer) the
# bytecode -- and therefore its init-code-hash -- differs, so
# UniswapV2Router02 computes pair addresses the local factory did not
# deploy and every router call reverts with "call to non-contract
# address".
#
# Fix: after the local UniswapV2Pair artifact exists, compute its
# init-code-hash with `cast keccak` and patch UniswapV2Library.sol in
# place.  Subsequent forge builds recompile Router02 against the
# corrected library so router.pairFor() == factory.getPair().
#
# `lib/` is gitignored (forge install --no-git), so this target is also
# the source of truth for re-applying the patch on a fresh dependency
# install.  Run `make v2-patch-init-code-hash` (or any `sim-build`
# derivative) after `forge install` to re-apply.
.PHONY: v2-patch-init-code-hash
v2-patch-init-code-hash:
	@# Phase 1: ensure UniswapV2Pair artifact exists so we can hash it.
	@test -f out/UniswapV2Pair.sol/UniswapV2Pair.json || \
		forge build --skip test --skip script --skip 'src/uniswap_v3_build/*' >/dev/null
	@HASH=$$(cast keccak $$(jq -r '.bytecode.object' out/UniswapV2Pair.sol/UniswapV2Pair.json) | sed 's/^0x//'); \
		LIB=lib/v2-periphery/contracts/libraries/UniswapV2Library.sol; \
		CURRENT=$$(grep -oE "hex'[0-9a-f]*' // init code hash" $$LIB | sed -E "s/hex'([0-9a-f]*)'.*/\1/"); \
		if [ "$$CURRENT" = "$$HASH" ]; then \
			echo "v2-patch: UniswapV2Library hash already correct ($$HASH)"; \
		else \
			sed -i.bak "s/hex'[0-9a-f]*' \/\/ init code hash/hex'$$HASH' \/\/ init code hash/" $$LIB; \
			echo "v2-patch: patched UniswapV2Library init-code-hash $$CURRENT -> $$HASH"; \
		fi

sim-run:	sim-build
	python -m $(SIM_PKG) --scenario routing --days $(SIM_DAYS) --ticks-per-day $(SIM_TICKS)

sim-test:	sim-build
	python -m pytest $(SIM_TEST) -v -s

sim-plot:	$(ROUTING_VECTOR)
	python -m pytest $(SIM_PLOT_SCRIPT) -v -s

sim:		sim-run sim-plot


# ── Rebalancing simulation (Phase 1: staggered direct-mint agents) ──────
#
# DirectMintAgents enter on a staggered cadence (every ~30 days), each
# depositing into the most-underweight TOKEN/BUCK pool and holding for
# months.  The entry/exit flow naturally rebalances pools toward target
# weights.  BuckBasket has been fixed so equal weightBp yields equal
# target weights (0 => default 1/N share).
#
#   make sim-rebalancing         # build -> run -> plot (365 days)
#   make sim-run-rebalancing     # run the rebalancing scenario
#   make sim-plot-rebalancing    # render images/rebalancing-sim.png

REBALANCING_VECTOR   = test/vectors/rebalancing-sim.json
SIM_REB_PLOT         = alberta_buck/sim/plot_rebalancing.py

sim-run-rebalancing:	sim-build
	python -m $(SIM_PKG) --scenario rebalancing --days $(SIM_DAYS) --ticks-per-day $(SIM_TICKS)

sim-plot-rebalancing:	$(REBALANCING_VECTOR)
	python -m pytest $(SIM_REB_PLOT) -v -s

sim-rebalancing:	sim-run-rebalancing sim-plot-rebalancing


# ── Dependencies ─────────────────────────────────────────────────────

install:
	forge install OpenZeppelin/openzeppelin-contracts --no-git
	forge install smartcontractkit/chainlink-brownie-contracts --no-git
	forge install Uniswap/v3-core --no-git
	forge install Uniswap/v3-periphery --no-git
	forge install Uniswap/universal-router --no-git
	forge install foundry-rs/forge-std --no-git

update:
	forge update


# ── Cleanup ──────────────────────────────────────────────────────────

clean:
	forge clean
	rm -rf cache out broadcast


#
# venv:		Create a Virtual Env containing the installed repo
#
.PHONY: venv
venv:			"$(VENV)"
	@echo; echo "*** Activating $< VirtualEnv for Interactive $(SHELL)"
	@bash --init-file "$</bin/activate" -i

venv-%:			"$(VENV)"
	@echo; echo "*** Running in $< VirtualEnv: make $*"
	@bash --init-file "$</bin/activate" -ic "make $*"

"$(VENV)":
	@[[ "$(PYTHON_V)" =~ "^venv" ]] && ( echo -e "\n\n!!! $@ Cannot start a venv within a venv"; false ) || true
	@echo; echo "*** Building $@ VirtualEnv..."
	@rm -rf $@ && $(PYTHON) -m venv $(VENV_OPTS) "$@" && sed -i -e '1s:^:. $$HOME/.bashrc\n:' "$@/bin/activate" \
	    && source $@/bin/activate \
	    && python -m pip install --no-user --upgrade "$(BUCK_PYTHON)[tests,dev]"

venv-activate:
	pip install -e ".[tests]"



#
# nix-...:
#
# Use a Nix flake environment to execute the make target, eg.
#
#     nix-venv-activate
#
nix-%:
	@if [ -n "$(TARGET)" ]; then \
		nix develop .#$(TARGET) $(NIX_OPTS) --command make $*; \
	else \
		nix develop $(NIX_OPTS) --command make $*; \
	fi

#
# Target to allow the printing of 'make' variables, eg:
#
#     make print-PY3
#
print-%:
	@echo $* = "'$($*)'"
	@echo $*\'s origin is $(origin $*)

FORCE:
