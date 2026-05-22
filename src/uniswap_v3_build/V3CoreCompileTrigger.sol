// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity =0.7.6;

// Compile-trigger stub for Uniswap V3 core artifacts (Factory + Pool).
//
// Foundry only compiles files referenced by the import graph, so the stock
// `lib/v3-core/contracts/*.sol` would never produce out/ artifacts unless
// something imports them.  This file does nothing at runtime; its purpose
// is to pull the v3-core sources into the build so test fixtures can deploy
// them via `vm.deployCode`.
//
// `UniswapV3Factory` inherits `UniswapV3PoolDeployer`, which `new`s the
// `UniswapV3Pool` contract directly -- so referencing the factory drags
// in both the deployer and the pool implementation.

import "@uniswap/v3-core/contracts/UniswapV3Factory.sol";
