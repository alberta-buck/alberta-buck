"""Reference model for net-balance demurrage and issuance-based relief.

Time is measured in years and values in BUCK. Between events, ordinary account
balances decay continuously at a nominal annual rate; their fees transfer to
Jubilee. Jubilee also pays demurrage to itself, which is counted as gross
activity but has no effect on its balance. Credit positions accumulate linear
BUCK-years of outstanding issuance. This float-based, event-driven model is for
financial specification and tests, not production fixed-point contract math.

Jubilee's liquid BUCK are Carrying: the ledger tracks their net value and
outstanding fee separately. A payment of net relief removes a proportional
amount of gross BUCK and its fee, so the credit receives the requested full
value. Fund self-demurrage is already credited back to the fund as it accrues;
it must not be credited again when the carried fee is realized.

A simplified BUCK-only strategy can be liquidated at its current mark and
realizes returns only as explicit transfers from another account. Its BUCK
also pay demurrage into fund cash. Non-BUCK payments, price impact, liquidity
risk and real basket operations are outside this closed BUCK ledger.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict, dataclass, replace
import json
import math

JUBILEE = "Jubilee"
JUBILEE_INVESTED = "Jubilee invested"
FUND_ACCOUNTS = (JUBILEE, JUBILEE_INVESTED)


@dataclass
class Position:
    """Outstanding issued principal and its integral over time in BUCK-years."""

    principal: float = 0.0
    buck_years: float = 0.0


@dataclass(frozen=True)
class RetirementQuote:
    principal: float
    relief: float
    account_payment: float
    fund_available: float
    fund_cash: float
    fund_invested: float
    fund_shortfall: float
    fund_nominal_required: float | None
    fund_carried_fee: float | None


@dataclass(frozen=True)
class AvailableRetirement:
    """Relief determined after the modeled proportional strategy redemption."""

    principal: float
    baseline_relief: float
    benchmark: float
    redeemed_fraction: float
    redeemed_units: float
    redemption_proceeds: float
    fund_net_after_redemption: float
    reserve_target: float
    rate_multiplier: float | None
    relief: float
    account_payment: float
    fund_nominal_required: float
    fund_carried_fee: float


class InsufficientFunds(ValueError):
    """The proposed retirement cannot be fully funded by its designated payers."""


class DemurrageLedger:
    """Closed BUCK ledger with a linear relief claim per credit position.

    The model applies average-age, proportional retirement within a position.
    Separate positions keep separate histories. The rate is a continuous nominal
    annual rate, so an idle account with 100 BUCK holds 100 * exp(-0.02*t).
    Positive BUCK is created only by ``issue`` and removed only by ``retire``.
    """

    def __init__(self, rate: float = 0.02):
        self.rate = _nonnegative(rate, "rate")
        self.time = 0.0
        self.accounts: dict[str, float] = {JUBILEE: 0.0, JUBILEE_INVESTED: 0.0}
        self.positions: dict[str, Position] = {}
        self.gross_demurrage = 0.0
        self.from_holders = 0.0
        self.recycled_by_jubilee = 0.0
        self.jubilee_carried_fee = 0.0
        self.jubilee_fee_created = 0.0
        self.jubilee_fee_realized = 0.0
        self.jubilee_nominal_sent = 0.0
        # Full-value cash flows. Separate lifetime counters can each remain
        # non-negative even when the net income or net invested amount is
        # negative after investment gains and relief payments.
        self.fund_demurrage_received = 0.0
        self.fund_relief_paid = 0.0
        self.fund_invested = 0.0
        self.fund_withdrawn = 0.0
        self.fund_other_in = 0.0
        self.fund_other_out = 0.0
        # The fund owns all shares in this idealized strategy. Their BUCK mark
        # is accounts[JUBILEE_INVESTED]; units can remain even at a zero mark.
        self.strategy_units = 0.0
        self.strategy_open_basis = 0.0
        self.strategy_realized_gain = 0.0
        self.strategy_realized_loss = 0.0
        self.strategy_demurrage_received = 0.0

    @property
    def issued(self) -> float:
        return sum(p.principal for p in self.positions.values())

    @property
    def jubilee(self) -> float:
        return self.accounts[JUBILEE]

    @property
    def jubilee_nominal(self) -> float:
        """Raw Carrying amount, including the fund's accrued fee history."""
        return self.jubilee + self.jubilee_carried_fee

    @property
    def jubilee_assets(self) -> float:
        """Cash plus the modeled strategy's immediately redeemable BUCK."""
        return self.jubilee + self.accounts[JUBILEE_INVESTED]

    @property
    def aggregate_relief(self) -> float:
        """Total capped claims if all credit positions retire now."""
        return sum(min(p.principal, self.rate * p.buck_years)
                   for p in self.positions.values())

    def issue(self, position: str, amount: float, recipient: str) -> None:
        """Create BUCK against this position; undrawn credit is not issuance."""
        amount = _positive(amount, "amount")
        if not position or not recipient:
            raise ValueError("position and recipient must be nonempty")
        p = self.positions.setdefault(position, Position())
        p.principal += amount
        self.accounts[recipient] = self.accounts.get(recipient, 0.0) + amount
        if recipient == JUBILEE:
            self.fund_other_in += amount

    def transfer(self, sender: str, recipient: str, amount: float) -> None:
        """Transfer a full-value amount, including an actual fund acquisition.

        A transfer from Jubilee consumes enough Carrying gross BUCK that the
        recipient gets ``amount`` after the proportionally carried fee.
        Incoming transfers and the toy strategy pay full-value fresh BUCK.
        """
        self._transfer(sender, recipient, amount, relief=False)

    def _transfer(self, sender: str, recipient: str, amount: float, *, relief: bool) -> None:
        amount = _nonnegative(amount, "amount")
        if not sender or not recipient:
            raise ValueError("sender and recipient must be nonempty")
        available = self.accounts.get(sender, 0.0)
        if amount > available:
            raise InsufficientFunds(f"{sender} has {available:g}, needs {amount:g}")
        if sender == recipient:
            return
        if sender == JUBILEE and amount:
            if amount == available:
                carried = self.jubilee_carried_fee
            else:
                carried = amount * self.jubilee_carried_fee / available
            self.jubilee_carried_fee -= carried
            self.jubilee_fee_realized += carried
            self.jubilee_nominal_sent += amount + carried
        self.accounts[sender] = available - amount
        self.accounts[recipient] = self.accounts.get(recipient, 0.0) + amount
        if sender == JUBILEE:
            if recipient == JUBILEE_INVESTED:
                self.fund_invested += amount
            elif relief:
                self.fund_relief_paid += amount
            else:
                self.fund_other_out += amount
        if recipient == JUBILEE:
            if sender == JUBILEE_INVESTED:
                self.fund_withdrawn += amount
            else:
                self.fund_other_in += amount

    def deploy(self, amount: float) -> None:
        """Place cash into the idealized 1:1 BUCK strategy."""
        amount = _nonnegative(amount, "amount")
        mark = self.accounts[JUBILEE_INVESTED]
        new_units = (amount * self.strategy_units / mark
                     if self.strategy_units and mark else amount)
        self.transfer(JUBILEE, JUBILEE_INVESTED, amount)
        self.strategy_units += new_units
        self.strategy_open_basis += amount

    def strategy_return(self, payer: str, amount: float) -> None:
        """Realize a BUCK strategy return sourced from a non-fund account."""
        if payer in FUND_ACCOUNTS:
            raise ValueError("a strategy return must have an external source")
        if amount and self.strategy_units == 0:
            raise ValueError("a strategy return requires an open position")
        self.transfer(payer, JUBILEE_INVESTED, amount)

    def strategy_loss(self, recipient: str, amount: float) -> None:
        """Model a strategy loss as BUCK value transferred out of the fund."""
        if recipient in FUND_ACCOUNTS:
            raise ValueError("a strategy loss must leave the fund")
        self.transfer(JUBILEE_INVESTED, recipient, amount)

    def redeem_strategy(self, fraction: float) -> tuple[float, float]:
        """Redeem that fraction of owned units, including a zero-value sale."""
        fraction = _nonnegative(fraction, "fraction")
        if fraction > 1:
            raise ValueError("redemption fraction cannot exceed one")
        units = self.strategy_units * fraction
        proceeds = self.accounts[JUBILEE_INVESTED] * fraction
        basis = self.strategy_open_basis * fraction
        self.transfer(JUBILEE_INVESTED, JUBILEE, proceeds)
        self.strategy_units -= units
        self.strategy_open_basis -= basis
        self.strategy_realized_gain += max(0.0, proceeds - basis)
        self.strategy_realized_loss += max(0.0, basis - proceeds)
        return units, proceeds

    def advance(self, years: float) -> None:
        """Advance an event-free interval using the exact continuous solution."""
        years = _nonnegative(years, "years")
        if years == 0.0:
            return
        gross = self.issued * self.rate * years
        for p in self.positions.values():
            p.buck_years += p.principal * years
        z = self.rate * years
        loss_fraction = -math.expm1(-z)
        remaining_fraction = math.exp(-z)
        holder_start = sum(value for name, value in self.accounts.items()
                           if name not in FUND_ACCOUNTS)
        strategy_start = self.accounts[JUBILEE_INVESTED]
        # Holder and strategy fees arrive continuously. Self-demurrage is
        # charged on evolving net fund cash, not previously paid fees.
        # z - (1 - exp(-z)) needs a series when z is very small.
        curvature = (z * z * (0.5 - z / 6 + z * z / 24 - z * z * z / 120)
                     if z < 1e-4 else z - loss_fraction)
        self_fee = self.jubilee * z + (holder_start + strategy_start) * curvature
        incoming = 0.0
        for name, held in self.accounts.items():
            if name not in FUND_ACCOUNTS:
                fee = held * loss_fraction
                # Compute the small remainder directly to avoid cancellation
                # after very long idle periods; expm1 keeps tiny fees accurate.
                self.accounts[name] = held * remaining_fraction
                incoming += fee
        strategy_fee = strategy_start * loss_fraction
        self.accounts[JUBILEE_INVESTED] = strategy_start * remaining_fraction
        self.accounts[JUBILEE] += incoming + strategy_fee
        self.fund_demurrage_received += incoming + strategy_fee
        self.strategy_demurrage_received += strategy_fee
        self.jubilee_carried_fee += self_fee
        self.jubilee_fee_created += self_fee
        self.gross_demurrage += gross
        self.from_holders += incoming
        # Integral(rate * fund balance dt): the fund pays this to itself.
        self.recycled_by_jubilee += gross - incoming
        self.time += years

    def quote_retirement(self, position: str, amount: float) -> RetirementQuote:
        """Quote the fixed 2% baseline, for comparison with available relief."""
        amount = _positive(amount, "amount")
        p = self.positions[position]
        if amount > p.principal:
            raise ValueError("retirement exceeds outstanding issuance")
        # Relief cannot exceed the principal being retired, even after 50 years.
        relief = min(amount, self.rate * p.buck_years * amount / p.principal)
        fund_shortfall = max(0.0, relief - self.jubilee_assets)
        gross = None
        if fund_shortfall == 0:
            # If needed, the idealized strategy returns fresh full-value BUCK
            # before the carrying transfer, diluting the existing fee basis.
            net_at_payment = max(self.jubilee, relief)
            gross = 0.0 if relief == 0 else relief * (
                net_at_payment + self.jubilee_carried_fee
            ) / net_at_payment
        return RetirementQuote(
            principal=amount,
            relief=relief,
            account_payment=amount - relief,
            fund_available=self.jubilee_assets,
            fund_cash=self.jubilee,
            fund_invested=self.accounts[JUBILEE_INVESTED],
            fund_shortfall=fund_shortfall,
            fund_nominal_required=gross,
            fund_carried_fee=None if gross is None else gross - relief,
        )

    def retire(self, position: str, amount: float, payer: str) -> RetirementQuote:
        """Retire using the fixed 2% benchmark; a diagnostic comparison only.

        An underfunded payout fails atomically; no artificial BUCK is advanced to
        the fund and no extra debt is silently imposed on the payer.
        """
        if payer in FUND_ACCOUNTS:
            raise ValueError("payer must be distinct from Jubilee")
        quote = self.quote_retirement(position, amount)
        if quote.fund_shortfall > 0.0:
            raise InsufficientFunds(f"Jubilee lacks {quote.fund_shortfall:g} BUCK")
        available = self.accounts.get(payer, 0.0)
        if _exceeds_float(quote.account_payment, available):
            raise InsufficientFunds(
                f"{payer} lacks {quote.account_payment - available:g} BUCK"
            )
        p = self.positions[position]
        remaining_fraction = 1.0 - amount / p.principal
        if quote.relief > self.jubilee:
            # The toy strategy redeems at par with no delay or price impact.
            fraction = (quote.relief - self.jubilee) / self.accounts[JUBILEE_INVESTED]
            self.redeem_strategy(fraction)
        # Deliver the full net relief using the Carrying path, then extinguish
        # the credit using the account payment plus that exact net relief.
        self._transfer(JUBILEE, payer, quote.relief, relief=True)
        self.accounts[payer] = max(0.0, self.accounts[payer] - quote.principal)
        p.principal -= amount
        p.buck_years *= remaining_fraction
        return quote

    def quote_available_retirement(
        self, position: str, amount: float, redeemed_fraction: float | None = None,
        reserve_fraction: float = 0.1,
    ) -> AvailableRetirement:
        """Preview relief from cash after redeeming the specified LP fraction.

        The reserve signal and proportional redemption policy are experimental:
        by default the fund redeems requested target relief divided by the
        total outstanding unadjusted relief benchmark. The caller may supply a
        different fraction to compare alternatives. The signal is post-sale net
        cash / (reserve_fraction * benchmark).
        """
        if redeemed_fraction is None:
            redeemed_fraction = self.redemption_fraction(position, amount, "relief")
        redeemed_fraction = _nonnegative(redeemed_fraction, "redeemed_fraction")
        if redeemed_fraction > 1:
            raise ValueError("redemption fraction cannot exceed one")
        reserve_fraction = _positive(reserve_fraction, "reserve_fraction")
        if reserve_fraction > 1:
            raise ValueError("reserve fraction cannot exceed one")
        baseline = self.quote_retirement(position, amount).relief
        benchmark = self.aggregate_relief
        proceeds = self.accounts[JUBILEE_INVESTED] * redeemed_fraction
        cash = self.jubilee + proceeds
        target = reserve_fraction * benchmark
        multiplier = cash / target if target > 0 else None
        relief = min(amount, baseline * multiplier, cash) if multiplier is not None else 0.0
        nominal = relief * (cash + self.jubilee_carried_fee) / cash if relief else 0.0
        return AvailableRetirement(
            principal=amount, baseline_relief=baseline, benchmark=benchmark,
            redeemed_fraction=redeemed_fraction,
            redeemed_units=self.strategy_units * redeemed_fraction,
            redemption_proceeds=proceeds, fund_net_after_redemption=cash,
            reserve_target=target, rate_multiplier=multiplier,
            relief=relief, account_payment=amount - relief,
            fund_nominal_required=nominal, fund_carried_fee=nominal - relief,
        )

    def redemption_fraction(self, position: str, amount: float, basis: str) -> float:
        """Compare two candidate proportional-redemption bases."""
        baseline = self.quote_retirement(position, amount).relief
        if basis == "issuance":
            return min(1.0, amount / self.issued)
        if basis == "relief":
            return min(1.0, baseline / self.aggregate_relief) if self.aggregate_relief else 0.0
        raise ValueError("redemption basis must be issuance or relief")

    def retire_available(
        self, position: str, amount: float, payer: str, redeemed_fraction: float | None = None,
        reserve_fraction: float = 0.1,
    ) -> AvailableRetirement:
        """Redeem first, then pay relief bounded by actual full-value cash.

        Relief is calculated from the actual modeled proceeds after redemption.
        Failure to pay the debtor's share restores the entire reference ledger;
        a zero-value redemption never causes a fund shortfall error.
        """
        if payer in FUND_ACCOUNTS:
            raise ValueError("payer must be distinct from Jubilee")
        if redeemed_fraction is None:
            redeemed_fraction = self.redemption_fraction(position, amount, "relief")
        redeemed_fraction = _nonnegative(redeemed_fraction, "redeemed_fraction")
        if redeemed_fraction > 1:
            raise ValueError("redemption fraction cannot exceed one")
        # The reference uses a snapshot to model transaction rollback. Actual
        # contract execution must enforce atomicity without copying the ledger.
        before = copy.deepcopy(self.__dict__)
        try:
            units, proceeds = self.redeem_strategy(redeemed_fraction)
            result = self.quote_available_retirement(position, amount, 0.0, reserve_fraction)
            result = replace(result, redeemed_fraction=redeemed_fraction,
                             redeemed_units=units, redemption_proceeds=proceeds)
            available = self.accounts.get(payer, 0.0)
            if _exceeds_float(result.account_payment, available):
                raise InsufficientFunds(f"{payer} lacks {result.account_payment - available:g} BUCK")
            p = self.positions[position]
            remaining_fraction = 1.0 - amount / p.principal
            self._transfer(JUBILEE, payer, result.relief, relief=True)
            self.accounts[payer] = max(0.0, self.accounts[payer] - amount)
            p.principal -= amount
            p.buck_years *= remaining_fraction
            return result
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(before)
            raise


def _nonnegative(value: float, label: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return value


def _exceeds_float(amount: float, available: float) -> bool:
    """Ignore sub-raw-unit float noise in this non-contract reference model."""
    return amount > available and not math.isclose(amount, available, rel_tol=1e-14, abs_tol=1e-12)


def _positive(value: float, label: str) -> float:
    value = _nonnegative(value, label)
    if value == 0:
        raise ValueError(f"{label} must be positive")
    return value


def carrying_gross_for_net_units(net: int, gross: int, fee: int) -> int:
    """Minimum raw-unit transfer for ``net`` with a pro-rata floored fee.

    This is a candidate integer convention: a gross transfer x carries
    floor(fee * x / gross) units of fee. The current Solidity contract instead
    apportions buck-seconds and then converts them to fees, so its rounding
    needs a separate derivation and cross-check before using this on chain.
    """
    if any(not isinstance(x, int) or isinstance(x, bool) for x in (net, gross, fee)):
        raise TypeError("net, gross and fee must be integer raw units")
    if net < 0 or gross < 0 or fee < 0 or fee > gross:
        raise ValueError("invalid Carrying balances")
    if net > gross - fee:
        raise InsufficientFunds("the Carrying balance lacks the requested net BUCK")
    if net == 0:
        return 0
    return (net - 1) * gross // (gross - fee) + 1


def snapshot(ledger: DemurrageLedger, position: str, holder: str) -> dict:
    p = ledger.positions[position]
    quote = ledger.quote_retirement(position, p.principal) if p.principal else None
    return {
        "years": ledger.time,
        "issued": ledger.issued,
        "holder": ledger.accounts[holder],
        "jubilee": ledger.jubilee_assets,
        "jubilee_cash": ledger.jubilee,
        "jubilee_cash_nominal": ledger.jubilee_nominal,
        "jubilee_cash_carried_fee": ledger.jubilee_carried_fee,
        "jubilee_fee_created": ledger.jubilee_fee_created,
        "jubilee_fee_realized": ledger.jubilee_fee_realized,
        "jubilee_nominal_sent": ledger.jubilee_nominal_sent,
        "jubilee_invested": ledger.accounts[JUBILEE_INVESTED],
        "strategy_units": ledger.strategy_units,
        "strategy_open_basis": ledger.strategy_open_basis,
        "strategy_realized_gain": ledger.strategy_realized_gain,
        "strategy_realized_loss": ledger.strategy_realized_loss,
        "strategy_demurrage_received": ledger.strategy_demurrage_received,
        "fund_cash_flows": {
            "demurrage_received": ledger.fund_demurrage_received,
            "relief_paid": ledger.fund_relief_paid,
            "invested": ledger.fund_invested,
            "withdrawn": ledger.fund_withdrawn,
            "other_in": ledger.fund_other_in,
            "other_out": ledger.fund_other_out,
        },
        "gross_demurrage": ledger.gross_demurrage,
        "received_from_holders": ledger.from_holders,
        "fund_self_demurrage": ledger.recycled_by_jubilee,
        "aggregate_relief": ledger.aggregate_relief,
        "aggregate_fund_shortfall": max(0.0, ledger.aggregate_relief - ledger.jubilee_assets),
        "retirement": asdict(quote) if quote is not None else None,
        "conservation_error": sum(ledger.accounts.values()) - ledger.issued,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=float, default=50, help="end of run (default: 50)")
    parser.add_argument("--issued", type=float, default=100, help="initial issuance (default: 100)")
    parser.add_argument("--rate", type=float, default=0.02, help="nominal annual rate (default: 0.02)")
    parser.add_argument("--acquire-all", action="store_true", help="transfer the remaining holder BUCK to Jubilee at the end; any non-BUCK consideration is outside this model")
    parser.add_argument("--deploy-fund", action="store_true", help="deploy fund cash to a 1:1 BUCK strategy at each checkpoint")
    parser.add_argument("--strategy-return", type=float, default=0, help="BUCK returned by the strategy at the end, sourced from the holder")
    parser.add_argument("--wipe-strategy", action="store_true", help="model a total strategy loss to the holder before retirement")
    parser.add_argument("--retire", type=float, default=0, help="credit principal to reduce after the final checkpoint")
    parser.add_argument("--redemption-basis", choices=("issuance", "relief"), default="relief", help="select the fraction's denominator (default: relief)")
    parser.add_argument("--redeem-fraction", type=float, help="explicit fraction of strategy units sold before retirement; overrides --redemption-basis")
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    args = parser.parse_args(argv)
    years = _nonnegative(args.years, "years")
    ledger = DemurrageLedger(args.rate)
    ledger.issue("credit", args.issued, "holder")
    checkpoints = sorted({t for t in (0.0, 1.0, 25.0, 49.0, 50.0, years) if t <= years})
    rows = []
    for t in checkpoints:
        ledger.advance(t - ledger.time)
        if args.deploy_fund:
            ledger.deploy(ledger.jubilee)
        rows.append(snapshot(ledger, "credit", "holder"))
    if args.strategy_return:
        ledger.strategy_return("holder", args.strategy_return)
    lost = None
    if args.wipe_strategy:
        lost = ledger.accounts[JUBILEE_INVESTED]
        ledger.strategy_loss("holder", lost)
    acquired = None
    if args.acquire_all:
        acquired = ledger.accounts["holder"]
        ledger.transfer("holder", JUBILEE, acquired)
    available_retirement = None
    if args.retire:
        fraction = (args.redeem_fraction if args.redeem_fraction is not None
                    else ledger.redemption_fraction("credit", args.retire, args.redemption_basis))
        available_retirement = ledger.retire_available("credit", args.retire, "holder", fraction)
    result = {"rate": ledger.rate, "snapshots": rows, "acquired_at_end": acquired,
              "strategy_return": args.strategy_return,
              "strategy_loss": lost,
              "redemption_basis": "explicit" if args.redeem_fraction is not None else args.redemption_basis,
              "available_retirement": asdict(available_retirement) if available_retirement else None,
              "final": snapshot(ledger, "credit", "holder")}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Continuous nominal annual rate: {:.2%}".format(ledger.rate))
        print(" year     holder    Jubilee  gross fees  self-paid    target  baseline gap")
        for row in rows:
            q = row["retirement"]
            print("{:5.1f} {:10.6f} {:10.6f} {:11.6f} {:10.6f} {:9.6f} {:10.6f}".format(
                row["years"], row["holder"], row["jubilee"], row["gross_demurrage"],
                row["fund_self_demurrage"], q["relief"], q["fund_shortfall"]))
        if args.strategy_return:
            print(f"\nStrategy return: {args.strategy_return:.6f} BUCK transferred from the holder.")
        if lost is not None:
            print(f"\nStrategy loss: {lost:.6f} BUCK transferred to the holder.")
        if acquired is not None:
            print(f"\nJubilee acquires {acquired:.6f} BUCK from the holder; "
                  "the non-BUCK consideration is not modeled.")
        if acquired is not None or args.strategy_return or args.deploy_fund:
            shortfall = result['final']['aggregate_fund_shortfall']
            print(f"Fund cash: {ledger.jubilee:.6f}; deployed: {ledger.accounts[JUBILEE_INVESTED]:.6f}; "
                  f"aggregate baseline shortfall: {shortfall:.6f}")
        if args.deploy_fund:
            print("The strategy redeems its current mark at par; price impact and delay are not modeled.")
        print(f"Jubilee cash: {ledger.jubilee_nominal:.6f} nominal, "
              f"{ledger.jubilee_carried_fee:.6f} carried fee, {ledger.jubilee:.6f} net.")
        quote = result["final"]["retirement"]
        if available_retirement is not None:
            print(f"Sold {available_retirement.redeemed_fraction:.2%} of strategy units for "
                  f"{available_retirement.redemption_proceeds:.6f} BUCK; "
                  f"relief {available_retirement.relief:.6f}, "
                  f"debtor payment {available_retirement.account_payment:.6f}.")
            print(f"To deliver that relief: {available_retirement.fund_nominal_required:.6f} nominal, "
                  f"{available_retirement.fund_carried_fee:.6f} carried fee.")
        elif quote is None:
            print("Credit fully retired.")
        elif quote["fund_nominal_required"] is not None:
            print(f"Net relief {quote['relief']:.6f} requires "
                  f"{quote['fund_nominal_required']:.6f} nominal BUCK after any strategy liquidation.")
        else:
            print("Full relief is unavailable even after liquidating the idealized strategy.")
        print("Gross fees include fund-to-itself payments; they are not all new fund assets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
