// Contract execution for the portable household. All money stays in its borrower or savings proxy.
import { encodeFunctionData, keccak256, toBytes } from "viem";
import { Q96, MIN_SQRT_RATIO, MAX_SQRT_RATIO } from "../v3.js";
import { MINT_MAX } from "../buckworld.js";

const E6 = 1_000_000n, E18 = 10n ** 18n;
const min = (...xs) => xs.reduce((a, b) => a < b ? a : b);
const max = (a, b) => a > b ? a : b;
const units = (v) => BigInt(Math.floor(v * 1e6));
const whole = (v) => Number(v) / 1e6;
const SINK = "0x000000000000000000000000000000000000dEaD";
const DEPOSITED = keccak256(toBytes("Deposited(address,uint256,address,uint256,uint256,uint128)"));

export function installHouseholdOps(world) {
  const { session: s, buck, usdc, basket, kctrl, credit } = world;
  const exec = (proxy, contract, name, args, tag, expect = "success") => s.send(proxy, "exec",
    [contract.address, encodeFunctionData({ abi: contract.abi, functionName: name, args })],
    { tag, gas: 5_000_000n, expect });
  const cash = (account) => s.call(usdc, "balanceOf", [world.holderAddress(account)]);
  const reserves = async (pool, input) => {
    const [L, slot, t0] = await Promise.all([s.call(pool, "liquidity"), s.call(pool, "slot0"), s.call(pool, "token0")]);
    const r0 = L * Q96 / slot[0], r1 = L * slot[0] / Q96;
    return input.address.toLowerCase() === t0.toLowerCase() ? [r0, r1] : [r1, r0];
  };
  const swap = async (account, pool, input, output, amount, recipient, tag) => {
    if (amount <= 0n) return { got: 0n, spent: 0n };
    const proxy = await world.proxyFor(account);
    const [t0, t1] = await Promise.all([s.call(pool, "token0"), s.call(pool, "token1")]);
    const zero = input.address.toLowerCase() === t0.toLowerCase();
    const outputView = output.address === buck.address ? "signedBalanceOf" : "balanceOf";
    const before = await s.call(output, outputView, [recipient]);
    const available = await s.call(input, "balanceOf", [proxy.address]);
    const inputView = input.address === buck.address ? "signedBalanceOf" : "balanceOf";
    const inputBefore = await s.call(input, inputView, [proxy.address]);
    amount = min(amount, available);
    await s.send(proxy, "swap", [pool.address, recipient, zero, amount,
      zero ? MIN_SQRT_RATIO + 1n : MAX_SQRT_RATIO - 1n, t0, t1], { tag, gas: 3_000_000n });
    return { got: (await s.call(output, outputView, [recipient])) - before,
      spent: inputBefore - (await s.call(input, inputView, [proxy.address])) };
  };
  const pay = async (account, amount, tag = "household:pay") => {
    amount = min(amount, await cash(account));
    if (amount > 0n) await exec(await world.proxyFor(account), usdc, "transfer", [SINK, amount], tag);
    return amount;
  };
  const read = async (account) => {
    const cs = await world.creditState(account), [ru, rb] = await reserves(world.poolUB, usdc);
    const [k, bv, spot, signed] = await Promise.all([world.K(), world.bvib(), world.spotUB(),
      s.call(buck, "signedBalanceOf", [world.holderAddress(account)])]);
    let deposit = 0n;
    for (const tid of world._credits.get(account.address) ?? []) deposit += await s.call(buck, "mintsPrincipal", [tid]);
    let insuranceAge = 0n;
    if (deposit) {
      const [raw, fee] = await Promise.all([s.call(buck, "rawBalanceOf", [world.poolAcct]),
        s.call(buck, "feeOwing", [world.poolAcct])]);
      insuranceAge = raw ? min(deposit, fee * deposit / raw) : deposit;
    }
    return { ...cs, signed, deposit, insuranceAge, k: Number(k) / 1e18, ru: whole(ru), rb: whole(rb),
      fee: world.fees.ub / 1e6, basket_usd: Number(bv) / 1e18 * whole(spot), spot: whole(spot),
      cash: await cash(account) };
  };
  const buy = (account, budget) => swap(account, world.poolUB, usdc, buck, budget,
    world.holderAddress(account), "household:buy");

  const issue = async (account, state, verdict, buffer, premiumBp) => {
    const out = { sold: 0n, got: 0n, premium: 0n, gateSpent: 0n, gateGot: 0n, reason: "hold" };
    const fixed = units(-verdict.parts.fixed), penalty = units(-verdict.parts.penalty);
    const available = await cash(account);
    if (available < buffer + fixed + penalty) return { ...out, reason: "cash-wait" };
    const want = units(verdict.buck), me = world.holderAddress(account);
    const k = await world.K(), unactivated = units(state.unactivated);
    const per = k - BigInt(premiumBp) * 10n * 10n ** 14n;
    const cap = per > 0n ? unactivated * per / E18 : 0n;
    const spendable = await s.call(buck, "balanceOf", [me]);
    const amount = min(max(0n, want - spendable) * 1002n / 1000n, cap);
    const proxy = await world.proxyFor(account);
    try {
      if (amount >= E6) {
        await s.send(kctrl, "compute", [], { tag: "household:gate", expect: "either" });
        const arg = amount * 100n >= cap * 99n ? MINT_MAX : amount;
        const [, principal] = await s.call(buck, "quoteMint", [arg, world._credits.get(account.address)]);
        const ff = await s.call(kctrl, "fundingFactor");
        const required = principal * ff / E18 * 1005n / 1000n;
        const short = max(0n, required - await s.call(buck, "balanceOf", [me]));
        if (short) {
          const [ru, rb] = (await reserves(world.poolUB, usdc)).map(Number);
          const target = Math.min(rb * .5, Number(short) * 1.01 + 1e6);
          const cost = BigInt(Math.floor(target * ru / ((rb - target) * (1 - world.fees.ub / 1e6)))) + 1n;
          const b = await buy(account, min(cost, max(0n, available - buffer - fixed - penalty)));
          out.gateSpent += b.spent; out.gateGot += b.got;
        }
        if (await s.call(buck, "balanceOf", [me]) < required) return { ...out, reason: "gate-wait" };
        const before = await s.call(buck, "signedBalanceOf", [me]);
        const receipt = await exec(proxy, buck, "mint", [arg], "household:activate", "either");
        if (receipt.status !== "success") return { ...out, reason: "mint-wait" };
        out.premium = max(0n, before - await s.call(buck, "signedBalanceOf", [me]));
      }
      const saleAmount = min(want, await s.call(buck, "balanceOf", [me]));
      const result = await swap(account, world.poolUB, buck, usdc, saleAmount, me, "household:issue");
      out.sold = result.spent; out.got = result.got; out.reason = "issued";
      return out;
    } catch (e) { return { ...out, reason: "error", error: String(e).slice(0, 200) }; }
  };

  const invest = async (agent, budget) => {
    const out = { spent: 0n };
    const { account, tokenIdx } = agent;
    agent.savings ??= await world.proxyFor({ address: `savings:${account.address}` });
    const saver = agent.savings, token = world.tokens[tokenIdx].erc20;
    let held = await s.call(token, "balanceOf", [saver.address]);
    try {
      if (held === 0n && budget > E6) {
        const [ru] = await reserves(world.tokens[tokenIdx].poolUsdc, usdc);
        const spend = min(budget, max(0n, await cash(account) - agent.buffer),
          BigInt(Math.floor(Number(ru) * agent.p.max_impact_bp / 20000)));
        if (spend > E6) {
          const r = await swap(account, world.tokens[tokenIdx].poolUsdc, usdc, token, spend,
            saver.address, "household:invest-acquire");
          out.spent = r.spent;
          held = await s.call(token, "balanceOf", [saver.address]);
        }
      }
      if (!held) return out;
      await exec(saver, token, "approve", [basket.address, held], "household:invest-approve");
      const receipt = await exec(saver, basket, "depositToken", [token.address, held, 0n], "household:deposit", "either");
      if (receipt.status !== "success") return { ...out, error: "deposit refused; TOKEN remains in savings" };
      // Receipt id is the second indexed argument of Deposited(address,uint256,...).
      const log = receipt.logs.find((l) => l.address.toLowerCase() === basket.address.toLowerCase()
        && l.topics[0] === DEPOSITED);
      if (!log) throw new Error("deposit receipt event missing");
      const rid = BigInt(log.topics[2]);
      const dep = await s.call(basket, "deposits", [rid]);
      const shares = world.basketKind === "machine" ? (await s.call(basket, "holdings", [rid]))[0] : 0n;
      world.receipts += 1;
      return { ...out, rid, principal: dep[0], shares };
    } catch (e) { return { ...out, error: String(e).slice(0, 200) }; }
  };
  const mark = async (agent) => {
    const st = await read(agent.account);
    const basketMark = world.basketKind === "machine" && agent.shares
      ? agent.shares * (await s.call(basket, "sharePrice")) / E18 : agent.basketPrincipal;
    let pending = 0n;
    if (agent.savings) {
      const held = await s.call(world.tokens[agent.tokenIdx].erc20, "balanceOf", [agent.savings.address]);
      const [ru, rt] = await reserves(world.tokens[agent.tokenIdx].poolUsdc, usdc);
      pending = rt ? held * ru / rt : 0n;
    }
    return { ...st, basketMark, pending, nw: st.cash + pending - agent.mortgage - agent.livingArrears - agent.premiumArrears
      + BigInt(Math.trunc(Number(basketMark + st.deposit - st.insuranceAge - st.drawn + min(st.jub, st.drawn)
        + max(0n, st.signed)) * st.spot)) };
  };
  // The investor deposits the BUCKs it holds. When the basket's price guard refuses, it offers half,
  // up to four times; what is still refused stays for the next decision.
  const depositBuck = async (agent) => {
    const proxy = await world.proxyFor(agent.account), me = world.holderAddress(agent.account);
    let amount = await s.call(buck, "balanceOf", [me]), refusals = 0, receipt = null;
    for (let i = 0; i < 4 && amount > E6; i++, amount /= 2n) {
      await exec(proxy, buck, "approve", [basket.address, amount], "investor:approve");
      receipt = await exec(proxy, basket, "depositToken", [buck.address, amount, 0n], "investor:deposit", "either");
      if (receipt.status === "success") break;
      refusals += 1;
    }
    if (!receipt || receipt.status !== "success") return { refusals };
    const log = receipt.logs.find((l) => l.address.toLowerCase() === basket.address.toLowerCase()
      && l.topics[0] === DEPOSITED);
    if (!log) throw new Error("deposit receipt event missing");
    const rid = BigInt(log.topics[2]);
    const dep = await s.call(basket, "deposits", [rid]);
    const shares = world.basketKind === "machine" ? (await s.call(basket, "holdings", [rid]))[0] : 0n;
    world.receipts += 1;
    return { rid, principal: dep[0], shares, refusals };
  };
  // Mirrors equilibrium_agents._impact_cap: the input that moves a constant-product pool at most `bp`.
  const impactCap = (reserve, bp) => bp <= 0 || reserve <= 0n ? 0n
    : BigInt(Math.trunc(Number(reserve) * (1 / Math.sqrt(1 - Math.min(.99, bp / 10000)) - 1)));
  // The investor's exit: redeem every receipt, then sell what came back, a capped bite per decision.
  const withdraw = async (agent) => {
    const proxy = await world.proxyFor(agent.account), me = world.holderAddress(agent.account);
    const out = { redeemed: [], usd: 0n, left: false, refused: false };
    for (const rid of [...agent.receipts]) {
      const r = await exec(proxy, basket, "redeem", [rid, 0n, 0n], "investor:redeem", "either");
      if (r.status !== "success") return { ...out, refused: true, left: true };
      out.redeemed.push(rid);
    }
    const before = await cash(agent.account), bp = agent.p.max_impact_bp;
    const held = await s.call(buck, "balanceOf", [me]);
    if (held > E6) {
      // Leave half a BUCK: demurrage accrues between the read and the swap.
      const [, rb] = await reserves(world.poolUB, usdc), bite = min(held - E6 / 2n, max(E6, impactCap(rb, bp)));
      await swap(agent.account, world.poolUB, buck, usdc, bite, me, "investor:sell");
      out.left ||= held - bite > E6;
    }
    for (const t of world.tokens) {
      const amount = await s.call(t.erc20, "balanceOf", [me]);
      if (amount <= 0n) continue;
      const [, rt] = await reserves(t.poolUsdc, usdc), bite = min(amount, max(1n, impactCap(rt, bp)));
      await swap(agent.account, t.poolUsdc, t.erc20, usdc, bite, me, "investor:sell");
      out.left ||= bite < amount;
    }
    out.usd = await cash(agent.account) - before;
    return out;
  };
  world.households = { cash, pay, read, buy, issue, invest, mark, reserves, depositBuck, withdraw };
  return world;
}
