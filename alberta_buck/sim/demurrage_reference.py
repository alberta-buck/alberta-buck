"""Reference model for net-balance demurrage and issuance-based relief.

Time is measured in years and values in BUCK. Between events, ordinary account
balances decay continuously at a nominal annual rate; their fees transfer to
Jubilee. Jubilee also pays demurrage to itself, which is counted as gross
activity but has no effect on its balance. Credit positions accumulate linear
BUCK-years of outstanding issuance. This float-based, event-driven model is for
financial specification and tests, not production fixed-point contract math.

A simplified strategy is backed 1:1 by BUCK, can be liquidated at par, and
realizes returns only as explicit transfers from another account. Non-BUCK
payments, changing marks, liquidity risk and real basket operations are outside
this closed BUCK ledger.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
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

    @property
    def issued(self) -> float:
        return sum(p.principal for p in self.positions.values())

    @property
    def jubilee(self) -> float:
        return self.accounts[JUBILEE]

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

    def transfer(self, sender: str, recipient: str, amount: float) -> None:
        """Transfer already-issued BUCK, including an actual fund acquisition."""
        amount = _nonnegative(amount, "amount")
        if not sender or not recipient:
            raise ValueError("sender and recipient must be nonempty")
        available = self.accounts.get(sender, 0.0)
        if amount > available:
            raise InsufficientFunds(f"{sender} has {available:g}, needs {amount:g}")
        if sender == recipient:
            return
        self.accounts[sender] = available - amount
        self.accounts[recipient] = self.accounts.get(recipient, 0.0) + amount

    def deploy(self, amount: float) -> None:
        """Place cash into the idealized 1:1 BUCK strategy."""
        self.transfer(JUBILEE, JUBILEE_INVESTED, amount)

    def strategy_return(self, payer: str, amount: float) -> None:
        """Realize a BUCK strategy return sourced from a non-fund account."""
        if payer in FUND_ACCOUNTS:
            raise ValueError("a strategy return must have an external source")
        self.transfer(payer, JUBILEE_INVESTED, amount)

    def advance(self, years: float) -> None:
        """Advance an event-free interval using the exact continuous solution."""
        years = _nonnegative(years, "years")
        if years == 0.0:
            return
        gross = self.issued * self.rate * years
        for p in self.positions.values():
            p.buck_years += p.principal * years
        loss_fraction = -math.expm1(-self.rate * years)
        remaining_fraction = math.exp(-self.rate * years)
        incoming = 0.0
        for name, held in self.accounts.items():
            if name not in FUND_ACCOUNTS:
                fee = held * loss_fraction
                # Compute the small remainder directly to avoid cancellation
                # after very long idle periods; expm1 keeps tiny fees accurate.
                self.accounts[name] = held * remaining_fraction
                incoming += fee
        self.accounts[JUBILEE] += incoming
        self.gross_demurrage += gross
        self.from_holders += incoming
        # Integral(rate * fund balance dt): the fund pays this to itself.
        self.recycled_by_jubilee += gross - incoming
        self.time += years

    def quote_retirement(self, position: str, amount: float) -> RetirementQuote:
        """Quote a proportional reduction at the position's average age."""
        amount = _positive(amount, "amount")
        p = self.positions[position]
        if amount > p.principal:
            raise ValueError("retirement exceeds outstanding issuance")
        # Relief cannot exceed the principal being retired, even after 50 years.
        relief = min(amount, self.rate * p.buck_years * amount / p.principal)
        return RetirementQuote(
            principal=amount,
            relief=relief,
            account_payment=amount - relief,
            fund_available=self.jubilee_assets,
            fund_cash=self.jubilee,
            fund_invested=self.accounts[JUBILEE_INVESTED],
            fund_shortfall=max(0.0, relief - self.jubilee_assets),
        )

    def retire(self, position: str, amount: float, payer: str) -> RetirementQuote:
        """Extinguish issuance using precisely the quoted account and fund shares.

        An underfunded payout fails atomically; no artificial BUCK is advanced to
        the fund and no extra debt is silently imposed on the payer.
        """
        if payer in FUND_ACCOUNTS:
            raise ValueError("payer must be distinct from Jubilee")
        quote = self.quote_retirement(position, amount)
        if quote.fund_shortfall > 0.0:
            raise InsufficientFunds(f"Jubilee lacks {quote.fund_shortfall:g} BUCK")
        available = self.accounts.get(payer, 0.0)
        if quote.account_payment > available:
            raise InsufficientFunds(
                f"{payer} lacks {quote.account_payment - available:g} BUCK"
            )
        p = self.positions[position]
        remaining_fraction = 1.0 - amount / p.principal
        if quote.relief > self.jubilee:
            # The toy strategy redeems at par with no delay or price impact.
            self.transfer(JUBILEE_INVESTED, JUBILEE, quote.relief - self.jubilee)
        self.accounts[payer] = available - quote.account_payment
        self.accounts[JUBILEE] -= quote.relief
        p.principal -= amount
        p.buck_years *= remaining_fraction
        return quote


def _nonnegative(value: float, label: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return value


def _positive(value: float, label: str) -> float:
    value = _nonnegative(value, label)
    if value == 0:
        raise ValueError(f"{label} must be positive")
    return value


def snapshot(ledger: DemurrageLedger, position: str, holder: str) -> dict:
    p = ledger.positions[position]
    quote = ledger.quote_retirement(position, p.principal)
    return {
        "years": ledger.time,
        "issued": ledger.issued,
        "holder": ledger.accounts[holder],
        "jubilee": ledger.jubilee_assets,
        "jubilee_cash": ledger.jubilee,
        "jubilee_invested": ledger.accounts[JUBILEE_INVESTED],
        "gross_demurrage": ledger.gross_demurrage,
        "received_from_holders": ledger.from_holders,
        "fund_self_demurrage": ledger.recycled_by_jubilee,
        "aggregate_relief": ledger.aggregate_relief,
        "aggregate_fund_shortfall": max(0.0, ledger.aggregate_relief - ledger.jubilee_assets),
        "retirement": asdict(quote),
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
    acquired = None
    if args.acquire_all:
        acquired = ledger.accounts["holder"]
        ledger.transfer("holder", JUBILEE, acquired)
    result = {"rate": ledger.rate, "snapshots": rows, "acquired_at_end": acquired,
              "strategy_return": args.strategy_return,
              "final": snapshot(ledger, "credit", "holder")}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Continuous nominal annual rate: {:.2%}".format(ledger.rate))
        print(" year     holder    Jubilee  gross fees  self-paid    relief  shortfall")
        for row in rows:
            q = row["retirement"]
            print("{:5.1f} {:10.6f} {:10.6f} {:11.6f} {:10.6f} {:9.6f} {:10.6f}".format(
                row["years"], row["holder"], row["jubilee"], row["gross_demurrage"],
                row["fund_self_demurrage"], q["relief"], q["fund_shortfall"]))
        if args.strategy_return:
            print(f"\nStrategy return: {args.strategy_return:.6f} BUCK transferred from the holder.")
        if acquired is not None:
            print(f"\nJubilee acquires {acquired:.6f} BUCK from the holder; "
                  "the non-BUCK consideration is not modeled.")
        if acquired is not None or args.strategy_return or args.deploy_fund:
            print(f"Fund cash: {ledger.jubilee:.6f}; deployed: {ledger.accounts[JUBILEE_INVESTED]:.6f}; "
                  f"retirement shortfall: {result['final']['retirement']['fund_shortfall']:.6f}")
        if args.deploy_fund:
            print("The strategy redeems at par; prices, delays, losses and costs are not modeled.")
        print("Gross fees include fund-to-itself payments; they are not all new fund assets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
