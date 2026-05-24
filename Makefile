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


.PHONY: all build test clean fmt snapshot
.PHONY: fork-sepolia fork-mainnet fork-mainnet-cache anvil stop-anvil
.PHONY: deploy-local deploy-sepolia
.PHONY: install update
.PHONY: test-python venv-activate
.PHONY: vectors plots images
.PHONY: vector-lifecycle vector-equilibrium vector-arb
.PHONY: plot-lifecycle plot-equilibrium plot-arb
.PHONY: sim sim-build sim-run sim-test sim-plot
.PHONY: sim-rebalancing sim-run-rebalancing sim-plot-rebalancing
.PHONY: prices-routing vector-routing plot-routing images-routing


# ── Build ────────────────────────────────────────────────────────────

all:			build test

build:
	forge build $(FORGE_OPTS)

test:
	forge test $(FORGE_OPTS) -vvv
unit-%:
	forge test $(FORGE_OPTS) --match-test $* -vvv
path-%:
	forge test $(FORGE_OPTS) --match-path $* -vvv

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

venv-activate:
	pip install -e ".[tests]"


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


# ── Routing stabilizer simulation ─────────────────────────────────────
#
# Builds the Universal Router artifact, generates price CSVs, runs the
# Forge test, and renders the plot.  The UR lives in a sub-project with
# its own foundry.toml (solc 0.8.26, via_ir); we build it separately and
# stage the artifact, then temporarily disable its foundry.toml during
# the main project's forge test to avoid test-discovery interference.
#
#   make images-routing       # prices -> router -> test -> plot (full pipeline)
#   make vector-routing       # just the forge test (after prices + router)
#   make plot-routing         # just the Python plot
#   make prices-routing       # regenerate price CSVs only

ROUTING_DIR	= test/stabilizer-routing-op47
SIM_PRICES_DIR  = alberta_buck/sim/prices
SIM_PLOT_SCRIPT = alberta_buck/sim/plot_routing.py
SIM_GEN_PRICES  = alberta_buck/sim/gen_prices.py
SIM_ARTIFACTS   = alberta_buck/sim/artifacts

ROUTING_PRICES	= $(SIM_PRICES_DIR)/paxg.csv $(SIM_PRICES_DIR)/cbbtc.csv $(SIM_PRICES_DIR)/aoil.csv
ROUTING_ARTIFACT = $(SIM_ARTIFACTS)/UniversalRouter.json
ROUTING_VECTOR	= test/vectors/routing-sim.json
ROUTING_IMAGE	= images/routing-sim.png

prices-routing:	$(ROUTING_PRICES)

# Generate price CSVs in alberta_buck/sim/prices/; symlink back to
# test/stabilizer-routing-op47/ for the legacy Forge test compatibility.
$(ROUTING_PRICES): $(SIM_GEN_PRICES)
	python3 $(SIM_GEN_PRICES)
	mkdir -p $(ROUTING_DIR)
	cd $(ROUTING_DIR) && \
		ln -sf ../../$(SIM_PRICES_DIR)/paxg.csv paxg.csv && \
		ln -sf ../../$(SIM_PRICES_DIR)/cbbtc.csv cbbtc.csv && \
		ln -sf ../../$(SIM_PRICES_DIR)/aoil.csv aoil.csv

$(ROUTING_ARTIFACT):
	( cd lib/universal-router && FORK_URL=http://localhost forge build --skip test --skip script )
	mkdir -p $(SIM_ARTIFACTS)
	cp lib/universal-router/out/UniversalRouter.sol/UniversalRouter.json $@
	mkdir -p $(ROUTING_DIR)/artifacts
	cd $(ROUTING_DIR)/artifacts && ln -sf ../../$(SIM_ARTIFACTS)/UniversalRouter.json UniversalRouter.json

vector-routing:	$(ROUTING_PRICES) $(ROUTING_ARTIFACT)
	@test -f lib/universal-router/foundry.toml.bak || \
		cp lib/universal-router/foundry.toml lib/universal-router/foundry.toml.bak 2>/dev/null || true
	cp lib/universal-router/foundry.toml lib/universal-router/foundry.toml.bak 2>/dev/null; \
	touch lib/universal-router/foundry.toml 2>/dev/null; \
	rm lib/universal-router/foundry.toml 2>/dev/null || true; \
	forge test $(FORGE_OPTS) --match-contract RoutingSimTest --skip 'test/stabilizer-routing-dsv4/*' -vv; \
	EX=$$?; mv lib/universal-router/foundry.toml.bak lib/universal-router/foundry.toml 2>/dev/null || true; \
	exit $$EX

plot-routing:	$(ROUTING_VECTOR)
	python -m pytest $(SIM_PLOT_SCRIPT) -v -s

$(ROUTING_IMAGE): $(ROUTING_VECTOR)
	python -m pytest $(SIM_PLOT_SCRIPT) -v -s

images-routing:	prices-routing $(ROUTING_ARTIFACT) vector-routing plot-routing


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
sim-build:	$(ROUTING_ARTIFACT) $(ROUTING_PRICES)
	FOUNDRY_PROFILE=v3 forge build --skip test --skip script
	forge build --skip test --skip script --skip 'src/uniswap_v3_build/*'

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
