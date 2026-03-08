#
# Alberta Buck — Ethereum Smart Contracts
#
# Foundry/Anvil-based build, test, and local fork environment.
#

# RPC endpoints for forking.  Override via environment or .env file.
# Free tier: https://dashboard.alchemy.com/ or https://infura.io/
# Local node: http://localhost:8545 (Reth, Geth, Erigon)
-include .env
SEPOLIA_RPC_URL		?= https://eth-sepolia.g.alchemy.com/v2/YOUR_KEY
MAINNET_RPC_URL		?= https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY

# Anvil defaults
ANVIL_PORT		?= 8545
ANVIL_BLOCK_TIME	?= 0
FORK_BLOCK		?=

# Forge options
FORGE_OPTS		?= --optimize --optimizer-runs 200

# Fork block pinning (deterministic tests): set FORK_BLOCK=12345 to pin
ifdef FORK_BLOCK
  ANVIL_FORK_OPTS	= --fork-block-number $(FORK_BLOCK)
else
  ANVIL_FORK_OPTS	=
endif


.PHONY: all build test clean fmt snapshot
.PHONY: fork-sepolia fork-mainnet anvil stop-anvil
.PHONY: deploy-local deploy-sepolia
.PHONY: install update


# ── Build ────────────────────────────────────────────────────────────

all:			build test

build:
	forge build $(FORGE_OPTS)

test:
	forge test $(FORGE_OPTS) -vvv

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

stop-anvil:
	-pkill -f "anvil --port $(ANVIL_PORT)" 2>/dev/null


# ── Deployment ───────────────────────────────────────────────────────

deploy-local:
	forge script script/Deploy.s.sol --broadcast --rpc-url http://localhost:$(ANVIL_PORT) -vvv

deploy-sepolia:
	forge script script/Deploy.s.sol --broadcast --rpc-url $(SEPOLIA_RPC_URL) --verify -vvv


# ── Dependencies ─────────────────────────────────────────────────────

install:
	forge install OpenZeppelin/openzeppelin-contracts --no-git
	forge install smartcontractkit/chainlink-brownie-contracts --no-git
	forge install Uniswap/v3-core --no-git
	forge install Uniswap/v3-periphery --no-git

update:
	forge update


# ── Cleanup ──────────────────────────────────────────────────────────

clean:
	forge clean
	rm -rf cache out broadcast
