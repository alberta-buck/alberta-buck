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

# Forge options.  The solc pin (0.8.28, avoiding a 0.8.31 IR codegen bug)
# and the uniswap_v*_build skips now live in [profile.default] in
# foundry.toml -- on a command line they applied only where somebody
# remembered them, and the bare builds below are exactly where nobody did.
FORGE_OPTS		?= --optimize --optimizer-runs 200 $(FORGE_SKIP_GENERATED)

# test/RegressionTest.sol imports a Groth16 verifier that the SNARK
# toolchain GENERATES into build/snark/ (gitignored).  foundry resolves the
# whole project graph before it applies any --skip name filter, so when that
# file is absent every `forge build` fails at parse time -- which is exactly
# what a fresh clone and CI see.  Skip that one path when the generated
# verifier is not present, and run it normally when it is.
FORGE_SKIP_GENERATED	= $(if $(wildcard build/snark/regression/RegressVerifier.sol),,--skip 'test/RegressionTest.sol')

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
.PHONY: snark-g1tie snark-g1tie-clean snark-g1tie-regen snark-spend snark-test-regression snark-update
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
	forge test $(FORGE_OPTS) $(FORGE_SKIP_SNARK_TESTS) -vvv

# The SNARK suites read proof fixtures the circom/snarkjs toolchain
# GENERATES into build/snark/ (gitignored, and a trusted setup away).  With
# the fixtures present they run; without them they fail at vm.readFile, so a
# fresh clone or a CI runner that has not built the circuits skips them --
# deliberately and visibly, rather than by pretending the suite is green.
#
#   make snark-...      # generate the fixtures, then these run too
SNARK_TEST_SUITES	= MintVerifierTest|MintVerifierA2Test|NotesA2TieTest|SpendVerifierTest
FORGE_SKIP_SNARK_TESTS	= $(if $(wildcard build/snark/mint_batch_a2_n1/fixtures/basic.json),,\
			     --no-match-contract '$(SNARK_TEST_SUITES)')
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
	forge test $(FORGE_OPTS) --match-contract 'RebalanceDirector' -vv

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
build-uniswap-artifacts: stage-uniswap
	forge build --skip test --skip script $(FORGE_SKIP_GENERATED)

# The third-party artifacts come from Uniswap's own published npm packages,
# pinned exactly in package.json.  We no longer compile them: their pragmas
# are =0.5.16 / =0.6.6 / =0.7.6, which foundry cannot resolve at all on
# arm64 macOS, and the copies that used to sit in out/ were stale leftovers
# from a toolchain that no longer exists -- reproducible on no fresh clone
# and in no CI runner.  See scripts/stage-uniswap.mjs.
.PHONY: stage-uniswap stage-uniswap-check
stage-uniswap:
	node scripts/stage-uniswap.mjs

stage-uniswap-check:
	node scripts/stage-uniswap.mjs --check

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

# alberta-buck-notes-flow.org, alberta-buck-paper.org,
# alberta-buck-receipt.org and alberta-buck-proofs.org are EXECUTABLE: their
# python blocks form one org-babel session per document, whose RESULTS drawers are
# regenerated by re-running every block top to bottom -- which requires
# an emacs whose python resolves inside a venv with the alberta_buck
# package installed.  The venv-% wrapper provides that:
#
#   make nix-venv-doc-flow     # execute the transcript + render .txt/.pdf
#   make nix-venv-doc-paper
#   make nix-venv-doc-receipt  # spawns anvil; needs forge artifacts (nix-build)
#   make nix-venv-doc-proofs   # Part IV's check imports the protocol's own H and leaves
doc-identity-example:
	emacs --batch -l scripts/render-exec-doc.el alberta-buck-identity-example.org

doc-flow:
	emacs --batch -l scripts/render-exec-doc.el alberta-buck-notes-flow.org

doc-paper:
	emacs --batch -l scripts/render-exec-doc.el alberta-buck-paper.org

doc-receipt:
	emacs --batch -l scripts/render-exec-doc.el alberta-buck-receipt.org

doc-proofs:
	emacs --batch -l scripts/render-exec-doc.el alberta-buck-proofs.org


# ── Worked-example vectors and plots ─────────────────────────────────
#
# `images` regenerates the scenario vectors and their plots from scratch:
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

# ── AB-RCPT/2 receipt golden-text renders ────────────────────────────
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


# ── Forge vectors from the Python identity reference ─────────────────
#
# Three vector sets have generators of their own and are read only by Forge:
# the B1 depositor binding, the registry's accumulator vectors, and the insurer
# gate.  A leaf, tag or transcript change needs all three rerun:
#
#   make nix-venv-forge-identity-vectors
.PHONY: forge-identity-vectors
forge-identity-vectors:
	python scripts/gen_b1_binding_vectors.py
	python -m alberta_buck.registry.vectors --output test/vectors/registry/
	python scripts/gen_insurer_gate_vectors.py


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
# RUNBOOK: doc/snark-regeneration.org -- the dependency chain, timings, disk
# budget, and the --b-only trap.  Read it before regenerating anything; the
# artifacts here are a MATCHED SET and regenerating one member invalidates the
# committed proof vectors of the others.  Run `make nix-test` immediately
# after any snark-* target, BEFORE committing.
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
# DESTRUCTIVE.  This is the ONLY target that deletes build/snark/ptau, and that
# directory is the expensive one: the powers of tau run to gigabytes
# (pot20_final.ptau alone is 1.1G) and take hours to regenerate.  Every other
# snark target leaves it alone -- ensure_ptau() reuses an existing
# potN_final.ptau and prints "[ptau] reusing".  If you only want fresh zkeys
# and verifiers, `make snark-setup` is the target you want: it rebuilds the
# circuits against the ptau you already have.
#
# Guarded because losing this directory once already cost a recovery from an
# old checkout.  Set CONFIRM=yes for non-interactive use (CI, make -j).
snark-ptau:
	@if [ "$(CONFIRM)" != "yes" ]; then \
	  echo; echo "  *** snark-ptau DELETES the powers of tau ***"; echo; \
	  if [ -d build/snark/ptau ]; then \
	    echo "  about to remove $$(du -sh build/snark/ptau 2>/dev/null | cut -f1) from build/snark/ptau:"; \
	    ls build/snark/ptau/*_final.ptau 2>/dev/null | sed 's|^|    |'; \
	  else \
	    echo "  (no build/snark/ptau present -- nothing to lose)"; \
	  fi; \
	  echo; echo "  Regenerating takes hours.  For zkeys/verifiers only: make snark-setup"; echo; \
	  printf "  Type 'delete-ptau' to proceed: "; read ans; \
	  if [ "$$ans" != "delete-ptau" ]; then echo "  aborted -- nothing removed"; exit 1; fi; \
	fi
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

# Spend circuit only (circuits/spend.circom).  Isolated compile + setup, then
# copy the matched set (r1cs/wasm/zkey/verifier + re-proved e2e spend vectors).
# Does not rebuild mint or the gate circuits.
snark-spend:
	$(SNARK_PATH) bash scripts/snark/setup_spend.sh

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
# setup_deposit_fold.sh for fast proving of the multi-million-constraint gates
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

# The three deposit gates.  Each is a full Groth16 setup under DEV ENTROPY --
# not a ceremony -- and each bakes the aggregator depth into its r1cs, so a
# depth change means redoing them.  The folds need ~16 GB of node heap and
# rapidsnark for proving; the script handles both.
#
#   make snark-deposit-fold-a1    # the addressed gate, A1 layout (3.31M)
#   make snark-deposit-fold-a2    # the addressed gate, A2 layout (3.34M)
#   make snark-b1-membership      # the bearer membership circuit (492K)
snark-deposit-fold-a1:	rapidsnark
	$(SNARK_PATH) bash scripts/snark/setup_deposit_fold.sh a1

snark-deposit-fold-a2:	rapidsnark
	$(SNARK_PATH) bash scripts/snark/setup_deposit_fold.sh a2

snark-b1-membership:
	$(SNARK_PATH) bash scripts/snark/setup_b1_membership.sh

snark-deposit-fold-clean:
	rm -rf build/snark/deposit_fold_a1 build/snark/deposit_fold_a2

snark-b1-membership-clean:
	rm -rf build/snark/b1_membership

# End-to-end Notes fixtures: one mutually-consistent world per flavor (A1,
# A2, B1) with REAL proofs at every gate, consumed by test/NotesE2E.t.sol.
# Requires the mint, spend and deposit-gate setups to exist (see the
# prerequisites comment in scripts/snark/gen_e2e_fixtures.sh).
snark-e2e-fixtures:
	rm -rf build/snark/e2e
	$(SNARK_PATH) bash scripts/snark/gen_e2e_fixtures.sh

snark-e2e-clean:
	rm -rf build/snark/e2e alberta_buck/test/vectors/e2e

# BN254 G-generator stride-8 powers table for the fixed-base multiplications.
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
#
# Every Anvil-style sim below composes from a common matrix:
#
#   SIM_BACKEND  = anvil (subprocess, RPC-faithful; default)
#                | pyrevm (in-process revm: identical outputs, ~6.5x wall
#                  on short runs, far more on long ones -- see
#                  alberta_buck/sim/pyrevm_backend.py)
#   SIM_BASKET   = prorata (BuckBasketProRata + UniswapV3 venue facet)
#                | legacy (fused BuckBasket)
#   SIM_DIRECTOR = pairs (PairsRebalanceDirector: differential-mode
#                  tick-EMA ladders, quorum turn votes; default)
#                | vrate (BasketRebalanceDirector: share-deviation regime)
#   Env knobs (deploy-time director params, for short smoke runs):
#     DIRECTOR_DEADBAND_BP=10  DIRECTOR_QUORUM=2|3 (pairs)  DIRECTOR_WINDOW=3 (vrate)
#
#   e.g.  make nix-sim-rebalancing SIM_BACKEND=pyrevm SIM_DIRECTOR=vrate
#         SIM_BACKEND=pyrevm make nix-sim-historical SIM_YEARS=5
SIM_BACKEND	?= pyrevm        # pyrevm (default: in-process, fast) | anvil
SIM_DIRECTOR	?= pairs         # pairs (default) | vrate
SIM_SCENARIO	?= rebalancing   # routing | rebalancing (see scenario.py)
SIM_PKG		= alberta_buck.sim
SIM_TEST	= alberta_buck/test/test_routing_sim_web3.py

# Two-step Solidity build:
#  (1) v3 profile: compile 0.7.6 Uniswap V3 core contracts without via_ir.
#  (2) default profile: compile everything else with via_ir enabled
#      (required for BuckBasket's deep call stack).  Skips the 0.7.6
#      trigger to avoid the IR-incompatibility error.
# Both profiles share the same ``out/`` directory.
sim-build:	$(ROUTING_ARTIFACT) $(ROUTING_PRICES) stage-uniswap
	forge build --skip test --skip script $(FORGE_SKIP_GENERATED)

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
		node scripts/stage-uniswap.mjs >/dev/null
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
	python -m $(SIM_PKG) --scenario routing --days $(SIM_DAYS) --ticks-per-day $(SIM_TICKS) \
		--basket $(SIM_BASKET) --backend $(SIM_BACKEND) --director $(SIM_DIRECTOR)

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
	python -m $(SIM_PKG) --scenario rebalancing --days $(SIM_DAYS) --ticks-per-day $(SIM_TICKS) \
		--basket $(SIM_BASKET) --backend $(SIM_BACKEND) --director $(SIM_DIRECTOR)

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
		--backend $(SIM_BACKEND) --director $(SIM_DIRECTOR) \
		--out $(REBALANCING_VECTOR_PRORATA)

sim-run-rebalancing-traditional:	sim-build
	python -m $(SIM_PKG) --scenario rebalancing --days $(SIM_DAYS) \
		--ticks-per-day $(SIM_TICKS) --basket legacy \
		--backend $(SIM_BACKEND) \
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


# -- The reverting regime: oscillation without drift -------------------
#
# The committed price CSVs are GBM with +8%/+15%/+2% annual drift baked in,
# which confounds every reversion measurement made against them.  A
# rebalancing premium is a claim about harvesting oscillation and a demand
# agent is judged on buying cheap; in a market that rises throughout,
# buy-and-hold beats both for reasons unrelated to either mechanism.
#
# The revert regime keeps the same volatility and removes the drift: an
# Ornstein-Uhlenbeck walk pinned by a Brownian bridge, so each series ends
# EXACTLY where it began.  Whatever is earned here came from the
# oscillation, because there is no trend left to earn from.
#
#   make nix-sim-gen-prices-revert       # write prices/*-rev.csv (committed)
#   make nix-sim-rebalancing-revert      # run + plot the reverting regime

REBALANCING_VECTOR_REVERT = test/vectors/rebalancing-sim-revert.json

.PHONY: sim-gen-prices-revert sim-run-rebalancing-revert
.PHONY: sim-plot-rebalancing-revert sim-rebalancing-revert

sim-gen-prices-revert:
	python -m $(SIM_PKG).gen_prices --regime revert

sim-run-rebalancing-revert:	sim-build
	python -m $(SIM_PKG) --scenario rebalancing-revert --days $(SIM_DAYS) \
		--ticks-per-day $(SIM_TICKS) --basket $(SIM_BASKET) \
		--backend $(SIM_BACKEND) --director $(SIM_DIRECTOR) \
		--out $(REBALANCING_VECTOR_REVERT)

sim-plot-rebalancing-revert:	$(REBALANCING_VECTOR_REVERT)
	REB_VECTOR=$(REBALANCING_VECTOR_REVERT) \
		REB_OUT=images/rebalancing-sim-revert.png \
		python -m pytest $(SIM_REB_PLOT) -v -s

sim-rebalancing-revert:	sim-run-rebalancing-revert sim-plot-rebalancing-revert

# Who collects the rebalancing premium: per-commodity excursions, the
# basketValueInBuck break, and the depositor/treasury split.  The chain-sim
# answer to the article's Figure 1.
#   make nix-sim-plot-basket-split
.PHONY: sim-plot-basket-split
sim-plot-basket-split:	$(REBALANCING_VECTOR_REVERT)
	python -m pytest alberta_buck/sim/plot_basket_split.py -v -s


# ── Monetary operations A/B ───────────────────────────────────────────
#
# The BuckBasket's operations desk (alberta-buck-operations.org, phase 2)
# run as an agent against the live chain sim, compared against the same
# scenario and seed without it.  Two runs, then the comparison:
#
#   make nix-venv-sim-monetary-ops        # both runs + the table
#   make nix-venv-sim-compare-ops         # just the table, from existing runs
#
# Written to their own vectors so neither clobbers the committed baseline.

OPS_VECTOR_OFF	= test/vectors/monetary-ops-off.json
OPS_VECTOR_ON	= test/vectors/monetary-ops-on.json

.PHONY: sim-monetary-ops sim-run-ops-off sim-run-ops-on sim-compare-ops

# SIM_SEED selects the draw.  Both arms MUST use the same one -- the whole
# comparison is that they differ only in the roster.  Sweep several: the
# model's failure case reversed sign between one seed and nine.
SIM_SEED	?=
OPS_SEED	= $(if $(SIM_SEED),--seed $(SIM_SEED),)

sim-run-ops-off:	sim-build
	SIM_MONETARY_OPS=0 python -m $(SIM_PKG) --scenario rebalancing-revert \
		--days $(SIM_DAYS) --ticks-per-day $(SIM_TICKS) \
		--basket $(SIM_BASKET) --backend $(SIM_BACKEND) \
		--director $(SIM_DIRECTOR) $(OPS_SEED) --out $(OPS_VECTOR_OFF)

sim-run-ops-on:	sim-build
	SIM_MONETARY_OPS=1 python -m $(SIM_PKG) --scenario rebalancing-revert \
		--days $(SIM_DAYS) --ticks-per-day $(SIM_TICKS) \
		--basket $(SIM_BASKET) --backend $(SIM_BACKEND) \
		--director $(SIM_DIRECTOR) $(OPS_SEED) --out $(OPS_VECTOR_ON)

sim-compare-ops:
	python -m $(SIM_PKG).compare_ops $(OPS_VECTOR_OFF) $(OPS_VECTOR_ON)

sim-monetary-ops:	sim-run-ops-off sim-run-ops-on sim-compare-ops


# ── The ops BASKET A/B (contract, not agent) ──────────────────────────
#
# Same scenario and seed; the ONLY difference is which shell is deployed.
# The monetary-ops AGENT is off in both arms, so what is measured is the
# contract-side desk -- full-strength Q2/Q4 via burnFromBasket/mintFromBasket,
# which no agent can reach.
#
#   make nix-venv-sim-basket-ops        # both arms + the table
#
# Sweep the overlap between the fast desk and K's slow forcing with
# SIM_OPS_POSITION_BP / SIM_OPS_OUTRIGHT_BP / SIM_OPS_CAPITAL_USD.

BASKET_VECTOR_OFF	= test/vectors/basket-ops-off.json
BASKET_VECTOR_ON	= test/vectors/basket-ops-on.json

.PHONY: sim-basket-ops sim-run-basket-off sim-run-basket-on sim-compare-basket

sim-run-basket-off:	sim-build
	SIM_MONETARY_OPS=0 python -m $(SIM_PKG) --scenario rebalancing-revert \
		--days $(SIM_DAYS) --ticks-per-day $(SIM_TICKS) \
		--basket prorata --backend $(SIM_BACKEND) \
		--director $(SIM_DIRECTOR) $(OPS_SEED) --out $(BASKET_VECTOR_OFF)

sim-run-basket-on:	sim-build
	SIM_MONETARY_OPS=0 python -m $(SIM_PKG) --scenario rebalancing-revert \
		--days $(SIM_DAYS) --ticks-per-day $(SIM_TICKS) \
		--basket ops --backend $(SIM_BACKEND) \
		--director $(SIM_DIRECTOR) $(OPS_SEED) --out $(BASKET_VECTOR_ON)

sim-compare-basket:
	python -m $(SIM_PKG).compare_ops $(BASKET_VECTOR_OFF) $(BASKET_VECTOR_ON)

sim-basket-ops:	sim-run-basket-off sim-run-basket-on sim-compare-basket


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
		--backend $(SIM_BACKEND) --director $(SIM_DIRECTOR) \
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
		--backend $(SIM_BACKEND) --director $(SIM_DIRECTOR) \
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
		--backend $(SIM_BACKEND) \
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
	python -m $(SIM_PKG) --experiment $(EQ_EXPERIMENT) --backend $(SIM_BACKEND) $(EQ_SETS)

# Run any experiment by TOML basename: make sim-experiment-<name> runs
# alberta_buck/sim/experiments/<name>.toml -> test/vectors/eq-<name>.json.
sim-experiment-%:	sim-build
	python -m $(SIM_PKG) --backend $(SIM_BACKEND) \
		--experiment alberta_buck/sim/experiments/$*.toml $(EQ_SETS)

# -- Rebalancing under the equilibrium financial structure -------------
#
# The eq-baseline-5yr world (same window, basket recomposition, deploy
# knobs, borrower/saver loop and seed) plus a DirectorKeeperAgent, so the
# rebalance director's contribution to the closed loop is the only delta
# against eq-baseline-5yr-prorata.  Own vector/image names; neither the
# synthetic rebalancing scenarios nor the eq-baseline vectors are touched.
#
#   make nix-sim-rebalancing-eq          # run -> plot
#   make nix-sim-run-rebalancing-eq      # just the run (~35min+ full 5y)
#   make nix-sim-plot-rebalancing-eq     # just the plot
#
# Smoke first with a short horizon:  ... REBALANCING_EQ_DAYS=120
# (the full 5y window is still generated; only the run is truncated).

REBALANCING_EQ_TOML	= alberta_buck/sim/experiments/rebalancing-eq-5yr.toml
REBALANCING_EQ_VECTOR	= test/vectors/rebalancing-sim-eq.json
REBALANCING_EQ_IMAGE	= images/rebalancing-sim-eq.png
REBALANCING_EQ_DAYS	?=

.PHONY: sim-run-rebalancing-eq sim-plot-rebalancing-eq sim-rebalancing-eq

sim-run-rebalancing-eq:	sim-build
	python -m $(SIM_PKG) --experiment $(REBALANCING_EQ_TOML) \
		$(if $(REBALANCING_EQ_DAYS),--days $(REBALANCING_EQ_DAYS)) \
		--backend $(SIM_BACKEND) --director $(SIM_DIRECTOR) \
		--out $(REBALANCING_EQ_VECTOR)

sim-plot-rebalancing-eq:	$(REBALANCING_EQ_VECTOR)
	REB_VECTOR=$(REBALANCING_EQ_VECTOR) REB_OUT=$(REBALANCING_EQ_IMAGE) \
		python -m pytest $(SIM_REB_PLOT) -v -s

sim-rebalancing-eq:	sim-run-rebalancing-eq sim-plot-rebalancing-eq

# Variants by extension name (knobs / trend / disruption axes -- see the
# naming strategy in experiments/rebalancing-eq-5yr.toml):
#
#   make sim-rebalancing-eq-<ext>    # experiments/rebalancing-eq-5yr-<ext>.toml
#                                    # -> test/vectors/rebalancing-sim-eq-<ext>.json
#                                    # -> images/rebalancing-sim-eq-<ext>.png
#   make sim-compare-rebalancing-eq  # eqmetrics over all arms + the banked
#                                    # equilibrium baseline

sim-run-rebalancing-eq-%:	sim-build
	python -m $(SIM_PKG) \
		--experiment alberta_buck/sim/experiments/rebalancing-eq-5yr-$*.toml \
		$(if $(REBALANCING_EQ_DAYS),--days $(REBALANCING_EQ_DAYS)) \
		--backend $(SIM_BACKEND) --director $(SIM_DIRECTOR) \
		--out test/vectors/rebalancing-sim-eq-$*.json

sim-plot-rebalancing-eq-%:
	REB_VECTOR=test/vectors/rebalancing-sim-eq-$*.json \
		REB_OUT=images/rebalancing-sim-eq-$*.png \
		python -m pytest $(SIM_REB_PLOT) -v -s

sim-rebalancing-eq-%:	sim-run-rebalancing-eq-% sim-plot-rebalancing-eq-%

.PHONY: sim-compare-rebalancing-eq
sim-compare-rebalancing-eq:
	python -m alberta_buck.sim.eqmetrics \
		test/vectors/eq-baseline-5yr-prorata.json \
		test/vectors/rebalancing-sim-eq*.json

# THE MATRIX: re-run every rebalancing-eq-5yr*.toml arm against the
# CURRENT contracts (sim-build recompiles first, so contract changes --
# BuckBasket, Buck, the controller -- propagate to every arm), then print
# the eqmetrics table.  Each arm is hours at full cadence; MATRIX_JOBS
# arms run concurrently (pyrevm is in-process, one core each).
#
#   make nix-venv-sim-rebalancing-eq-matrix
#   make nix-venv-sim-rebalancing-eq-matrix MATRIX_JOBS=8

MATRIX_JOBS	?= 4

.PHONY: sim-rebalancing-eq-matrix
sim-rebalancing-eq-matrix:	sim-build
	ls alberta_buck/sim/experiments/rebalancing-eq-5yr*.toml \
	| xargs -P $(MATRIX_JOBS) -I{} sh -c '\
		ext=$$(basename {} .toml); ext=$${ext#rebalancing-eq-5yr}; \
		echo "=== arm $${ext:-base}: {}"; \
		python -m $(SIM_PKG) --experiment {} \
			--backend $(SIM_BACKEND) --director $(SIM_DIRECTOR) \
			--out test/vectors/rebalancing-sim-eq$$ext.json \
			> test/vectors/rebalancing-sim-eq$$ext.log 2>&1'
	$(MAKE) sim-compare-rebalancing-eq

# THE EXCURSION CATALOGUE: injected excursions x defender mixes x intensity
# on the portcast cast (2-year window, injection at day 365).  Arms are
# experiments/catalogue-<arm>.toml; mixes/scales are applied as --set
# overrides by alberta_buck.sim.catalogue (see its docstring).  Cells
# whose vector exists are reused (a killed grid resumes); the report is
# build/sim/catalogue/summary.md.  Design: REBALANCING-EQ.org.
#
#   make nix-venv-sim-catalogue                      # full grid (7 x 6)
#   make nix-venv-sim-catalogue CAT_ARMS=dump,squeeze CAT_MIXES=none,basket
#   make nix-venv-sim-catalogue CAT_SCALE=0.5,2       # intensity sweep
#   make nix-venv-sim-catalogue-report               # tables from vectors
#   make nix-venv-sim-catalogue-dump CAT_MIXES=all    # one arm

CAT_ARMS	?= none,dump,squeeze,grind-down,grind-up,spike,step
CAT_MIXES	?= none,usdc,buck,credit,basket,all
CAT_SCALE	?= 1
CAT_JOBS	?= 10
CAT_DIR		?= build/sim/catalogue

.PHONY: sim-catalogue sim-catalogue-report
sim-catalogue:	sim-build
	python -m alberta_buck.sim.catalogue --arms $(CAT_ARMS) \
		--mixes $(CAT_MIXES) --scale $(CAT_SCALE) \
		--jobs $(CAT_JOBS) --outdir $(CAT_DIR)

sim-catalogue-report:
	python -m alberta_buck.sim.catalogue --report-only \
		--arms $(CAT_ARMS) --mixes $(CAT_MIXES) --scale $(CAT_SCALE) \
		--outdir $(CAT_DIR)

sim-catalogue-%:	sim-build
	python -m alberta_buck.sim.catalogue --arms $* \
		--mixes $(CAT_MIXES) --scale $(CAT_SCALE) \
		--jobs $(CAT_JOBS) --outdir $(CAT_DIR)

# The realistic observation world: honest BuckCreditDebtorAgents (real
# premium credit + real funding gate) as the issuance channel, with
# savers/investors/arbs/whale/PID; growth regimes via arrive_mode knobs
# (see experiments/growth-*.toml).  pyrevm recommended.
#
#   make sim-debtors                 # run + plot the realistic world
sim-run-debtors:	sim-build
	python -m $(SIM_PKG) --backend $(SIM_BACKEND) \
		--experiment alberta_buck/sim/experiments/realistic.toml
sim-plot-debtors:
	python -m alberta_buck.sim.plot_octl \
		--data test/vectors/eq-eq-realistic.json \
		--out images/equilibrium-realistic.png
sim-debtors:	sim-run-debtors sim-plot-debtors

# The debtor-ledger AUDIT: one pinned-knob honest debtor in isolation, then
# assert the BUCK-vs-counterfactual accounting against a pure-Python ledger
# replica (amortization exactness, the conservation identity
# adv = interest_saved - premium - trade_loss, uniform superiority net of
# costs, and the xfail'd Jubilee-melt doctrine gap).
#
#   make nix-venv-sim-isolation      # pyrevm lives in the repo venv
sim-isolation:	sim-build
	python -m $(SIM_PKG) --backend $(SIM_BACKEND) \
		--experiment alberta_buck/sim/experiments/isolation.toml
	python -m pytest alberta_buck/test/test_debtor_ledger.py -v -s

# The LIVE simulation server: pyrevm worlds (one per client session) with
# WS /s/<sid>/{frames,control,rpc} on SIM_SERVER_PORT and a viem-ready
# HTTP JSON-RPC on SIM_SERVER_PORT+1.  Provision + start:
#
#   make nix-venv-sim-server              # the ~/.screenrc BuckSim screen
#   SIM_SERVER_EXPERIMENT=alberta_buck/sim/experiments/backdrop-ab.toml \
#       make nix-venv-sim-server          # serve a different world
#
# NB: 8797/8798 -- bucky.kundert.ca's MLX bot owns 8787.
SIM_SERVER_PORT       ?= 8797
SIM_SERVER_EXPERIMENT ?= alberta_buck/sim/experiments/backdrop.toml
SIM_SERVER_PACE       ?= 0

.PHONY: sim-server
sim-server:	sim-build
	python -m alberta_buck.sim.server \
		--experiment $(SIM_SERVER_EXPERIMENT) \
		--port $(SIM_SERVER_PORT) --pace $(SIM_SERVER_PACE)

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
.PHONY: core-wallet-vectors core-registry-vectors
.PHONY: core-vectors-sync core-vectors-check
.PHONY: poseidon-constants poseidon-constants-check

# Emit from the py_ecc reference (kernel_vectors.py forces
# BUCK_IDENTITY_BACKEND=py itself; the binding need not be built).
core-identity-vectors:
	python -m alberta_buck.wallet.kernel_vectors core/vectors/identity-kernel-vectors.json

# Wallet + registry kernel vectors (same doctrine: py reference emits,
# cargo/pytest/node replay; regenerating is an ABI-break-level event).
core-wallet-vectors:
	python -m alberta_buck.wallet.wallet_kernel_vectors core/vectors/wallet-kernel-vectors.json

core-registry-vectors:
	python -m alberta_buck.registry.kernel_vectors core/vectors/registry-kernel-vectors.json

# Each published crate carries its OWN copy of the vectors it replays:
# `cargo package` includes only files under the crate directory, so a crate
# whose tests reached ../../../test/vectors would ship a suite that cannot
# run for anyone downstream (alberta-buck-deployment.org, P0/4).  -sync
# refreshes the copies after regenerating the canonical files; -check fails
# on drift and gates core-test-rust, so a stale copy cannot ship.
core-vectors-sync:
	cp test/vectors/math-vectors.json            core/rust/buck-math/tests/vectors/
	cp test/vectors/identity.json                core/rust/buck-identity/tests/vectors/
	cp core/vectors/identity-kernel-vectors.json core/rust/buck-identity/tests/vectors/
	cp core/vectors/registry-kernel-vectors.json core/rust/buck-registry/tests/vectors/
	cp core/vectors/wallet-kernel-vectors.json   core/rust/buck-wallet/tests/vectors/

core-vectors-check:
	@cmp test/vectors/math-vectors.json            core/rust/buck-math/tests/vectors/math-vectors.json
	@cmp test/vectors/identity.json                core/rust/buck-identity/tests/vectors/identity.json
	@cmp core/vectors/identity-kernel-vectors.json core/rust/buck-identity/tests/vectors/identity-kernel-vectors.json
	@cmp core/vectors/registry-kernel-vectors.json core/rust/buck-registry/tests/vectors/registry-kernel-vectors.json
	@cmp core/vectors/wallet-kernel-vectors.json   core/rust/buck-wallet/tests/vectors/wallet-kernel-vectors.json
	@echo "vendored crate vectors match the canonical files"

# Poseidon round constants and MDS matrices, DERIVED from the Poseidon
# specification's Grain LFSR rather than copied from circomlib -- see NOTICE
# and the generator's docstring.  The output is byte-identical to circomlib's
# file, which -check proves whenever node_modules/circomlibjs is installed.
POSEIDON_GEN	= core/rust/buck-identity/constants/generate.py
CIRCOMLIB_JSON	= node_modules/circomlibjs/src/poseidon_constants.json

poseidon-constants:
	python3 $(POSEIDON_GEN) --write

poseidon-constants-check:
	python3 $(POSEIDON_GEN) --check $(if $(wildcard $(CIRCOMLIB_JSON)),--check-against $(CIRCOMLIB_JSON))

# ── Published contract artifacts (alberta-buck-contracts) ────────────
#
# The reproducible build behind the published package.  [profile.dist] in
# foundry.toml pins the compiler and writes to its own out-dist/, so a dev
# build can never decide what gets published; scripts/contracts-dist.py
# emits only the contracts we own and asserts the pin held.
#
#   make contracts-dist          # build + emit dist/contracts/
#   make contracts-dist-check    # verify the build (CI release gate)
#
# The Uniswap implementations are deliberately absent: they are BUSL-1.1 /
# GPL-2.0 / GPL-3.0 and consumers take them from Uniswap's own packages.
.PHONY: contracts-dist contracts-dist-build contracts-dist-check

contracts-dist-build:
	forge build --skip test --skip script $(FORGE_SKIP_GENERATED)

contracts-dist:		contracts-dist-build
	python3 scripts/contracts-dist.py --emit
	@echo "  npm:  core/contracts   pypi: core/contracts/python"

contracts-dist-check:	contracts-dist-build
	python3 scripts/contracts-dist.py --check

core-js-deps:
	cd core/js && npm ci

# Bundle the forge artifacts the JS platform needs into one importable
# module (core/js/artifacts/bundle.mjs) -- the browser cannot read out/.
core-js-artifacts:
	node core/js/bin/bundle-artifacts.mjs

# The Python kernel bindings: PyO3 cdylibs built with plain cargo (the
# .cargo/config.toml link flags stand in for maturin) and placed inside
# the buck_core package -- import buck_core.buck_math /
# buck_core.buck_identity / buck_core.buck_wallet / buck_core.buck_registry.
# The identity dylib defines THREE #[pymodule] entry points; copying the
# one artifact under each module filename gives three imports from one
# compiled kernel (Python calls the PyInit_<basename> matching the file).
# rm before cp: overwriting a .so in place keeps its inode, and macOS
# caches code signatures by inode -- a stale cache SIGKILLs (Killed: 9)
# the next import.  A fresh inode per copy sidesteps it.
core-build-py:
	python3 scripts/stage-kernel.py --dev

# The JS kernel bindings: wasm-pack (npm devDependency of core/js) emits
# nodejs-target packages into core/js/kernel/node/ (flat: buck_math.* and
# buck_identity.* coexist).  buck_math: BigInt ABI.  buck_identity: 0x-hex
# ABI wrapped by core/js/src/identity.js into the BigInt-native API.
#
# core/js/kernel IS the published npm package alberta-buck-kernel: node/ is
# its CommonJS half, web/ (core-build-wasm-web) its ES-module half, selected
# by the exports map in core/js/kernel/package.json.  wasm-pack drops its own
# package.json into each out-dir naming whichever crate built last; we
# overwrite both with the one field that must be right -- the module type
# governing how Node parses the .js files in that directory.
core-build-wasm:
	@test -x core/js/node_modules/.bin/wasm-pack || { echo "wasm-pack missing; run: make nix-core-js-deps"; exit 1; }
	cd core/rust/bindings/js && ../../../js/node_modules/.bin/wasm-pack \
		build --release --target nodejs \
		--out-dir ../../../js/kernel/node --out-name buck_math
	cd core/rust/bindings/js-identity && ../../../js/node_modules/.bin/wasm-pack \
		build --release --target nodejs \
		--out-dir ../../../js/kernel/node --out-name buck_identity
	@# wasm-pack writes a .gitignore of "*" into its out-dir; npm honours
	@# it even against the files allowlist, which would publish a package
	@# with no wasm in it at all.  Drop it.
	rm -f core/js/kernel/node/.gitignore
	echo '{ "type": "commonjs" }' > core/js/kernel/node/package.json

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
		--out-dir ../../../js/kernel/web --out-name buck_identity
	cd core/rust/bindings/js && ../../../js/node_modules/.bin/wasm-pack \
		build --release --target web \
		--out-dir ../../../js/kernel/web --out-name buck_math
	rm -f core/js/kernel/web/.gitignore
	echo '{ "type": "module" }' > core/js/kernel/web/package.json
	@# The demo pages fetch the web build from their own directory over
	@# http; they are static HTML with no bundler to resolve a package
	@# name, so they get a copy rather than a resolution.
	mkdir -p core/js/demo/wasm-web
	cp core/js/kernel/web/buck_*.js core/js/kernel/web/buck_*.wasm \
	   core/js/kernel/web/buck_*.d.ts core/js/demo/wasm-web/

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

# Stage the compiled kernels into the alberta-buck-kernel package.  The
# identity, wallet and registry kernels are ONE cdylib with three
# #[pymodule] entry points -- byte-identical files today -- so it ships once
# as _kernel.abi3.so and each module loads it under its own name.  4.9 MB of
# wheel becomes 1.9 MB.
.PHONY: core-kernel-dist
core-kernel-dist:
	python3 scripts/stage-kernel.py

core-build:	core-build-py core-build-wasm

core-test-py:
	python -m pytest core/python/tests -q

core-test-js:
	@test -d core/js/node_modules || { echo "core/js deps missing; run: make nix-core-js-deps"; exit 1; }
	cd core/js && node --test

core-test-rust:	core-vectors-check poseidon-constants-check
	cd core/rust && cargo test --quiet

core-test:	core-test-py core-test-js core-test-rust


# ── Dependencies ─────────────────────────────────────────────────────

# Dependencies are PINNED to exact tags.  An unpinned `forge install`
# fetches whatever HEAD is that day, and these libraries are compiled INTO
# our contracts -- so the published bytecode would change underneath us with
# no commit to show for it (alberta-buck-deployment.org, P2.5).  Bumping a
# pin is deliberate: rebuild, re-run contracts-dist-check, new version.
# These four are the only Solidity dependencies src/ and test/ actually
# import -- v3-core for its interfaces, chainlink for AggregatorV3Interface,
# OpenZeppelin for the token bases, forge-std for the harness.  The Uniswap
# IMPLEMENTATIONS are no longer dependencies at all: their compiled
# artifacts come from Uniswap's npm packages (scripts/stage-uniswap.mjs).
#
# Tags are exact and are the GIT tag, which for these repositories is NOT
# the npm version -- Uniswap v3-core publishes npm 1.0.1 from a repository
# whose latest tag is v1.0.0, and chainlink tags without a leading "v".
# Guessing from package.json is how CI first failed here.
install:
	forge install OpenZeppelin/openzeppelin-contracts@v5.6.1 --no-git
	forge install smartcontractkit/chainlink-brownie-contracts@1.3.0 --no-git
	forge install Uniswap/v3-core@v1.0.0 --no-git
	forge install foundry-rs/forge-std@v1.16.1 --no-git

# Additionally required by the Python routing sim, which builds the
# Universal Router as its own sub-project for its artifact.
install-sim:	install
	forge install Uniswap/universal-router@v1.6.0 --no-git

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
