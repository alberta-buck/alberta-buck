// BasketSaver: the live evolution of prototypes/basket-investor.js --
// USDC wealth deployed into the BuckBasket for a term, acquiring the
// constituent TOKEN via whichever route quotes the better after-swap
// valuation (direct, or through BUCK -- world.buyTokenBest).  A single
// registered EOA: deposits need no identity, but redemption pays BUCK,
// so setup runs the real onboarding ceremony.
//
// ROI accounting follows the Python DM agents: USD committed vs USD
// realized, with dollar-days so returns annualize
// (profitUsd * 365n / dollarDays).

import { fundAccount, onboard } from "../buckworld.js";

export class BasketSaver {
  /**
   * @param opts.account  fresh viem account (funded + onboarded in setup)
   * @param opts.budget   USDC base units to deploy per cycle
   * @param opts.holdDays days from deposit to redemption
   * @param opts.tokenIdx which basket constituent to acquire
   * @param opts.fields   identity KYC fields (defaults provided)
   */
  constructor({ account, budget = 25_000n * 10n ** 6n, holdDays = 60,
                tokenIdx = 0, fields = null }) {
    Object.assign(this, { account, budget, holdDays, tokenIdx, fields });
    this.position = null;
    this.ledger = [];        // {day, event, usd} -- the ROI chart
    this.profitUsd = 0n;
    this.dollarDays = 0n;
  }

  async setup(world) {
    await fundAccount(world, this.account.address);
    await onboard(world, this.account, this.fields ?? {
      given_name: "Sana", family_name: "Saver",
      id_type: "Alberta Identity Card", id_number: "AIC-2026-0055501",
      jurisdiction: "Alberta, Canada", date_of_birth: "1991-05-05",
      issued_at: "2026-07-04T00:00:00Z", epoch: 42,
    });
    await world.fiatIn(this.account, this.budget, { tag: "saver:endow" });
    this.usdc = this.budget;
  }

  async act(world, day, tick) {
    if (tick !== 0) return;

    if (!this.position) {
      if (this.usdc < 1_000n * 10n ** 6n) return;   // spent: hold
      const spend = this.usdc;
      const { out, viaBuck } = await world.buyTokenBest(
        this.tokenIdx, spend, this.account, { tag: `saver:acquire:d${day}` });
      const { receiptId } = await world.basketDeposit(
        this.tokenIdx, out, this.account, { tag: `saver:deposit:d${day}` });
      this.usdc = 0n;
      this.position = { receiptId, day, costUsd: spend };
      this.ledger.push({ day, event: "deposit", usd: -spend, viaBuck });
      return;
    }

    const mark = await world.receiptValueUsd(this.position.receiptId, day);
    this.ledger.push({ day, event: "mark", usd: mark });

    if (day - this.position.day >= this.holdDays) {
      const gotUsd = await world.basketRedeem(
        this.position.receiptId, this.account, { tag: `saver:redeem:d${day}` });
      this.profitUsd += gotUsd - this.position.costUsd;
      this.dollarDays += this.position.costUsd
                       * BigInt(day - this.position.day);
      this.ledger.push({ day, event: "redeem", usd: gotUsd });
      this.position = null;    // holds the redeemed assets; one cycle v1
    }
  }
}
