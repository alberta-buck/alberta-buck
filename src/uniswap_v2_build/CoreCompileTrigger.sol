// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity =0.5.16;

// Compile-trigger stub for Uniswap V2 core artifacts.
//
// Foundry only compiles files referenced by the import graph, so the stock
// `lib/v2-core/contracts/*.sol` would never produce out/ artifacts unless
// something imports them.  This file does nothing at runtime; its purpose
// is to pull the v2-core sources into the build so test/UniswapV2.t.sol
// can deploy them via `vm.deployCode`.

import "@uniswap/v2-core/contracts/UniswapV2Factory.sol";
import "@uniswap/v2-core/contracts/UniswapV2Pair.sol";
