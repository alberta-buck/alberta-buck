"""Closed-system comparison of gross demurrage and funded Jubilee returns.

The 2% return case is deliberately explicit: the holder pays the return to
the invested fund. It is an idealized 2% annualized return on the entire fund,
not evidence that a basket can achieve it. No further BUCK are issued.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json

from alberta_buck.sim.demurrage_reference import DemurrageLedger


@dataclass(frozen=True)
class Comparison:
    years: float
    supply: float
    gross_demurrage: float
    no_return_holder: float
    no_return_fund: float
    no_return_fund_nominal: float
    return_case_holder: float
    return_case_fund: float
    return_transferred: float


def compare_fund_returns(principal: float = 100, steps_per_year: int = 365) -> list[Comparison]:
    if principal <= 0 or steps_per_year <= 0:
        raise ValueError("principal and steps per year must be positive")
    plain = DemurrageLedger()
    earning = DemurrageLedger()
    for ledger in (plain, earning):
        ledger.issue_unspent("facility", principal, "owner")
        ledger.spend_unspent("owner", "merchant", principal)

    rows: list[Comparison] = []
    paid = 0.0

    def snapshot(years: float) -> None:
        rows.append(Comparison(
            years=years, supply=plain.issued, gross_demurrage=plain.gross_demurrage,
            no_return_holder=plain.balance_of("merchant"), no_return_fund=plain.jubilee_assets,
            no_return_fund_nominal=plain.jubilee_nominal,
            return_case_holder=earning.balance_of("merchant"), return_case_fund=earning.jubilee_assets,
            return_transferred=paid,
        ))

    snapshot(0)
    for step in range(1, 50 * steps_per_year + 1):
        dt = 1 / steps_per_year
        plain.advance(dt)
        earning.advance(dt)
        if earning.jubilee:
            earning.deploy(earning.jubilee)
        # The continuously compounded fee on the outside holding plus a
        # 2%-annualized return on the fund gives d(fund)/dt = 2% * supply.
        # A small end-of-step correction makes the sampled path exact. The
        # return comes from a real existing BUCK holder, never from minting.
        target = principal * step / (50 * steps_per_year)
        transfer = min(max(0.0, target - earning.jubilee_assets), earning.balance_of("merchant"))
        if transfer:
            earning.strategy_return("merchant", transfer)
            paid += transfer
        if step % (10 * steps_per_year) == 0 or step == 25 * steps_per_year:
            snapshot(step / steps_per_year)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable rows")
    args = parser.parse_args()
    rows = compare_fund_returns()
    if args.json:
        print(json.dumps([asdict(row) for row in rows], indent=2))
        return
    print("Years  Gross fees   No-return holder   No-return fund   2%-return holder   2%-return fund   Return paid")
    for row in rows:
        print(f"{row.years:5.0f}  {row.gross_demurrage:10.2f}  {row.no_return_holder:17.2f}"
              f"  {row.no_return_fund:15.2f}  {row.return_case_holder:17.2f}"
              f"  {row.return_case_fund:14.2f}  {row.return_transferred:11.2f}")
    print("\nThe return is transferred from the merchant's existing BUCK; supply stays 100.")
    print("The return case assumes the entire fund earns 2% annualized and is fully investable.")


if __name__ == "__main__":
    main()
