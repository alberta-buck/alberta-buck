// buildOnePool: the smallest real world -- TOKEN and USDC (MockERC20s), a
// Uniswap V3 factory + one TOKEN/USDC pool initialized at `price`, and a
// SimLP helper stocked and LP'd full-range, mirroring the Python
// deploy.py's TOKEN/USDC pool construction.  Runs on ANY session --
// Tevm in-process (standalone JS sim) or anvil -- from the same code.

import { Q96, fullRangeTicks, sqrtPriceX96 } from "../v3.js";

const FEE = 3000;              // 60-tick spacing, as the sim's TOKEN/USDC pools
const TICK_SPACING = 60;

/**
 * @param session   a Session
 * @param artifacts (name) => {abi, bytecode} (nodefs loadArtifact, or a
 *                  bundled map in the browser)
 * @param opts.price      USDC base units (1e6) per whole TOKEN (default $2.50)
 * @param opts.usdcDepth  USDC base units of full-range depth (default $10M)
 * @returns world {session, usdc, token, pool, simlp}
 */
export async function buildOnePool(session, artifacts,
                                   { price = 2_500_000n,
                                     usdcDepth = 10_000_000n * 10n ** 6n } = {}) {
  const gas = 15_000_000n;     // deploys fit tevm's default block gas limit
  const usdc = await session.deploy(artifacts("MockERC20"),
    ["USD Coin", "USDC", 6], { name: "USDC", gas });
  const token = await session.deploy(artifacts("MockERC20"),
    ["Test Token", "TOK", 18], { name: "TOK", gas });
  const factory = await session.deploy(artifacts("UniswapV3Factory"), [],
    { name: "UniswapV3Factory", gas });
  const simlp = await session.deploy(artifacts("SimLP"), [],
    { name: "SimLP", gas });

  await session.send(factory, "createPool",
    [token.address, usdc.address, FEE], { tag: "onepool:createPool" });
  const poolAddr = await session.call(factory, "getPool",
    [token.address, usdc.address, FEE]);
  const pool = session.contractAt(artifacts("UniswapV3Pool").abi, poolAddr);

  const sqrt = sqrtPriceX96(token.address, 10n ** 18n, usdc.address, price);
  await session.send(pool, "initialize", [sqrt], { tag: "onepool:init" });

  // Stock SimLP generously (it pays both mint and later swaps from its
  // own balance), then mint full-range liquidity sized to usdcDepth.
  const tokenStock = (4n * usdcDepth * 10n ** 18n) / price;
  await session.send(usdc, "mint", [simlp.address, 4n * usdcDepth],
                     { tag: "onepool:stock-usdc" });
  await session.send(token, "mint", [simlp.address, tokenStock],
                     { tag: "onepool:stock-token" });

  const usdcIs0 = usdc.address.toLowerCase() < token.address.toLowerCase();
  const L = usdcIs0 ? (usdcDepth * sqrt) / Q96 : (usdcDepth * Q96) / sqrt;
  const [lo, hi] = fullRangeTicks(TICK_SPACING);
  const [t0, t1] = usdcIs0 ? [usdc.address, token.address]
                           : [token.address, usdc.address];
  await session.send(simlp, "mint",
    [pool.address, lo, hi, L, t0, t1], { tag: "onepool:mint-liquidity" });

  return { session, usdc, token, pool, simlp };
}
