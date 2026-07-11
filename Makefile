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


.PHONY: all build test clean fmt snapshot build-uniswap-artifacts
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
.PHONY: sim sim-build sim-run sim-test sim-plot sim-tui
.PHONY: test-director test-director-regimes sim-run-director sim-director
.PHONY: sim-run-policy sim-run-policy-sweep sim-plot-policy sim-policy sim-policy-sweep
.PHONY: sim-rebalancing sim-run-rebalancing sim-plot-rebalancing
.PHONY: sim-run-flow sim-plot-flow sim-flow
.PHONY: prices-routing plot-routing


# ── Build ────────────────────────────────────────────────────────────

all:			build test

build:
	forge build $(FORGE_OPTS)

# Run nix-emacs to start an emacs inside the Nix-supplied environment
emacs:
	emacs -nw

test: build-uniswap-artifacts
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

# ── BasketRebalanceDirector tests ────────────────────────────────────
#
# The director's Forge suite reads DIR_WINDOW / DIR_RHO1E9 from the
# environment, so the same assertions can be exercised across a window x
# rho parameter matrix.  The regimes target runs the whole matrix in
# parallel (one forge process per regime; `build` first so they share a
# warm compile cache).
#
#   make nix-test-director           # unit + fuzz suite, default regime
#   make nix-test-director-regimes   # parallel window x rho matrix
#   make nix-sim-director            # 30-day Anvil smoke sim (keeper agent)

DIRECTOR_WINDOWS	?= 6 8 16
DIRECTOR_RHOS		?= 1500000000 3000000000 6000000000

test-director:	build
	forge test $(FORGE_OPTS) --match-contract BasketRebalanceDirector -vv

test-director-regimes:	build
	@rc=0; pids=""; \
	for w in $(DIRECTOR_WINDOWS); do \
	  for r in $(DIRECTOR_RHOS); do \
	    log="out/director-w$$w-r$$r.log"; \
	    ( DIR_WINDOW=$$w DIR_RHO1E9=$$r forge test $(FORGE_OPTS) \
	        --match-contract BasketRebalanceDirector > "$$log" 2>&1 ) & \
	    pids="$$pids $$!=w$$w-r$$r"; \
	  done; \
	done; \
	for pr in $$pids; do \
	  p=$${pr%%=*}; tag=$${pr##*=}; \
	  if wait "$$p"; then echo "director regime $$tag  PASS"; \
	  else echo "director regime $$tag  FAIL  (see out/director-$$tag.log)"; rc=1; fi; \
	done; exit $$rc

# Formatting and linting
fmt:
	forge fmt

fmt-check:
	forge fmt --check

# Build the Uniswap V2/V3 artifacts (via their compile-trigger stubs under
# src/uniswap_v*_build and the v3 foundry profile) into the shared out/
# directory.  Tests that use vm.deployCode("out/Uniswap*.json") (e.g.
# BuckBasket, BuckLifecycle, UniswapV2Integration, BuckK*V3*, equilibrium/arb
# scenarios) require these.  Also ensures the critical V2 init-code-hash patch
# has been applied so UniswapV2Router02 computes the same pair addresses as the
# locally-built V2Factory.
build-uniswap-artifacts: v2-patch-init-code-hash
	FOUNDRY_VIA_IR=false FOUNDRY_PROFILE=v3 forge build --skip test --skip script
	forge build --skip test --skip script --skip 'src/uniswap_v3_build/*'

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


# ── Documentation rendering ─────────────────────────────────────────
#
# The org-mode masters live in the repo root and export into images/
# via each document's #+EXPORT_FILE_NAME.  Render one with batch emacs:
#
#   make nix-doc-alberta-buck-notes   # -> images/alberta-buck-notes.{txt,pdf}
#   make doc-alberta-buck-proofs      # (inside an emacs+LaTeX environment)
#
# Notes:
#   - org-ascii-charset utf-8 matches the committed .txt renders (the
#     batch default is plain ascii, which reflows every heading rule).
#   - emacs >= 30 refuses remote resources in batch mode; the logo host
#     must be allowed explicitly (org-safe-remote-resources).
#   - "PDF file produced with errors" is the usual LaTeX cross-reference
#     noise; check the page count if in doubt.

DOC_RENDER_EVAL	= (progn							\
		    (require (quote ox-ascii)) (require (quote ox-latex))	\
		    (setq org-ascii-charset (quote utf-8))			\
		    (setq org-safe-remote-resources				\
			  (list "\\`https://perry\\.kundert\\.ca/"))		\
		    (org-ascii-export-to-ascii)					\
		    (org-latex-export-to-pdf))

doc-%:		%.org
	emacs --batch $< --eval '$(DOC_RENDER_EVAL)'

# alberta-buck-notes-flow.org, alberta-buck-paper.org and
# alberta-buck-receipt.org are EXECUTABLE: their python blocks form one
# seeded org-babel session per document, whose RESULTS drawers are
# regenerated by re-running every block top to bottom -- which requires
# an emacs whose python resolves inside a venv with the alberta_buck
# package installed.  The venv-% wrapper provides that:
#
#   make nix-venv-doc-flow     # execute the transcript + render .txt/.pdf
#   make nix-venv-doc-paper
#   make nix-venv-doc-receipt  # spawns anvil; needs forge artifacts (nix-build)
doc-identity-example:
	emacs --batch -l scripts/render-exec-doc.el alberta-buck-identity-example.org

doc-flow:
	emacs --batch -l scripts/render-exec-doc.el alberta-buck-notes-flow.org

doc-paper:
	emacs --batch -l scripts/render-exec-doc.el alberta-buck-paper.org

doc-receipt:
	emacs --batch -l scripts/render-exec-doc.el alberta-buck-receipt.org


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
# Regenerates alberta_buck/test/vectors/receipt-*.golden.txt (all eight
# kinds: the five recipient-side receipts plus the three issuer-side Note
# receipts) from the current renderer and the canonical identity.json
# vectors.  The goldens are consumed only by the Python tests, so they live
# in the Python tree; test/vectors/ holds the artifacts the forge tests
# read.  Run whenever the receipt layout changes intentionally; the
# golden-file tests in test_render.py will fail until these are
# re-generated.
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
# See doc/historical/alberta-buck-verifier-bug.org.
snark-g1tie:
	rm -rf build/snark/g1tie
	$(SNARK_PATH) bash scripts/snark/setup_g1tie.sh

snark-g1tie-regen: snark-g1tie

# Regression test: regenerates verifier artifacts atomically and tests both
# freshly-generated AND pre-existing (known-working) verifiers on forge.
# Designed to isolate ARM vs x86_64 WASM execution differences.
# See doc/historical/alberta-buck-verifier-bug.org.
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

# A1-layout note-binding circuit (circuits/note_binding_a1.circom): the A1
# sibling of snark-note-binding (same toolchain requirements; ~2.9M
# non-linear constraints, five ScalarMulG + one ScalarMulH).
snark-note-binding-a1:	rapidsnark
	rm -rf build/snark/note_binding_a1
	$(SNARK_PATH) bash scripts/snark/setup_note_binding_a1.sh

snark-note-binding-a1-clean:
	rm -rf build/snark/note_binding_a1

# End-to-end Notes fixtures: one mutually-consistent world per flavor (A1,
# A2, B1) with REAL proofs at every gate, consumed by test/NotesE2E.t.sol.
# Requires the mint/spend/g1tie/note-binding setups to exist (see the
# prerequisites comment in scripts/snark/gen_e2e_fixtures.sh).
snark-e2e-fixtures:
	rm -rf build/snark/e2e
	$(SNARK_PATH) bash scripts/snark/gen_e2e_fixtures.sh

snark-e2e-clean:
	rm -rf build/snark/e2e alberta_buck/test/vectors/e2e

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
# Run these through Nix, e.g. `make nix-sim-run-rebalancing`, or from inside
# `nix develop`; the system shell may not have Foundry, Anvil, Web3, or the
# project Python dependencies.
#
#   make nix-sim                # full pipeline: build -> run -> plot
#   make nix-sim-build          # emit SimLP + stack artifacts (+ UR artifact)
#   make nix-sim-run            # run the routing scenario (SIM_DAYS=120)
#   make nix-sim-test           # the pytest smoke wrapper
#   make nix-sim-plot           # render images/routing-sim.png from the JSON
#
# Override horizon:  make nix-sim-run SIM_DAYS=365 SIM_TICKS=4

SIM_DAYS	?= 365
SIM_TICKS	?= 4
SIM_BASKET	?= prorata       # prorata (BuckBasketProRata, default) | legacy (BuckBasket)
SIM_SCENARIO	?= rebalancing   # routing | rebalancing (see scenario.py)
SIM_PKG		= alberta_buck.sim
SIM_TEST	= alberta_buck/test/test_routing_sim_web3.py

# Two-step Solidity build:
#  (1) v3 profile: compile 0.7.6 Uniswap V3 core contracts without via_ir.
#  (2) default profile: compile everything else with via_ir enabled
#      (required for BuckBasket's deep call stack).  Skips the 0.7.6
#      trigger to avoid the IR-incompatibility error.
# Both profiles share the same ``out/`` directory.
sim-build:	$(ROUTING_ARTIFACT) $(ROUTING_PRICES) v2-patch-init-code-hash
	FOUNDRY_VIA_IR=false FOUNDRY_PROFILE=v3 forge build --skip test --skip script
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
	python -m $(SIM_PKG) --scenario routing --days $(SIM_DAYS) --ticks-per-day $(SIM_TICKS) --basket $(SIM_BASKET)

sim-test:	sim-build
	python -m pytest $(SIM_TEST) -v -s

sim-plot:	$(ROUTING_VECTOR)
	python -m pytest $(SIM_PLOT_SCRIPT) -v -s

sim:		sim-run sim-plot

# ── Interactive curses inspector (live component / agent viewer) ────────
#
# A navigable TUI over a live sim: deploy + step the timeline yourself and
# inspect every on-chain component and agent (summary -> detail).  This is
# the display-only first layer; parameter adjustment comes later.
#
#   make nix-sim-tui                                       # rebalancing/prorata
#   make nix-sim-tui SIM_SCENARIO=routing SIM_BASKET=legacy   # lighter, fast deploy
#
# In the UI:  arrows/PgUp/PgDn move; Right/Left expand/collapse; [space] step
# a tick, [d] step a day, [r] run/pause, [w]rite a snapshot vector, [q]uit.
sim-tui:	sim-build
	@echo "sim-tui: scenario: $(SIM_SCENARIO)"
	@echo "sim-tui: basket:   $(SIM_BASKET)"
	python -m $(SIM_PKG).tui --scenario $(SIM_SCENARIO) --basket $(SIM_BASKET) \
		--days $(SIM_DAYS) --ticks-per-day $(SIM_TICKS)

sim-tui-prorata: SIM_BASKET=prorata
sim-tui-prorata: sim-tui


# ── Rebalancing simulation (Phase 1: staggered direct-mint agents) ──────
#
# DirectMintAgents enter/exit stochastically.  Current TOKEN deposits LP into
# the deposited token's own TOKEN/BUCK pool; redemption allocation is the
# basket-side "sell overweight" leg.  BUCK deposits route to the most
# underweight pool, but this scenario's agents do not currently enter with
# BUCK.  BuckBasket has been fixed so equal weightBp yields equal target
# weights (0 => default 1/N share).
#
#   make nix-sim-rebalancing         # build -> run -> plot (365 days)
#   make nix-sim-run-rebalancing     # run the rebalancing scenario
#   make nix-sim-plot-rebalancing    # render images/rebalancing-sim.png

REBALANCING_VECTOR   = test/vectors/rebalancing-sim.json
SIM_REB_PLOT         = alberta_buck/sim/plot_rebalancing.py

sim-run-rebalancing:	sim-build
	python -m $(SIM_PKG) --scenario rebalancing --days $(SIM_DAYS) --ticks-per-day $(SIM_TICKS) --basket $(SIM_BASKET)

sim-plot-rebalancing:	$(REBALANCING_VECTOR)
	python -m pytest $(SIM_REB_PLOT) -v -s

sim-rebalancing:	sim-run-rebalancing sim-plot-rebalancing


# ── Rebalancing A/B: BuckBasketProRata vs the traditional BuckBasket ────
#
# Two independent targets, each writing its own vector + image so the runs
# can be compared side by side.  The hypothesis: BUCK direct-mint agents +
# the rebalancing effect of BuckBasketProRata redemptions yield a smoother
# holder ROI than the traditional basket.
#
#   make nix-sim-rebalancing-prorata        # build -> run (prorata) -> plot
#   make nix-sim-rebalancing-traditional    # build -> run (legacy)  -> plot
#   make nix-sim-run-rebalancing-prorata    # just the run
#
# Horizon override applies as usual:  ... SIM_DAYS=365 SIM_TICKS=4

REBALANCING_VECTOR_PRORATA      = test/vectors/rebalancing-sim-prorata.json
REBALANCING_VECTOR_TRADITIONAL  = test/vectors/rebalancing-sim-traditional.json

.PHONY: sim-run-rebalancing-prorata sim-run-rebalancing-traditional
.PHONY: sim-plot-rebalancing-prorata sim-plot-rebalancing-traditional
.PHONY: sim-rebalancing-prorata sim-rebalancing-traditional

sim-run-rebalancing-prorata:	sim-build
	python -m $(SIM_PKG) --scenario rebalancing --days $(SIM_DAYS) \
		--ticks-per-day $(SIM_TICKS) --basket prorata \
		--out $(REBALANCING_VECTOR_PRORATA)

sim-run-rebalancing-traditional:	sim-build
	python -m $(SIM_PKG) --scenario rebalancing --days $(SIM_DAYS) \
		--ticks-per-day $(SIM_TICKS) --basket legacy \
		--out $(REBALANCING_VECTOR_TRADITIONAL)

sim-plot-rebalancing-prorata:	$(REBALANCING_VECTOR_PRORATA)
	REB_VECTOR=$(REBALANCING_VECTOR_PRORATA) \
		REB_OUT=images/rebalancing-sim-prorata.png \
		python -m pytest $(SIM_REB_PLOT) -v -s

sim-plot-rebalancing-traditional:	$(REBALANCING_VECTOR_TRADITIONAL)
	REB_VECTOR=$(REBALANCING_VECTOR_TRADITIONAL) \
		REB_OUT=images/rebalancing-sim-traditional.png \
		python -m pytest $(SIM_REB_PLOT) -v -s

sim-rebalancing-prorata:	sim-run-rebalancing-prorata sim-plot-rebalancing-prorata
sim-rebalancing-traditional:	sim-run-rebalancing-traditional sim-plot-rebalancing-traditional


# ── Pure price-flow basket simulator (no Anvil) ───────────────────────
#
# Ad-hoc check of investor flow rebalancing against the generated
# PAXG/cbBTC/AOIL price CSVs.
#
#   make nix-sim-flow        # run -> plot
#   make nix-sim-run-flow    # write test/vectors/basket-flow-sim.json
#   make nix-sim-plot-flow   # render images/basket-flow-sim.png

FLOW_VECTOR	= test/vectors/basket-flow-sim.json
FLOW_IMAGE	= images/basket-flow-sim.png
SIM_FLOW_PLOT	= alberta_buck/sim/plot_basket_flow.py

$(FLOW_VECTOR):	$(ROUTING_PRICES) alberta_buck/sim/basket_flow.py
	python -m alberta_buck.sim.basket_flow

sim-run-flow:	$(ROUTING_PRICES)
	python -m alberta_buck.sim.basket_flow

sim-plot-flow:	$(FLOW_VECTOR) $(SIM_FLOW_PLOT)
	python -m alberta_buck.sim.plot_basket_flow

sim-flow:	sim-run-flow
	python -m alberta_buck.sim.plot_basket_flow


# ── Rebalance-policy model (no Anvil) ─────────────────────────────────
#
# Deviation x MA-acceleration rebalancing factor over the hist-*.csv
# constituents plus a synthetic M2-lag driver; compares hold / prop /
# band / factor policies.  --sweep adds the per-constituent MA-window
# coordinate sweep (slower).
#
#   make nix-sim-policy        # run -> plot
#   make nix-sim-run-policy    # write test/vectors/rebalance-policy.json
#   make nix-sim-plot-policy   # render images/rebalance-policy.png

POLICY_VECTOR	= test/vectors/rebalance-policy.json
POLICY_IMAGE	= images/rebalance-policy.png
POLICY_OPTS	?=

$(POLICY_VECTOR):	alberta_buck/sim/rebalance_policy.py
	python -m alberta_buck.sim.rebalance_policy $(POLICY_OPTS)

sim-run-policy:
	python -m alberta_buck.sim.rebalance_policy $(POLICY_OPTS)

sim-run-policy-sweep:
	python -m alberta_buck.sim.rebalance_policy --sweep $(POLICY_OPTS)

sim-plot-policy:	$(POLICY_VECTOR) alberta_buck/sim/plot_rebalance_policy.py
	python -m alberta_buck.sim.plot_rebalance_policy

sim-policy:	sim-run-policy
	python -m alberta_buck.sim.plot_rebalance_policy

sim-policy-sweep:	sim-run-policy-sweep
	python -m alberta_buck.sim.plot_rebalance_policy

# Article figures for alberta-buck-rebalance.org (mechanism, vs-hold,
# vs-prop, frontier) from the same rebalance-policy vector.
sim-plot-article:	$(POLICY_VECTOR)
	python -m alberta_buck.sim.plot_rebalance_article

# 30-day Anvil smoke sim exercising the BasketRebalanceDirector end to end:
# the rebalancing scenario's DirectorKeeperAgent pokes the director's work
# wheel each tick and executes its advisory efforts through the router.
sim-run-director:
	python -m $(SIM_PKG) --scenario rebalancing --days 30 \
		--ticks-per-day $(SIM_TICKS) --basket prorata \
		--out test/vectors/director-smoke.json

sim-director:	sim-build sim-run-director


# ── Historical commodity & labour quote source ───────────────────────
#
# Builds NRGY/BULN/FOOD (from Bank of Canada BCPI sub-indices) and a
# synthesized LABR series as inflation-neutralized real-CAD quotes, then
# fills hourly samples between monthly anchors with seeded Brownian bridges.
# Data is vendored under alberta_buck/sim/quotes/data (self-contained).
#
#   make nix-sim-quotes-plot   # render images/commodity-quotes-sim.png
#   make nix-test-quotes       # run the quote-source property tests

QUOTES_IMAGE	= images/commodity-quotes-sim.png

.PHONY: sim-quotes-plot test-quotes

sim-quotes-plot:
	python -m alberta_buck.sim.quotes.plot_quotes

test-quotes:
	python -m pytest alberta_buck/test/test_quotes.py -v


# ── Historical basket simulation (real macro data) ───────────────────
#
# Runs the full BUCK stack on Anvil against REAL historical prices --
#   PAXG (gold, USD), cbBTC (bitcoin, USD), NRGC (energy, CAD), LABR
#   (labour, CAD) -- initialized to equal weights by value on the start day.
# Window defaults to the last SIM_YEARS years of available data (~2025-09).
#
#   make nix-sim-gen-historical               # write the daily CSVs only
#   make nix-sim-historical                   # build -> run -> plot
#   make nix-sim-run-historical SIM_YEARS=1   # just the run (smaller window)
#   make nix-sim-historical SIM_YEARS=2 HIST_TICKS=1
#
# A 5-year daily run is large (1800+ days x agent population); start with
# SIM_YEARS=1 to smoke-test before committing to the full horizon.

HISTORICAL_VECTOR	= test/vectors/historical-sim.json
HISTORICAL_IMAGE	= images/historical-sim.png
SIM_YEARS		?= 5
HIST_TICKS		?= 1

.PHONY: sim-gen-historical sim-run-historical sim-plot-historical sim-historical

sim-gen-historical:
	python -m alberta_buck.sim.gen_historical --years $(SIM_YEARS)

sim-run-historical:	sim-build
	python -m $(SIM_PKG) --scenario historical --years $(SIM_YEARS) \
		--ticks-per-day $(HIST_TICKS) --basket $(SIM_BASKET) \
		--out $(HISTORICAL_VECTOR)

sim-plot-historical:	$(HISTORICAL_VECTOR)
	REB_VECTOR=$(HISTORICAL_VECTOR) REB_OUT=$(HISTORICAL_IMAGE) \
		python -m pytest $(SIM_REB_PLOT) -v -s

sim-historical:	sim-run-historical sim-plot-historical


# ── Equilibrium basket simulation (BUCK-K feedback loop) ─────────────
#
# Same real macro price feeds as the historical scenario, plus the MONETARY
# feedback the BuckKControllerDirect PID defends: FatCreditBorrower agents
# issue/retire BUCK against K-scaled credit limits, pushing basketValueInBuck
# around 1.0 while the controller trims buckK to hold parity.  A PidKeeper
# advances the PID on the 30-min (ticks_per_day=48) money cadence.
#
#   make nix-sim-equilibrium                 # build -> run (short) -> plot
#   make nix-sim-run-equilibrium             # just the run
#   make nix-sim-plot-equilibrium            # render images/equilibrium-sim.png
#   make nix-sim-equilibrium EQ_DAYS=60 EQ_YEARS=1.5
#
# Start short (EQ_DAYS=30) to smoke-test the loop before a long horizon.

EQUILIBRIUM_VECTOR	= test/vectors/equilibrium-sim.json
EQUILIBRIUM_IMAGE	= images/equilibrium-sim.png
SIM_EQ_PLOT		= alberta_buck/sim/plot_equilibrium.py
EQ_YEARS		?= 1.5
EQ_TICKS		?= 48
EQ_DAYS			?= 20

.PHONY: sim-run-equilibrium sim-plot-equilibrium sim-equilibrium

sim-run-equilibrium:	sim-build
	python -m $(SIM_PKG) --scenario equilibrium --years $(EQ_YEARS) \
		--days $(EQ_DAYS) --ticks-per-day $(EQ_TICKS) --basket $(SIM_BASKET) \
		--out $(EQUILIBRIUM_VECTOR)

sim-plot-equilibrium:	$(EQUILIBRIUM_VECTOR)
	EQ_VECTOR=$(EQUILIBRIUM_VECTOR) EQ_OUT=$(EQUILIBRIUM_IMAGE) \
		python -m pytest $(SIM_EQ_PLOT) -v -s

sim-equilibrium:	sim-run-equilibrium sim-plot-equilibrium

# -- Experiment harness over the equilibrium scenario ------------------
#
# Declarative initial conditions + day-indexed scripted interventions
# (controller retunes, agent knobs, price/uptake shocks, population
# changes) from a TOML file; see alberta_buck/sim/experiments/template.toml.
#
#   make nix-sim-experiment                                    # baseline
#   make nix-sim-experiment EQ_EXPERIMENT=path/to/exp.toml EQ_SETS="--set deploy.k0=0.8"
#   make nix-sim-experiment-shock-price                        # by TOML name
#   make nix-sim-plot-eq-shock-price                           # its 7-pane plot
#   make nix-sim-sweep EQ_EXPERIMENTS="a.toml b.toml" EQ_SEEDS=1,2,3 EQ_JOBS=3
#   make nix-sim-metrics EQ_VECTORS="test/vectors/eq-*.json"

EQ_EXPERIMENT	?= alberta_buck/sim/experiments/baseline-5yr.toml
EQ_EXPERIMENTS	?= $(EQ_EXPERIMENT)
EQ_SETS		?=
EQ_SEEDS	?=
EQ_JOBS		?= 3
EQ_SWEEP_DIR	?= test/vectors/sweep
EQ_VECTORS	?= test/vectors/eq-*.json

.PHONY: sim-experiment sim-sweep sim-metrics

sim-experiment:	sim-build
	python -m $(SIM_PKG) --experiment $(EQ_EXPERIMENT) $(EQ_SETS)

# Run any experiment by TOML basename: make sim-experiment-<name> runs
# alberta_buck/sim/experiments/<name>.toml -> test/vectors/eq-<name>.json.
sim-experiment-%:	sim-build
	python -m $(SIM_PKG) \
		--experiment alberta_buck/sim/experiments/$*.toml $(EQ_SETS)

sim-sweep:	sim-build
	python -m alberta_buck.sim.sweep $(EQ_EXPERIMENTS) \
		$(if $(EQ_SEEDS),--seeds $(EQ_SEEDS)) --jobs $(EQ_JOBS) \
		--outdir $(EQ_SWEEP_DIR) $(EQ_SETS)

sim-metrics:
	python -m alberta_buck.sim.eqmetrics $(EQ_VECTORS)

# -- Named sweeps: the banked campaign incantations --------------------
#
# Each reproduces one campaign from EQUILIBRIUM.md: experiment file(s) x
# seeds run in parallel (each with its own anvil), the eqmetrics table is
# printed, and vectors + summary.json land in test/vectors/sweep-<name>/.
# Override seeds per-run with EQ_SEEDS=..., parallelism with EQ_JOBS=N.
#
#   make nix-sim-sweep-baseline   # baseline-5yr x 5 seeds (parity campaign)
#   make nix-sim-sweep-shocks     # intervention suite, canonical seed each
#   make nix-sim-sweep-savers2x   # saver-demand probe on the failing seeds
#   make nix-sim-sweep-capacity   # capacity+demand probe on the worst seed

EQ_EXP_DIR		 = alberta_buck/sim/experiments
SWEEP_baseline_EXPS	 = $(EQ_EXP_DIR)/baseline-5yr.toml
SWEEP_baseline_SEEDS	 = 41404,1337,2025,7,99
SWEEP_shocks_EXPS	 = $(EQ_EXP_DIR)/retune-mid.toml \
			   $(EQ_EXP_DIR)/shock-price.toml \
			   $(EQ_EXP_DIR)/shock-uptake.toml \
			   $(EQ_EXP_DIR)/shock-demand.toml \
			   $(EQ_EXP_DIR)/population-churn.toml
SWEEP_shocks_SEEDS	 =
SWEEP_savers2x_EXPS	 = $(EQ_EXP_DIR)/baseline-savers2x.toml
SWEEP_savers2x_SEEDS	 = 1337,2025,99
SWEEP_capacity_EXPS	 = $(EQ_EXP_DIR)/baseline-capacity.toml
SWEEP_capacity_SEEDS	 = 99

sim-sweep-%:	sim-build
	$(if $(SWEEP_$*_EXPS),,$(error unknown sweep '$*'; defined: baseline shocks savers2x capacity))
	python -m alberta_buck.sim.sweep $(SWEEP_$*_EXPS) \
		$(if $(or $(EQ_SEEDS),$(SWEEP_$*_SEEDS)),--seeds $(or $(EQ_SEEDS),$(SWEEP_$*_SEEDS))) \
		--jobs $(EQ_JOBS) --outdir test/vectors/sweep-$*

# -- Named plots: 7-pane equilibrium render for any vector -------------
#
# make sim-plot-eq-<name> renders EQ_VECTOR (default test/vectors/
# eq-<name>.json, i.e. what sim-experiment-<name> wrote) to
# images/equilibrium-<name>.png.  The milestone images map to campaign
# vectors explicitly:
#
#   make nix-sim-plot-eq-baseline-5yr       # canonical-seed baseline
#   make nix-sim-plot-eq-population-churn   # worst shock run

PLOT_baseline-5yr_VECTOR	= test/vectors/sweep-baseline/eq-baseline-5yr-s41404.json
PLOT_population-churn_VECTOR	= test/vectors/sweep-shocks/eq-population-churn.json

sim-plot-eq-%:
	EQ_VECTOR=$(or $(EQ_VECTOR),$(PLOT_$*_VECTOR),test/vectors/eq-$*.json) \
	EQ_OUT=images/equilibrium-$*.png \
		python -m pytest $(SIM_EQ_PLOT) -v -s


# ── Core platform (core/: kernel + sessions; alberta-buck-platform.org) ──
#
#   make nix-core-build         # kernel bindings: Python .so + JS wasm pkg
#   make nix-core-test          # all three suites (Python, JS, Rust)
#   make nix-venv-core-test-py  # the Python suite inside the repo venv
#                               # (which installs alberta_buck AND -e
#                               #  core/python; see the venv recipe)
#
# Tests are minimal in Rust (kernel is vector-driven), primary in Python
# and JS.  The golden math vectors (test/vectors/math-vectors.json, from
# make nix-match-MathVectors) are asserted bit-identically by all three;
# the journal fixture (core/vectors/) by Python and JS -- change either
# only with every consuming suite in hand.
#
# Identity kernel vectors: core/vectors/identity-kernel-vectors.json is
# emitted by the pure-Python py_ecc REFERENCE path (the executable spec)
# and replayed nonce-for-nonce by all three suites.  Regenerating it is an
# ABI-break-level event -- do so only with the cargo/pytest/node suites in
# hand:
#
#   make nix-venv-core-identity-vectors

.PHONY: core-test core-test-py core-test-js core-test-rust core-js-deps
.PHONY: core-build core-build-py core-build-wasm core-identity-vectors

# Emit from the py_ecc reference (kernel_vectors.py forces
# BUCK_IDENTITY_BACKEND=py itself; the binding need not be built).
core-identity-vectors:
	python -m alberta_buck.wallet.kernel_vectors core/vectors/identity-kernel-vectors.json

core-js-deps:
	cd core/js && npm ci

# Bundle the forge artifacts the JS platform needs into one importable
# module (core/js/artifacts/bundle.mjs) -- the browser cannot read out/.
core-js-artifacts:
	node core/js/bin/bundle-artifacts.mjs

# The Python kernel bindings: PyO3 cdylibs built with plain cargo (the
# .cargo/config.toml link flags stand in for maturin) and placed inside
# the buck_core package -- import buck_core.buck_math /
# buck_core.buck_identity.
core-build-py:
	cd core/rust && cargo build --release -p buck-math-py -p buck-identity-py
	cp core/rust/target/release/libbuck_math.dylib \
	   core/python/buck_core/buck_math.so
	cp core/rust/target/release/libbuck_identity.dylib \
	   core/python/buck_core/buck_identity.so

# The JS kernel bindings: wasm-pack (npm devDependency of core/js) emits
# nodejs-target packages into core/js/wasm/ (flat: buck_math.* and
# buck_identity.* coexist; the shared package.json is cosmetic until the
# npm packaging phase).  buck_math: BigInt ABI.  buck_identity: 0x-hex
# ABI wrapped by core/js/src/identity.js into the BigInt-native API.
core-build-wasm:
	@test -x core/js/node_modules/.bin/wasm-pack || { echo "wasm-pack missing; run: make nix-core-js-deps"; exit 1; }
	cd core/rust/bindings/js && ../../../js/node_modules/.bin/wasm-pack \
		build --release --target nodejs \
		--out-dir ../../../js/wasm --out-name buck_math
	cd core/rust/bindings/js-identity && ../../../js/node_modules/.bin/wasm-pack \
		build --release --target nodejs \
		--out-dir ../../../js/wasm --out-name buck_identity

# Browser (web-target) builds of BOTH kernels + the demo pages.  Serve
# the demos (ES modules need http, not file://):
#   make nix-core-demo-identity     # Phase 3: in-browser proof ceremony
#   make nix-core-demo-buckworld    # Phase 4: the interactive buckworld
#   python3 -m http.server -d core/js/demo 8000
#   open http://localhost:8000/identity-proofs.html   (or buckworld.html)
core-build-wasm-web:
	@test -x core/js/node_modules/.bin/wasm-pack || { echo "wasm-pack missing; run: make nix-core-js-deps"; exit 1; }
	cd core/rust/bindings/js-identity && ../../../js/node_modules/.bin/wasm-pack \
		build --release --target web \
		--out-dir ../../../js/demo/wasm-web --out-name buck_identity
	cd core/rust/bindings/js && ../../../js/node_modules/.bin/wasm-pack \
		build --release --target web \
		--out-dir ../../../js/demo/wasm-web --out-name buck_math

core-demo-identity:	core-build-wasm-web
	@echo "demo ready: python3 -m http.server -d core/js/demo 8000"
	@echo "       then open http://localhost:8000/identity-proofs.html"

# The interactive buckworld page: bundle the controller + platform +
# viem/tevm into demo/app.js (the wasm binaries stay separate files the
# page fetches from wasm-web/).  Browser shims: the npm `buffer` polyfill
# (string_decoder in tevm's tree), and an empty `fs` (an @tevm/node
# state-persistence path the browser never takes).
core-demo-buckworld:	core-build-wasm-web core-js-artifacts
	cd core/js && npx esbuild demo/src/main.js --bundle --format=esm \
		--platform=browser --outfile=demo/app.js \
		--alias:buffer=buffer \
		--alias:fs=./demo/src/shims/fs-empty.js \
		--log-limit=8
	@echo "demo ready: python3 -m http.server -d core/js/demo 8000"
	@echo "       then open http://localhost:8000/buckworld.html"

# The equilibrium page: the two-agent BUCK-K loop with live charts and
# dynamic add-saver/add-debtor controls (demo/eqapp.js).
core-demo-eqworld:	core-build-wasm-web core-js-artifacts
	cd core/js && npx esbuild demo/src/eqmain.js --bundle --format=esm \
		--platform=browser --outfile=demo/eqapp.js \
		--alias:buffer=buffer \
		--alias:fs=./demo/src/shims/fs-empty.js \
		--log-limit=8
	@echo "demo ready: python3 -m http.server -d core/js/demo 8000"
	@echo "       then open http://localhost:8000/eqworld.html"

core-build:	core-build-py core-build-wasm

core-test-py:
	python -m pytest core/python/tests -q

core-test-js:
	@test -d core/js/node_modules || { echo "core/js deps missing; run: make nix-core-js-deps"; exit 1; }
	cd core/js && node --test

core-test-rust:
	cd core/rust && cargo test --quiet

core-test:	core-test-py core-test-js core-test-rust


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
	    && python -m pip install --no-user --upgrade "$(BUCK_PYTHON)[tests,dev]" \
	    && python -m pip install --no-user -e "$(BUCK_PYTHON)/core/python"

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
