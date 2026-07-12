// The FARMER: a real operation on the audited debtor chassis.  $3M of
// insured assets (land + grain inventory) pledged behind premium-bearing
// BuckCredit; a $750k mortgage and a $250k revolving operating line
// (drawn for spring inputs, repaid from the harvest); lumpy revenue --
// two harvest cheques a year against year-round expenses.  BUCK credit
// deploys against the DEAREST debt first (the operating line), the
// funding gate is saved for in advance, the unwind buys only below USD
// par, the liability melts at the chain's own Jubilee quote, and once
// the debts are gone the surplus invests in the BuckBasket.
//
// The counterfactual ledger runs the SAME farm without BUCK: the line
// and the mortgage serviced from cash alone.  Net worths:
//   actual: assets + banked + basketMark - mortgage - opLine - drawn + jub
//   hypo:   assets + hypoBanked - hypoMortgage - hypoOp

const BP = 10_000n;
const MONTH = 30;
const E18 = 10n ** 18n;
const E6 = 10n ** 6n;
const min = (a, b) => (a < b ? a : b);

export class Farmer {
  constructor({ account, land = 2_500_000n * E6, grain = 500_000n * E6,
                mortgage = 750_000n * E6, mortgageBp = 550n,
                payment = 4_600n * E6, opLimit = 250_000n * E6, opBp = 850n,
                expenses = 30_000n * E6, springExtra = 40_000n * E6,
                harvest = 330_000n * E6, premiumBp = 100n, aggrBp = 550n,
                saveRate = 50n, retireDiscBp = 200n,
                cashBuffer = 25_000n * E6, investIdx = 0,
                startCash = 200_000n * E6 }) {
    Object.assign(this, { account, land, grain, mortgage, mortgageBp,
                          payment, opLimit, opBp, expenses, springExtra,
                          harvest, premiumBp, aggrBp, saveRate,
                          retireDiscBp, cashBuffer, investIdx, startCash });
    this.assets = land + grain;
    this.mortgageOwing = mortgage;
    this.opDrawn = 0n;
    // Day 0 is post-harvest: startCash carries the winter-to-spring
    // trough so the operating line's cap never silently binds.
    this.usdc = startCash;
    this.hypoMortgage = mortgage;   // the no-BUCK twin runs the same farm
    this.hypoOp = 0n;
    this.hypoBanked = startCash;
    this.premiumPaid = 0n;
    this.tradeLoss = 0n;
    this.throttled = 0;
    this.jub = 0n;
    this.invested = null;           // open basket position {receiptId, cost}
    this.ledger = [];
  }

  async setup(world) {
    const half = this.land / 2n;    // two land parcels + the grain bins
    await world.pledgeInsured(this.account,
      [half, this.land - half, this.grain], this.premiumBp,
      { tag: "farmer:pledge" });
    await world.fiatIn(this.account, this.startCash,
                       { tag: "farmer:start-cash" });
  }

  #cashMonth(month) {
    // One month of farm cash flow on a ledger: returns [revenue, spend].
    const rev = (month === 8 || month === 9) ? this.harvest : 0n;
    const out = this.expenses
              + (month >= 4 && month <= 6 ? this.springExtra : 0n);
    return [rev, out];
  }

  #serviceDebts(cash, m) {
    // Operating line bridges the shortfall; surplus repays it FIRST
    // (dearest debt), then the fixed mortgage payment.  Pure ledger
    // arithmetic shared verbatim by both the real and hypo twins.
    m.op += (m.op * this.opBp) / BP / 12n;
    if (cash < m.spend) {
      const need = min(m.spend - cash, this.opLimit - m.op);
      m.op += need;
      cash += need;
    }
    cash -= min(cash, m.spend);
    if (m.mort > 0n) {
      const interest = (m.mort * this.mortgageBp) / BP / 12n;
      const pay = min(cash, min(this.payment, m.mort + interest));
      m.mort += interest - pay;
      cash -= pay;
    }
    const repay = min(cash > this.cashBuffer ? cash - this.cashBuffer : 0n,
                      m.op);
    m.op -= repay;
    return cash - repay;
  }

  async act(world, day, tick) {
    if (tick !== 0 || day % MONTH !== 0) return;
    const month = (day / MONTH) % 12;
    const [rev, spend] = this.#cashMonth(month);
    if (rev > 0n) await world.fiatIn(this.account, rev,
                                     { tag: `farmer:harvest:d${day}` });
    const real = { op: this.opDrawn, mort: this.mortgageOwing, spend };
    this.usdc = this.#serviceDebts(this.usdc + rev, real);
    this.opDrawn = real.op;
    this.mortgageOwing = real.mort;
    const hypo = { op: this.hypoOp, mort: this.hypoMortgage, spend };
    this.hypoBanked = this.#serviceDebts(this.hypoBanked + rev, hypo);
    this.hypoOp = hypo.op;
    this.hypoMortgage = hypo.mort;

    const cs = await world.creditState(this.account);
    const bvib = await world.session.call(world.basket, "basketValueInBuck");
    this.jub = min(cs.jub, cs.drawn);
    const spare = this.usdc > this.cashBuffer
      ? this.usdc - this.cashBuffer : 0n;
    const debt = this.opDrawn + this.mortgageOwing;
    const tranche = min(min(this.payment * 24n, cs.headroom), debt);
    const deployOk = bvib <= (E18 * (BP + this.aggrBp)) / BP;

    if (tranche >= E6 && debt > E6 && deployOk) {
      const { shortfall } = await world.gateShortfall(this.account, tranche);
      const save = min(shortfall, (spare * this.saveRate) / 100n);
      if (save >= E6) {
        const got = await world.buyBuck(save, this.account,
                                        { tag: `farmer:save:d${day}` });
        this.usdc -= save;
        this.tradeLoss += save - got;
      }
      const m = await world.mintTranche(this.account,
        min(tranche, cs.unactivated), { tag: `farmer:mint:d${day}` });
      if (!m.ok) {
        this.throttled += 1;
      } else {
        this.premiumPaid += m.premium;
        const { sold, got } = await world.sellBuckCapped(tranche,
          this.account, { tag: `farmer:draw:d${day}` });
        this.tradeLoss += sold - got;
        const toOp = min(got, this.opDrawn);
        this.opDrawn -= toOp;
        this.mortgageOwing -= min(got - toOp, this.mortgageOwing);
      }
    } else if (debt <= E6 && cs.drawn > 0n && spare >= E6) {
      const { spot, capBuck } = await world.unwindBite();
      if (spot <= (E6 * (BP - this.retireDiscBp)) / BP && capBuck >= E6) {
        const spend2 = min(spare, (min(cs.drawn, capBuck) * spot) / E6);
        if (spend2 >= E6) {
          const got = await world.buyBuck(spend2, this.account,
                                          { tag: `farmer:unwind:d${day}` });
          this.usdc -= spend2;
          this.tradeLoss += spend2 - got;
        }
      }
    } else if (debt <= E6 && !this.invested && spare >= 50_000n * E6) {
      // Debt-free: the surplus becomes a BuckBasket position.
      const { out } = await world.buyTokenBest(this.investIdx, spare,
        this.account, { tag: `farmer:acquire:d${day}` });
      const { receiptId } = await world.basketDepositAs(this.investIdx, out,
        this.account, { tag: `farmer:invest:d${day}` });
      this.invested = { receiptId, cost: spare };
      this.usdc -= spare;
    }

    const mark = this.invested
      ? await world.receiptValueUsd(this.invested.receiptId, day) : 0n;
    const post = await world.creditState(this.account);
    this.ledger.push({
      day, drawn: post.drawn, jub: this.jub, banked: this.usdc,
      opDrawn: this.opDrawn, mortgageOwing: this.mortgageOwing,
      basketMark: mark, premiumPaid: this.premiumPaid,
      tradeLoss: this.tradeLoss, hypoMortgage: this.hypoMortgage,
      hypoOp: this.hypoOp, hypoBanked: this.hypoBanked,
      nw: this.assets + this.usdc + mark + this.jub
        - this.mortgageOwing - this.opDrawn - post.drawn,
      hypoNw: this.assets + this.hypoBanked
        - this.hypoMortgage - this.hypoOp,
    });
  }
}
