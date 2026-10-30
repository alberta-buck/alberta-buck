"""Reference model for net-balance demurrage and issuance-based relief.

Time is measured in years and values in BUCK. Between events, ordinary account
balances decay continuously at a nominal annual rate; their fees transfer to
Jubilee. Jubilee also pays demurrage to itself, which is counted as gross
activity but has no effect on its balance. Credit positions accumulate linear
BUCK-years of outstanding issuance. This float-based, event-driven model is for
financial specification and tests, not production fixed-point contract math.

Issued but unspent credit is tracked separately from ordinary BUCK holdings.
It is part of issued supply and pays demurrage even when the ordinary holding
is zero. Each credit position retains its own issued principal and age.

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
from fractions import Fraction
import json
import math

JUBILEE = "Jubilee"
JUBILEE_INVESTED = "Jubilee invested"
FUND_ACCOUNTS = (JUBILEE, JUBILEE_INVESTED)


@dataclass
class Position:
    """Outstanding principal, gross BUCK-years and capped claimable age."""

    principal: float = 0.0
    buck_years: float = 0.0
    relief_years: float = 0.0
    owner: str | None = None
    credit_limit: float | None = None
    premium_rate: float = 0.0
    insurer: str | None = None
    insurance_pool: str | None = None
    insurance_principal: float = 0.0


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
    insurance_pool: str | None
    insurance_refund: float
    insurance_nominal_required: float | None
    insurance_principal_released: float


@dataclass(frozen=True)
class AvailableRetirement:
    """Relief determined after the modeled proportional strategy redemption."""

    principal: float
    baseline_relief: float
    benchmark: float
    redeemed_fraction: float
    redeemed_units: float
    redemption_proceeds: float
    payout_policy: str
    fund_net_after_redemption: float
    reserve_target: float
    rate_multiplier: float | None
    relief: float
    account_payment: float
    fund_nominal_required: float
    fund_carried_fee: float
    insurance_pool: str | None
    insurance_refund: float
    insurance_nominal_required: float | None
    insurance_principal_released: float


@dataclass(frozen=True)
class MarkedReliefQuote:
    """Cash-only settlement quote based on the entire fund's modeled mark."""

    principal: float
    baseline_relief: float
    aggregate_benchmark: float
    cash_net: float
    investment_mark: float
    fund_mark: float
    benchmark_share: float
    multiplier: float | None
    effective_annual_rate: float | None
    relief: float
    account_payment: float
    liquid_limit: float
    exceeds_liquid_limit: bool


@dataclass(frozen=True)
class Renewal:
    """Atomic renewal of one owned credit position."""

    position: str
    baseline_relief: float
    relief: float
    old_principal: float
    additional_issuance: float
    new_principal: float
    new_limit: float
    new_premium_rate: float
    old_insurance_pool: str | None
    new_insurance_pool: str | None
    old_insurance_principal: float
    new_insurance_principal: float
    insurance_refunded: float
    insurance_principal_released: float
    insurance_funded: float
    redemption_proceeds: float


class InsufficientFunds(ValueError):
    """The proposed retirement cannot be fully funded by its designated payers."""


def ideal_crossheld_basket_equity(
    assets: int | float | Fraction,
    principal: int | float | Fraction,
    fund_cash: int | float | Fraction,
    fund_share: int | float | Fraction,
    claim_share: int | float | Fraction,
) -> tuple[Fraction, Fraction]:
    """Return the lowest and highest idealized basket-equity fixed points.

    The fund owns ``fund_share`` of this basket, and the basket's facility
    owns ``claim_share`` of the fund's claim benchmark. With frictionless,
    fully liquid sale at the same mark, the simultaneous equations are::

        relief = min(principal, claim_share * (fund_cash + fund_share * equity))
        equity = max(0, assets - principal + relief)

    An interval wider than zero exposes an indeterminate cross-holding. This
    diagnostic does not quote an executable sale, implement K, or guarantee
    that the actual Jubilee relief equals this ideal mark. Exact fractions
    keep the singular case distinct from an arbitrarily close approximation.
    """
    a, p, c, f, r = (value if isinstance(value, Fraction) else Fraction(str(value))
                     for value in (assets, principal, fund_cash, fund_share, claim_share))
    if min(a, p, c) < 0 or not 0 <= f <= 1 or not 0 <= r <= 1:
        raise ValueError("assets, principal and cash must be nonnegative; shares must be in [0, 1]")
    feedback = f * r
    numerator = a - p + r * c
    if feedback == 1:
        if numerator == 0:
            return Fraction(0), a
        result = a if numerator > 0 else Fraction(0)
    else:
        result = max(Fraction(0), min(a, numerator / (1 - feedback)))
    return result, result


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
        self.unspent: dict[str, float] = {}
        self.insurer_pools: dict[str, str] = {}
        self.insurance_carried_fee: dict[str, float] = {}
        self.insurance_nominal_sent: dict[str, float] = {}
        self.insurance_strategies: dict[str, str] = {}
        self.positions: dict[str, Position] = {}
        # A matured lien may be released before the fund can burn its BUCK.
        # The deferred principal is still issued supply and still pays
        # demurrage. Premium refunds remain owed by the original insurer pool.
        self.deferred_jubilee_principal = 0.0
        self.closed_positions: set[str] = set()
        self.pending_insurance_refunds: dict[str, tuple[str, str, float]] = {}
        self.gross_demurrage = 0.0
        self.from_holders = 0.0
        self.recycled_by_jubilee = 0.0
        self.jubilee_carried_fee = 0.0
        self.jubilee_fee_created = 0.0
        self.jubilee_fee_inherited = 0.0
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
        return self.deferred_jubilee_principal + sum(p.principal for p in self.positions.values())

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
        return self.deferred_jubilee_principal + sum(
            min(p.principal, self.rate * p.relief_years) for p in self.positions.values()
        )

    def issue(self, position: str, amount: float, recipient: str) -> None:
        """Create BUCK against a position and deliver it to a holder."""
        amount = _positive(amount, "amount")
        if not position or not recipient:
            raise ValueError("position and recipient must be nonempty")
        if position in self.closed_positions:
            raise ValueError("a closed facility cannot issue again")
        p = self.positions.setdefault(position, Position())
        if p.owner is not None:
            raise ValueError("use issue_unspent for an owned credit position")
        p.principal += amount
        self.accounts[recipient] = self.accounts.get(recipient, 0.0) + amount
        if recipient == JUBILEE:
            self.fund_other_in += amount

    def register_insurer_pool(self, insurer: str, pool: str) -> None:
        """Register a Carrying pool; active deposits keep their original pool."""
        if not insurer or not pool or pool in FUND_ACCOUNTS:
            raise ValueError("insurer and non-Jubilee pool must be specified")
        if any(other != insurer and registered == pool
               for other, registered in self.insurer_pools.items()):
            raise ValueError("each insurer must use its own pool")
        if pool in self.insurance_carried_fee and pool not in self.insurance_strategies:
            raise ValueError("an insurer investment cannot be another insurer's pool")
        old = self.insurer_pools.get(insurer)
        if old is not None and old != pool and any(
            p.insurer == insurer and p.insurance_principal > 0 for p in self.positions.values()
        ):
            raise ValueError("active insurance principal must stay with its original pool")
        if pool not in self.insurance_carried_fee:
            if (self.accounts.get(pool, 0.0) or self.unspent.get(pool, 0.0) or
                    any(p.owner == pool and p.principal for p in self.positions.values())):
                raise ValueError("register a Carrying pool before funding it or issuing credit")
            strategy = f"insurer-investment:{pool}"
            if strategy in self.accounts or strategy in self.insurance_carried_fee:
                raise ValueError("insurer investment account already exists")
            self.accounts[pool] = 0.0
            self.insurance_carried_fee[pool] = 0.0
            self.insurance_nominal_sent[pool] = 0.0
            self.accounts[strategy] = 0.0
            self.insurance_carried_fee[strategy] = 0.0
            self.insurance_nominal_sent[strategy] = 0.0
            self.insurance_strategies[pool] = strategy
        self.insurer_pools[insurer] = pool

    def invest_insurance(self, pool: str, nominal: float) -> None:
        """Move nominal Carrying BUCK into the insurer's own mock strategy."""
        self.transfer(pool, self.insurance_strategies[pool], nominal)

    def issue_unspent(
        self, position: str, amount: float, owner: str, *, credit_limit: float | None = None,
        premium_rate: float | None = None, insurer: str | None = None, premium_deposit: float = 0.0,
    ) -> None:
        """Issue into unspent credit and optionally fund its insurer's pool.

        The simplified model takes a quoted net deposit; it does not price the
        insurance policy or implement the contract's funding-factor gate.
        """
        amount = _positive(amount, "amount")
        premium_deposit = _nonnegative(premium_deposit, "premium_deposit")
        if credit_limit is not None:
            credit_limit = _nonnegative(credit_limit, "credit_limit")
        if premium_rate is not None:
            premium_rate = _nonnegative(premium_rate, "premium_rate")
        if not position or not owner or owner in FUND_ACCOUNTS:
            raise ValueError("position and non-fund owner must be specified")
        if position in self.closed_positions:
            raise ValueError("a closed facility cannot issue again")
        if owner in self.insurance_carried_fee:
            raise ValueError("this reference requires non-Carrying facility owners")
        p = self.positions.get(position)
        if p is not None and p.owner != owner:
            raise ValueError("credit position belongs to a different owner")
        if p is not None and (credit_limit is not None or premium_rate is not None):
            raise ValueError("change existing insurance terms through renewal")
        selected_insurer = insurer if insurer is not None else (p.insurer if p else None)
        if p is not None and p.principal and selected_insurer != p.insurer:
            raise ValueError("change insurers through renewal")
        selected_pool = self.insurer_pools.get(selected_insurer) if selected_insurer else None
        if selected_insurer and selected_pool is None:
            raise ValueError("insurer has no registered pool")
        if premium_deposit and selected_pool is None:
            raise ValueError("a premium deposit requires an insurer pool")
        if p is not None and p.insurance_principal and p.insurance_pool != selected_pool:
            raise ValueError("existing principal belongs to the original pool")
        if premium_deposit > self.unspent.get(owner, 0.0) + amount:
            raise InsufficientFunds("unspent BUCK cannot fund the premium deposit")
        limit = p.credit_limit if p else credit_limit
        if limit is not None and (p.principal if p else 0.0) + amount > limit:
            raise ValueError("issuance exceeds the facility limit")
        if p is None:
            p = self.positions[position] = Position(
                owner=owner, credit_limit=credit_limit,
                premium_rate=premium_rate or 0.0, insurer=selected_insurer,
                insurance_pool=selected_pool,
            )
        p.principal += amount
        self.accounts.setdefault(owner, 0.0)
        self.unspent[owner] = self.unspent.get(owner, 0.0) + amount
        if premium_deposit:
            self.spend_unspent(owner, selected_pool, premium_deposit)
            p.insurance_principal += premium_deposit

    def spend_unspent(self, owner: str, recipient: str, amount: float) -> None:
        """Move unspent issued BUCK without issuing or retiring the facility."""
        amount = _nonnegative(amount, "amount")
        if not owner or not recipient:
            raise ValueError("owner and recipient must be nonempty")
        available = self.unspent.get(owner, 0.0)
        if amount > available:
            raise InsufficientFunds(f"{owner} has {available:g} unspent, needs {amount:g}")
        self.unspent[owner] = available - amount
        self.accounts[recipient] = self.accounts.get(recipient, 0.0) + amount
        if recipient == JUBILEE:
            self.fund_other_in += amount

    def _repayment_funds(self, payer: str) -> float:
        return self.accounts.get(payer, 0.0) + self.unspent.get(payer, 0.0)

    def balance_of(self, owner: str) -> float:
        """Full spendable balance; Carrying accounts report nominal BUCK."""
        if owner == JUBILEE:
            return self.jubilee_nominal
        if owner in self.insurance_carried_fee:
            return self.accounts.get(owner, 0.0) + self.insurance_carried_fee[owner]
        return self._repayment_funds(owner)

    def _consume_repayment(self, payer: str, amount: float) -> None:
        """Consume received/held BUCK first, then this owner's unspent BUCK."""
        held = self.accounts.get(payer, 0.0)
        from_held = min(held, amount)
        self.accounts[payer] = max(0.0, held - from_held)
        rest = amount - from_held
        if rest:
            self.unspent[payer] = max(0.0, self.unspent.get(payer, 0.0) - rest)

    def _quote_insurance_nominal(self, pool: str | None, nominal: float) -> tuple[float, float | None]:
        """Quote a nominal refund carrying the pool's average demurrage."""
        if not nominal:
            return 0.0, 0.0
        if pool is None:
            raise ValueError("insurance principal has no recorded pool")
        available = self.accounts.get(pool, 0.0)
        gross = available + self.insurance_carried_fee[pool]
        if nominal > gross and pool in self.insurance_strategies:
            strategy = self.insurance_strategies[pool]
            strategy_net = self.accounts[strategy]
            strategy_gross = strategy_net + self.insurance_carried_fee[strategy]
            needed = nominal - gross
            if strategy_gross <= 0 or _exceeds_float(needed, strategy_gross):
                return 0.0, None
            available += strategy_net * needed / strategy_gross
            gross += needed
        if gross <= 0 or _exceeds_float(nominal, gross):
            return 0.0, None
        return nominal * available / gross, nominal

    def _insurance_refund(self, p: Position, amount: float) -> tuple[float, float, float | None]:
        released = p.insurance_principal * amount / p.principal
        net, nominal = self._quote_insurance_nominal(p.insurance_pool, released)
        return released, net, nominal

    def _return_insurance_nominal(self, pool: str | None, owner: str, nominal: float) -> float:
        """Move nominal BUCK from the recorded Carrying pool to its owner."""
        net, available_nominal = self._quote_insurance_nominal(pool, nominal)
        if available_nominal is None:
            raise InsufficientFunds(f"insurance pool {pool} lacks the refund")
        if not nominal or pool == owner:
            return 0.0
        cash_gross = self.accounts[pool] + self.insurance_carried_fee[pool]
        if nominal > cash_gross and pool in self.insurance_strategies:
            strategy = self.insurance_strategies[pool]
            self._return_insurance_nominal(strategy, pool, nominal - cash_gross)
            net, available_nominal = self._quote_insurance_nominal(pool, nominal)
            if available_nominal is None:
                raise InsufficientFunds(f"insurance pool {pool} lacks the refund")
        carried = nominal - net
        self.accounts[pool] = max(0.0, self.accounts[pool] - net)
        self.insurance_carried_fee[pool] = max(0.0, self.insurance_carried_fee[pool] - carried)
        self.insurance_nominal_sent[pool] += nominal
        self.accounts[owner] = self.accounts.get(owner, 0.0) + net
        if owner == JUBILEE:
            self.fund_other_in += net
            self.jubilee_carried_fee += carried
            self.jubilee_fee_inherited += carried
        elif owner in self.insurance_carried_fee:
            self.insurance_carried_fee[owner] += carried
        return net

    def _fund_insurance(self, owner: str, pool: str, amount: float) -> None:
        """Pay a quoted full-value deposit from held, then unspent BUCK."""
        if _exceeds_float(amount, self._repayment_funds(owner)):
            raise InsufficientFunds(f"{owner} lacks BUCK to fund insurance")
        held = min(amount, self.accounts.get(owner, 0.0))
        if held:
            self.transfer(owner, pool, held)
        rest = amount - held
        if rest:
            self.spend_unspent(owner, pool, min(rest, self.unspent.get(owner, 0.0)))

    def reappraise_limit(self, position: str, limit: float) -> None:
        """Model a live limit change without calling back issued credit."""
        self.positions[position].credit_limit = _nonnegative(limit, "credit_limit")

    def transfer(self, sender: str, recipient: str, amount: float) -> None:
        """Transfer a nominal amount; ordinary accounts can use unspent BUCK.

        Carrying senders pass their proportional fee to the recipient. The
        internal full-value paths used by relief and deposits gross up instead.
        """
        amount = _nonnegative(amount, "amount")
        if not sender or not recipient:
            raise ValueError("sender and recipient must be nonempty")
        if sender == JUBILEE:
            self._transfer_jubilee_nominal(recipient, amount)
            return
        if sender in self.insurance_carried_fee:
            self._return_insurance_nominal(sender, recipient, amount)
            return
        if sender == JUBILEE_INVESTED:
            self._transfer(sender, recipient, amount, relief=False)
            return
        if amount > self._repayment_funds(sender):
            raise InsufficientFunds(f"{sender} lacks spendable BUCK")
        if sender == recipient:
            return
        held = min(amount, self.accounts.get(sender, 0.0))
        if held:
            self._transfer(sender, recipient, held, relief=False)
        unspent = amount - held
        if unspent:
            self.spend_unspent(sender, recipient, unspent)

    def transfer_full_value(self, sender: str, recipient: str, amount: float) -> None:
        """Send a net amount from a Carrying account, grossing up its fee."""
        if sender != JUBILEE and sender not in self.insurance_carried_fee:
            raise ValueError("full-value transfer requires a Carrying sender")
        self._transfer(sender, recipient, amount, relief=False)

    def _transfer_jubilee_nominal(self, recipient: str, nominal: float) -> None:
        gross = self.jubilee_nominal
        if nominal > gross:
            raise InsufficientFunds("Jubilee lacks nominal BUCK")
        if not nominal or recipient == JUBILEE:
            return
        net = nominal * self.jubilee / gross
        carried = nominal - net
        self.accounts[JUBILEE] = max(0.0, self.jubilee - net)
        self.jubilee_carried_fee = max(0.0, self.jubilee_carried_fee - carried)
        self.jubilee_fee_realized += carried
        self.jubilee_nominal_sent += nominal
        self.accounts[recipient] = self.accounts.get(recipient, 0.0) + net
        if recipient in self.insurance_carried_fee:
            self.insurance_carried_fee[recipient] += carried
        if recipient == JUBILEE_INVESTED:
            self.fund_invested += net
        else:
            self.fund_other_out += net

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
        elif sender in self.insurance_carried_fee and amount:
            fee = self.insurance_carried_fee[sender]
            carried = fee if amount == available else amount * fee / available
            self.insurance_carried_fee[sender] -= carried
            self.insurance_nominal_sent[sender] += amount + carried
        else:
            carried = 0.0
        self.accounts[sender] = available - amount
        self.accounts[recipient] = self.accounts.get(recipient, 0.0) + amount
        if carried and recipient == JUBILEE:
            self.jubilee_carried_fee += carried
            self.jubilee_fee_inherited += carried
        elif carried and recipient in self.insurance_carried_fee:
            self.insurance_carried_fee[recipient] += carried
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
        self.transfer_full_value(JUBILEE, JUBILEE_INVESTED, amount)
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
            p.relief_years += p.principal * years
            if self.rate:
                p.relief_years = min(p.relief_years, p.principal / self.rate)
        z = self.rate * years
        loss_fraction = -math.expm1(-z)
        remaining_fraction = math.exp(-z)
        holder_start = (sum(value for name, value in self.accounts.items()
                            if name not in FUND_ACCOUNTS) + sum(self.unspent.values()))
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
                if name in self.insurance_carried_fee:
                    self.insurance_carried_fee[name] += fee
        for owner, held in self.unspent.items():
            incoming += held * loss_fraction
            self.unspent[owner] = held * remaining_fraction
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

    def close_matured(self, position: str, owner: str) -> tuple[float, float]:
        """Release a fully aged lien and defer its unburned BUCK to Jubilee.

        No fund assets are sold and no owner's BUCK are spent. The original
        pool refunds its nominal deposit immediately if it can do so;
        otherwise its obligation remains claimable independently of the lien.
        Returns the deferred principal and net premium actually refunded.
        """
        p = self.positions[position]
        if p.owner != owner:
            raise ValueError("credit position belongs to a different owner")
        if not p.principal or _exceeds_float(p.principal, self.rate * p.relief_years):
            raise ValueError("the facility's average issuance age has not reached maturity")
        principal = p.principal
        refund = 0.0
        if p.insurance_principal:
            _, available = self._quote_insurance_nominal(p.insurance_pool, p.insurance_principal)
            if available is None:
                self.pending_insurance_refunds[position] = (owner, p.insurance_pool, p.insurance_principal)
            else:
                refund = self._return_insurance_nominal(p.insurance_pool, owner, p.insurance_principal)
        self.deferred_jubilee_principal += principal
        del self.positions[position]
        self.closed_positions.add(position)
        return principal, refund

    def claim_matured_insurance_refund(self, position: str, owner: str) -> float:
        """Claim a deferred nominal refund from the recorded insurer pool."""
        beneficiary, pool, nominal = self.pending_insurance_refunds[position]
        if beneficiary != owner:
            raise ValueError("refund belongs to a different owner")
        net = self._return_insurance_nominal(pool, owner, nominal)
        del self.pending_insurance_refunds[position]
        return net

    def burn_deferred_jubilee(self, amount: float) -> None:
        """Burn deferred supply using at most 10% of pre-operation fund cash."""
        amount = _positive(amount, "amount")
        if _exceeds_float(amount, self.deferred_jubilee_principal):
            raise ValueError("burn exceeds deferred Jubilee principal")
        if _exceeds_float(amount, self.jubilee * 0.1):
            raise InsufficientFunds("burn exceeds 10% of liquid Jubilee BUCK")
        carried = amount * self.jubilee_carried_fee / self.jubilee
        self.accounts[JUBILEE] -= amount
        self.jubilee_carried_fee -= carried
        self.jubilee_fee_realized += carried
        self.jubilee_nominal_sent += amount + carried
        self.fund_relief_paid += amount
        self.deferred_jubilee_principal -= amount

    def quote_retirement(self, position: str, amount: float) -> RetirementQuote:
        """Quote the fixed 2% baseline, for comparison with available relief."""
        amount = _positive(amount, "amount")
        p = self.positions[position]
        if amount > p.principal:
            raise ValueError("retirement exceeds outstanding issuance")
        # Relief cannot exceed the principal being retired, even after 50 years.
        relief = min(amount, self.rate * p.relief_years * amount / p.principal)
        fund_shortfall = max(0.0, relief - self.jubilee_assets)
        gross = None
        if fund_shortfall == 0:
            # If needed, the idealized strategy returns fresh full-value BUCK
            # before the carrying transfer, diluting the existing fee basis.
            net_at_payment = max(self.jubilee, relief)
            gross = 0.0 if relief == 0 else relief * (
                net_at_payment + self.jubilee_carried_fee
            ) / net_at_payment
        released, refund, insurance_nominal = self._insurance_refund(p, amount)
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
            insurance_pool=p.insurance_pool, insurance_refund=refund,
            insurance_nominal_required=insurance_nominal,
            insurance_principal_released=released,
        )

    def retire(self, position: str, amount: float, payer: str) -> RetirementQuote:
        """Retire using the fixed 2% benchmark; a diagnostic comparison only.

        An underfunded payout fails atomically; no artificial BUCK is advanced to
        the fund and no extra debt is silently imposed on the payer.
        """
        if payer in FUND_ACCOUNTS:
            raise ValueError("payer must be distinct from Jubilee")
        p = self.positions[position]
        if p.owner is not None and p.owner != payer:
            raise ValueError("credit position belongs to a different owner")
        quote = self.quote_retirement(position, amount)
        if quote.fund_shortfall > 0.0:
            raise InsufficientFunds(f"Jubilee lacks {quote.fund_shortfall:g} BUCK")
        if quote.insurance_nominal_required is None:
            raise InsufficientFunds(f"insurance pool {quote.insurance_pool} lacks the refund")
        available = self._repayment_funds(payer) + quote.insurance_refund
        if _exceeds_float(quote.account_payment, available):
            raise InsufficientFunds(
                f"{payer} lacks {quote.account_payment - available:g} BUCK"
            )
        remaining_fraction = 1.0 - amount / p.principal
        if quote.relief > self.jubilee:
            # The toy strategy redeems at par with no delay or price impact.
            fraction = (quote.relief - self.jubilee) / self.accounts[JUBILEE_INVESTED]
            self.redeem_strategy(fraction)
        if quote.insurance_principal_released:
            self._return_insurance_nominal(quote.insurance_pool, payer, quote.insurance_principal_released)
        p.insurance_principal -= quote.insurance_principal_released
        # Deliver the full net relief using the Carrying path, then extinguish
        # the credit using the account payment plus that exact net relief.
        self._transfer(JUBILEE, payer, quote.relief, relief=True)
        self._consume_repayment(payer, quote.principal)
        p.principal -= amount
        p.buck_years *= remaining_fraction
        p.relief_years *= remaining_fraction
        return quote

    def quote_available_retirement(
        self, position: str, amount: float, redeemed_fraction: float | None = None,
        reserve_fraction: float = 0.1, payout_policy: str = "proportional",
    ) -> AvailableRetirement:
        """Preview relief from cash after redeeming the specified LP fraction.

        The reserve signal and proportional redemption policy are experimental:
        by default the fund redeems requested target relief divided by the
        total outstanding unadjusted relief benchmark. The caller may supply a
        different fraction to compare alternatives. The default proportional
        policy allocates the same fraction of pre-sale cash plus actual sale
        proceeds. The reserve comparison uses post-sale net cash divided by
        (reserve_fraction * benchmark); it is sensitive to withdrawal order.
        """
        if redeemed_fraction is None:
            redeemed_fraction = self.redemption_fraction(position, amount, "relief")
        redeemed_fraction = _nonnegative(redeemed_fraction, "redeemed_fraction")
        if redeemed_fraction > 1:
            raise ValueError("redemption fraction cannot exceed one")
        reserve_fraction = _positive(reserve_fraction, "reserve_fraction")
        if reserve_fraction > 1:
            raise ValueError("reserve fraction cannot exceed one")
        if payout_policy not in ("reserve", "proportional"):
            raise ValueError("payout policy must be reserve or proportional")
        baseline = self.quote_retirement(position, amount).relief
        benchmark = self.aggregate_relief
        proceeds = self.accounts[JUBILEE_INVESTED] * redeemed_fraction
        cash = self.jubilee + proceeds
        target = reserve_fraction * benchmark
        multiplier = cash / target if target > 0 else None
        relief = min(amount, baseline * multiplier, cash) if multiplier is not None else 0.0
        nominal = relief * (cash + self.jubilee_carried_fee) / cash if relief else 0.0
        released, refund, insurance_nominal = self._insurance_refund(self.positions[position], amount)
        result = AvailableRetirement(
            principal=amount, baseline_relief=baseline, benchmark=benchmark,
            redeemed_fraction=redeemed_fraction,
            redeemed_units=self.strategy_units * redeemed_fraction,
            redemption_proceeds=proceeds, payout_policy="reserve", fund_net_after_redemption=cash,
            reserve_target=target, rate_multiplier=multiplier,
            relief=relief, account_payment=amount - relief,
            fund_nominal_required=nominal, fund_carried_fee=nominal - relief,
            insurance_pool=self.positions[position].insurance_pool,
            insurance_refund=refund, insurance_nominal_required=insurance_nominal,
            insurance_principal_released=released,
        )
        if payout_policy == "proportional":
            return self._proportional_payout(result, self.jubilee, proceeds, redeemed_fraction)
        return result

    def quote_marked_retirement(self, position: str, amount: float) -> MarkedReliefQuote:
        """Explore a no-sale payout based on cash plus the strategy's mark.

        The allocation is the selected capped BUCK-years divided by all
        capped BUCK-years, times fund mark, capped by retired principal. The
        result is not clamped to cash: a separate 10% cash limit indicates
        whether the full quoted payout would instead revert. The idealized
        strategy mark is not a claim about an executable real basket price.
        """
        amount = _positive(amount, "amount")
        p = self.positions[position]
        if amount > p.principal:
            raise ValueError("retirement exceeds outstanding issuance")
        baseline = min(amount, self.rate * p.relief_years * amount / p.principal)
        benchmark = self.aggregate_relief
        mark = self.jubilee_assets
        share = baseline / benchmark if benchmark else 0.0
        multiplier = mark / benchmark if benchmark else None
        relief = min(amount, share * mark)
        limit = self.jubilee * 0.1
        return MarkedReliefQuote(
            principal=amount, baseline_relief=baseline, aggregate_benchmark=benchmark,
            cash_net=self.jubilee, investment_mark=self.accounts[JUBILEE_INVESTED],
            fund_mark=mark, benchmark_share=share, multiplier=multiplier,
            effective_annual_rate=self.rate * multiplier if multiplier is not None else None,
            relief=relief, account_payment=amount - relief, liquid_limit=limit,
            exceeds_liquid_limit=_exceeds_float(relief, limit),
        )

    def _proportional_payout(self, quote: AvailableRetirement, cash_before: float,
                             proceeds: float, fraction: float) -> AvailableRetirement:
        """Allocate a matching share of cash and the actual strategy proceeds."""
        relief = (min(quote.principal, cash_before * fraction + proceeds)
                  if quote.baseline_relief else 0.0)
        cash = quote.fund_net_after_redemption
        nominal = relief * (cash + self.jubilee_carried_fee) / cash if relief else 0.0
        return replace(quote, payout_policy="proportional", relief=relief,
                       rate_multiplier=(relief / quote.baseline_relief
                                        if quote.baseline_relief else None),
                       account_payment=quote.principal - relief,
                       fund_nominal_required=nominal, fund_carried_fee=nominal - relief)

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
        reserve_fraction: float = 0.1, payout_policy: str = "proportional",
    ) -> AvailableRetirement:
        """Redeem first, then pay relief bounded by actual full-value cash.

        Relief is calculated from the actual modeled proceeds after redemption.
        Failure to pay the debtor's share restores the entire reference ledger;
        a zero-value redemption never causes a fund shortfall error.
        """
        if payer in FUND_ACCOUNTS:
            raise ValueError("payer must be distinct from Jubilee")
        p = self.positions[position]
        if p.owner is not None and p.owner != payer:
            raise ValueError("credit position belongs to a different owner")
        if redeemed_fraction is None:
            redeemed_fraction = self.redemption_fraction(position, amount, "relief")
        redeemed_fraction = _nonnegative(redeemed_fraction, "redeemed_fraction")
        if redeemed_fraction > 1:
            raise ValueError("redemption fraction cannot exceed one")
        if payout_policy not in ("reserve", "proportional"):
            raise ValueError("payout policy must be reserve or proportional")
        # The reference uses a snapshot to model transaction rollback. Actual
        # contract execution must enforce atomicity without copying the ledger.
        before = copy.deepcopy(self.__dict__)
        try:
            cash_before = self.jubilee
            units, proceeds = self.redeem_strategy(redeemed_fraction)
            result = self.quote_available_retirement(position, amount, 0.0, reserve_fraction,
                                                     payout_policy="reserve")
            result = replace(result, redeemed_fraction=redeemed_fraction,
                             redeemed_units=units, redemption_proceeds=proceeds)
            if payout_policy == "proportional":
                result = self._proportional_payout(result, cash_before, proceeds, redeemed_fraction)
            if result.insurance_nominal_required is None:
                raise InsufficientFunds(f"insurance pool {result.insurance_pool} lacks the refund")
            if result.insurance_principal_released:
                self._return_insurance_nominal(result.insurance_pool, payer,
                                               result.insurance_principal_released)
            available = self._repayment_funds(payer)
            if _exceeds_float(result.account_payment, available):
                raise InsufficientFunds(f"{payer} lacks {result.account_payment - available:g} BUCK")
            p = self.positions[position]
            remaining_fraction = 1.0 - amount / p.principal
            p.insurance_principal -= result.insurance_principal_released
            self._transfer(JUBILEE, payer, result.relief, relief=True)
            self._consume_repayment(payer, amount)
            p.principal -= amount
            p.buck_years *= remaining_fraction
            p.relief_years *= remaining_fraction
            return result
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(before)
            raise

    def renew_unspent(
        self, position: str, owner: str, *, new_limit: float, new_premium_rate: float,
        additional_issuance: float = 0.0, new_insurer: str | None = None,
        new_insurance_principal: float | None = None, redeemed_fraction: float | None = None,
        payout_policy: str = "proportional",
    ) -> Renewal:
        """Crystallize relief and replace one facility's terms atomically.

        ``new_limit`` is the caller's already-priced, K-adjusted capacity;
        the model does not authenticate the insurer or price premium reserves.
        If already over its prior limit, renewal may improve or preserve, but
        not worsen, the shortfall. Existing issued BUCK are never called back.
        """
        new_limit = _nonnegative(new_limit, "new_limit")
        new_premium_rate = _nonnegative(new_premium_rate, "new_premium_rate")
        additional_issuance = _nonnegative(additional_issuance, "additional_issuance")
        p = self.positions[position]
        if p.owner != owner:
            raise ValueError("credit position belongs to a different owner")
        old_principal = p.principal
        old_limit = p.credit_limit if p.credit_limit is not None else old_principal
        old_pool = p.insurance_pool
        old_deposit = p.insurance_principal
        new_deposit = (old_deposit if new_insurance_principal is None else
                       _nonnegative(new_insurance_principal, "new_insurance_principal"))
        insurer = p.insurer if new_insurer is None else new_insurer
        if insurer == "":
            raise ValueError("insurer must be specified")
        new_pool = self.insurer_pools.get(insurer) if insurer else None
        if insurer and new_pool is None:
            raise ValueError("insurer has no registered pool")
        if new_deposit and new_pool is None:
            raise ValueError("a premium deposit requires an insurer pool")
        if old_deposit and old_pool is None:
            raise ValueError("insurance principal has no recorded pool")
        if redeemed_fraction is None:
            redeemed_fraction = (self.redemption_fraction(position, old_principal, "relief")
                                 if old_principal else 0.0)
        redeemed_fraction = _nonnegative(redeemed_fraction, "redeemed_fraction")
        if redeemed_fraction > 1:
            raise ValueError("redemption fraction cannot exceed one")
        if payout_policy not in ("reserve", "proportional"):
            raise ValueError("payout policy must be reserve or proportional")

        before = copy.deepcopy(self.__dict__)
        try:
            cash_before = self.jubilee
            _, proceeds = self.redeem_strategy(redeemed_fraction)
            quote = (self.quote_available_retirement(position, old_principal, 0.0,
                                                     payout_policy="reserve")
                     if old_principal else None)
            if quote and payout_policy == "proportional":
                quote = self._proportional_payout(quote, cash_before, proceeds, redeemed_fraction)
            baseline = quote.baseline_relief if quote else 0.0
            relief = quote.relief if quote else 0.0
            new_principal = old_principal - relief + additional_issuance
            old_shortfall = max(0.0, old_principal - old_limit)
            new_shortfall = max(0.0, new_principal - new_limit)
            if _exceeds_float(new_shortfall, old_shortfall):
                raise ValueError("renewal would worsen the issued-credit shortfall")

            # Model a fresh policy: return the old nominal deposit carrying
            # its pool's fee, then pay the replacement deposit in full BUCK.
            # This also keeps the source unambiguous when insurers change.
            refund = self._return_insurance_nominal(old_pool, owner, old_deposit)
            funding = new_deposit
            self._transfer(JUBILEE, owner, relief, relief=True)
            self._consume_repayment(owner, relief)

            # Commit final capacity and issuance together; old coverage was
            # never deactivated. Relief already removed its own BUCK.
            p.principal = new_principal
            p.credit_limit = new_limit
            p.premium_rate = new_premium_rate
            p.insurer = insurer
            p.insurance_pool = new_pool
            p.buck_years = 0.0
            p.relief_years = 0.0
            self.unspent[owner] = self.unspent.get(owner, 0.0) + additional_issuance
            if funding:
                self._fund_insurance(owner, new_pool, funding)
            p.insurance_principal = new_deposit
            return Renewal(
                position=position, baseline_relief=baseline, relief=relief,
                old_principal=old_principal, additional_issuance=additional_issuance,
                new_principal=new_principal, new_limit=new_limit,
                new_premium_rate=new_premium_rate, old_insurance_pool=old_pool,
                new_insurance_pool=new_pool, old_insurance_principal=old_deposit,
                new_insurance_principal=new_deposit, insurance_refunded=refund,
                insurance_principal_released=old_deposit,
                insurance_funded=funding, redemption_proceeds=proceeds,
            )
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
        "position_buck_years": p.buck_years,
        "position_relief_years": p.relief_years,
        "holder": ledger.accounts[holder],
        "holder_unspent": ledger.unspent.get(holder, 0.0),
        "holder_available": ledger.balance_of(holder),
        "jubilee": ledger.jubilee_assets,
        "jubilee_cash": ledger.jubilee,
        "jubilee_cash_nominal": ledger.jubilee_nominal,
        "jubilee_cash_carried_fee": ledger.jubilee_carried_fee,
        "jubilee_fee_created": ledger.jubilee_fee_created,
        "jubilee_fee_inherited": ledger.jubilee_fee_inherited,
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
        "conservation_error": sum(ledger.accounts.values()) + sum(ledger.unspent.values()) - ledger.issued,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=float, default=50, help="end of run (default: 50)")
    parser.add_argument("--issued", type=float, default=100, help="initial issuance (default: 100)")
    parser.add_argument("--held", action="store_true", help="issue directly into ordinary held BUCK rather than issued-unspent credit")
    parser.add_argument("--rate", type=float, default=0.02, help="nominal annual rate (default: 0.02)")
    parser.add_argument("--acquire-all", action="store_true", help="transfer the remaining holder BUCK to Jubilee at the end; any non-BUCK consideration is outside this model")
    parser.add_argument("--deploy-fund", action="store_true", help="deploy fund cash to a 1:1 BUCK strategy at each checkpoint")
    parser.add_argument("--strategy-return", type=float, default=0, help="BUCK returned by the strategy at the end, sourced from the holder")
    parser.add_argument("--wipe-strategy", action="store_true", help="model a total strategy loss to the holder before retirement")
    parser.add_argument("--retire", type=float, default=0, help="credit principal to reduce after the final checkpoint")
    parser.add_argument("--redemption-basis", choices=("issuance", "relief"), default="relief", help="select the fraction's denominator (default: relief)")
    parser.add_argument("--redeem-fraction", type=float, help="explicit fraction of strategy units sold before retirement; overrides --redemption-basis")
    parser.add_argument("--payout-policy", choices=("reserve", "proportional"), default="proportional", help="payout policy (default: proportional); reserve is a comparison")
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    args = parser.parse_args(argv)
    years = _nonnegative(args.years, "years")
    ledger = DemurrageLedger(args.rate)
    if args.held:
        ledger.issue("credit", args.issued, "holder")
    else:
        ledger.issue_unspent("credit", args.issued, "holder")
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
        held = ledger.accounts["holder"]
        unspent = ledger.unspent.get("holder", 0.0)
        acquired = held + unspent
        ledger.transfer("holder", JUBILEE, held)
        ledger.spend_unspent("holder", JUBILEE, unspent)
    available_retirement = None
    if args.retire:
        fraction = (args.redeem_fraction if args.redeem_fraction is not None
                    else ledger.redemption_fraction("credit", args.retire, args.redemption_basis))
        available_retirement = ledger.retire_available("credit", args.retire, "holder", fraction,
                                                       payout_policy=args.payout_policy)
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
        print(" year       held    unspent    Jubilee  gross fees  self-paid    target  baseline gap")
        for row in rows:
            q = row["retirement"]
            print("{:5.1f} {:10.6f} {:10.6f} {:10.6f} {:11.6f} {:10.6f} {:9.6f} {:10.6f}".format(
                row["years"], row["holder"], row["holder_unspent"], row["jubilee"], row["gross_demurrage"],
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
