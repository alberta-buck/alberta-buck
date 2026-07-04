// The journal-parity mini-scenario, JS side: EXACTLY the op sequence
// alberta_buck.sim.mini emits (same tags, same order, same values) --
// buildOnePool, stock the trader, then per day a whale pin to the
// scenario's fixed target and a round-trip trade (with the day-0 SPL
// demo, declared expect:"revert").  core/vectors/mini-scenario.json is
// the single source of the numbers.

import { PinWhale } from "../agents/whale.js";
import { RoundTripTrader } from "../agents/trader.js";
import { runDays } from "../world.js";
import { buildOnePool } from "./onepool.js";

export async function runMini(session, artifacts, sc) {
  const world = await buildOnePool(session, artifacts, {
    price: BigInt(sc.price),
    usdcDepth: BigInt(sc.usdcDepth),
  });
  const { token, usdc, pool, simlp } = world;

  await session.send(token, "mint",
    [session.account.address, BigInt(sc.traderStock)],
    { tag: "mini:stock-trader" });

  const whale = new PinWhale({
    simlp, pool, token: token.address, tokenDec: sc.tokenDec,
    quote: usdc.address,
    refPrice: (day) => BigInt(sc.targets[day]),
  });
  const trader = new RoundTripTrader({
    simlp, pool, token, quote: usdc, amount: BigInt(sc.tradeTokens),
  });

  await runDays(world, [whale, trader], { days: sc.days, ticksPerDay: 1 });
  return world;
}
