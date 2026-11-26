import { parseEventLogs } from "viem";

/** Match the basket's own swaps to completed legs of each confirmed wheel response. */
export function rebalanceFills(world, day, tick) {
  const basket = world.basket.address.toLowerCase();
  const policy = world.machine.policy;
  const pools = new Map(world.tokens.map((t, i) => [t.poolBuck.address.toLowerCase(), i]));
  const fills = [];
  for (const receipt of world.lastWheelReceipts ?? []) {
    if (receipt.status !== "success") continue;
    const logs = [
      ...parseEventLogs({ abi: world.net.upkeep.abi, logs: receipt.logs, eventName: ["Responded", "WheelWork"] })
        .filter((event) => event.address.toLowerCase() === basket),
      ...parseEventLogs({ abi: policy.abi, logs: receipt.logs, eventName: "RebalanceGaps" })
        .filter((event) => event.address.toLowerCase() === policy.address.toLowerCase()),
      ...parseEventLogs({ abi: world.tokens[0].poolBuck.abi, logs: receipt.logs, eventName: "Swap" })
        .filter(({ address, args }) => pools.has(address.toLowerCase())
          && args.sender.toLowerCase() === basket && args.recipient.toLowerCase() === basket),
    ].sort((a, b) => a.logIndex - b.logIndex);
    let decision = null;
    const swaps = new Map(), legs = [];
    for (const { eventName, args, address, logIndex } of logs) {
      if (eventName === "Swap") {
        swaps.set(pools.get(address.toLowerCase()), { args, logIndex });
      } else if (eventName === "RebalanceGaps") {
        decision = args;
        swaps.clear();
      } else if (eventName === "Responded" && (args.action === 1 || args.action === 2)) {
        const buy = args.action === 2, token = Number(buy ? args.buy : args.sell);
        const swap = swaps.get(token);
        swaps.delete(token);
        if (!swap) continue; // An LP withdrawal can succeed with no market sale.
        const { amount0, amount1 } = swap.args;
        const tokenFirst = world.tokens[token].erc20.address.toLowerCase() < world.buck.address.toLowerCase();
        const [asset, buck] = tokenFirst ? [amount0, amount1] : [amount1, amount0];
        if (buy ? asset >= 0n || buck <= 0n : asset <= 0n || buck >= 0n) continue;
        legs.push({ day, tick, token, side: buy ? "buy" : "sell",
          token_amount: String(buy ? -asset : asset), buck_amount: String(buy ? buck : -buck),
          planned_sell: buy ? null : String(args.proceeds),
          urgency: decision ? String(decision.urgency[token]) : null,
          gap: decision ? String(decision.gaps[token]) : null,
          transaction: receipt.transactionHash, log_index: swap.logIndex });
      } else if (eventName === "WheelWork" && args.kind === 2) {
        const group = `${receipt.transactionHash}:${logIndex}`;
        fills.push(...legs.map((leg) => ({ ...leg, group })));
        legs.length = 0;
        swaps.clear();
        decision = null;
      }
    }
  }
  return fills;
}
