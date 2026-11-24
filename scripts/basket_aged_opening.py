"""Age the canonical opening balance sheet before the household simulation clock starts."""

from contextlib import contextmanager

from eth_account import Account
from web3.logs import DISCARD

from alberta_buck.sim import identity, loop
from alberta_buck.sim.basket_machine import publish_jubilee_valuation
from buck_core.basket import NetBasketOps
from buck_core.net_world import net_transfer_value

from basket_jubilee_endowment import BP, NOMINAL_MARGIN_BP, Q, RAW, SHARE_MARGIN_BP, opening_balances


DAY_SECONDS = 86400


def funded_passive_holders(d, total, count, seed):
    """Register deterministic simulation EOAs and transfer full-value Ordinary cash."""
    if d.reg.functions.isCarrying(d.simlp.address).call():
        raise ValueError("passive allocations require the standard Ordinary SimLP")
    if d.buck.functions.netBalanceOf(d.simlp.address).call() < total:
        raise ValueError("passive allocations exceed existing SimLP cash")
    if not total:
        return [], []
    chunk, remainder = divmod(total, count)
    wallets, transfers = [], []
    rng = identity.seeded_rng(seed)
    for i in range(count):
        label = "AgedOpeningPassive"
        account = Account.from_key(identity._deterministic_key(seed, label, i))
        args, secret = identity.register_args(
            d.issuer_kp, int(account.address, 16), identity.fields_for(label, i), rng,
            d.w3.eth.chain_id, int(d.reg.address, 16), with_sk=True)
        # Fund only native gas from the existing test signer and register in
        # the actual registry. The holder remains a private Ordinary EOA.
        d.w3.eth.send_transaction(dict(to=account.address, value=10**16, gas=21000,
                                       gasPrice=0, **{"from": d.gov}))
        d.chain.send(d.reg.functions.register(d.issuer_addr, *args), sender=account)
        identity.identity_approve(d.chain, d.reg, d.buck, account, args, secret, d.simlp.address, rng)
        if (not d.reg.functions.isVerified(account.address).call()
                or d.reg.functions.isCarrying(account.address).call()
                or d.buck.functions.isSponsored(account.address).call()):
            raise ValueError("passive holder is not a verified unsponsored Ordinary account")
        wallets.append(account.address)
        transfers.append(chunk + int(i < remainder))
    evidence = []
    for address, amount in zip(wallets, transfers, strict=True):
        operation = d.simlp.functions.exec(d.buck.address, d.buck.encode_abi(
            "transfer", args=[address, amount]))
        receipt = d.chain.send(operation, sender=d.gov)
        delivery = net_transfer_value(d.buck, receipt, d.simlp.address, address)
        if delivery.count != 1 or delivery.nominal != amount or delivery.net_scaled != amount * Q:
            raise ValueError("passive funding transfer did not deliver its authorized amount")
        evidence.append(dict(address=address, credited_raw=amount, tx=receipt["transactionHash"].hex()))
    return wallets, evidence


def read_opening(d, holders):
    buck = d.buck.functions
    fund = d.net_stack.fund
    pending = buck.maturitiesPending().call()
    return dict(
        **opening_balances(d, fund), timestamp=d.w3.eth.get_block("latest")["timestamp"],
        block=d.w3.eth.block_number, passive_cash_scaled=[buck.netBalanceScaled(a).call() for a in holders],
        maturities_pending=pending,
        fund_reserve=fund.functions.previewReserve().call() if not pending else None,
    )


def settle_opening(d, holders):
    buck, fund = d.buck.functions, d.net_stack.fund
    while buck.maturitiesPending().call():
        processed, current = buck.processMaturities(64).call({"from": d.gov})
        if not processed and not current:
            raise ValueError("claim heap cannot be made current by the bounded processor")
        d.chain.send(buck.processMaturities(64), sender=d.gov)
    for address in [*holders, d.simlp.address, fund.address]:
        d.chain.send(buck.settleDemurrage(address), sender=d.gov)
    d.chain.send(fund.functions.checkpointReserve(), sender=d.gov)


def invest_aged_excess(d):
    """Invest current reserve excess with the normal governed bounds and receipt."""
    fund = d.net_stack.fund
    target, excess, _ = fund.functions.previewReserve().call()
    amount = min(excess // Q - 1, d.buck.functions.maxFundNetPayout().call())
    if amount <= 0:
        return None
    nominal = d.buck.functions.fundNominalForNet(amount).call()
    quote = NetBasketOps(d.chain, d.basket, d.buck, execute=None).quote_deposit_shares(amount)
    if quote <= 0:
        raise ValueError("aged Jubilee excess has no positive deposit share quote")
    maximum = (nominal * (BP + NOMINAL_MARGIN_BP) + BP - 1) // BP
    minimum = max(1, quote * (BP - SHARE_MARGIN_BP) // BP)
    invest = fund.functions.invest(amount, maximum, amount, minimum)
    invest.call({"from": d.gov})
    receipt = d.chain.send(invest, sender=d.gov)
    events = [e["args"] for e in fund.events.Invested().process_receipt(receipt, errors=DISCARD)
              if e["address"].lower() == fund.address.lower()]
    if len(events) != 1 or events[0]["netSpentScaled"] > excess:
        raise ValueError("aged investment failed to reconcile to the available reserve excess")
    invested = events[0]
    return dict(tx=receipt["transactionHash"].hex(), request_raw=amount, credited_raw=invested["credited"],
                spent_scaled=invested["netSpentScaled"], shares_added=invested["sharesAdded"],
                prior_target_scaled=target, prior_excess_scaled=excess)


def apply_aged_opening(d, aged_days, passive_buck, passive_holders, seed, *, after_age=None):
    """Age existing shares, balances and claims without running market or household agents."""
    if (type(aged_days) is not int or aged_days < 0 or type(passive_buck) is not int or passive_buck < 0
            or type(passive_holders) is not int or passive_holders < 2):
        raise ValueError("require nonnegative whole age days and passive BUCK plus at least two holders")
    before = read_opening(d, [])
    holders, funding = funded_passive_holders(d, passive_buck * RAW, passive_holders, seed)
    distributed = read_opening(d, holders)
    if aged_days:
        d.anvil.warp_to(distributed["timestamp"] + aged_days * DAY_SECONDS)
    aged = read_opening(d, holders)
    if aged["timestamp"] - distributed["timestamp"] != aged_days * DAY_SECONDS:
        raise ValueError("EVM did not age by the requested calendar interval")
    if not aged["maturities_pending"] and distributed["claim_seconds"] is not None:
        change = aged["claim_seconds"] - distributed["claim_seconds"]
        if change != distributed["total_issued_raw"] * aged_days * DAY_SECONDS:
            raise ValueError("opening claim-seconds do not reconcile to elapsed issuance")
    settle_opening(d, holders)
    settled = read_opening(d, holders)
    investment = invest_aged_excess(d) if aged_days else None
    if investment:
        publish_jubilee_valuation(d)
    allocated = read_opening(d, holders)
    if investment and allocated["jubilee_shares"] - settled["jubilee_shares"] != investment["shares_added"]:
        raise ValueError("aged investment disagrees with the actual receipt share growth")
    targeted = after_age(d) if after_age is not None else None
    after = read_opening(d, holders)
    for field in ("total_issued_raw", "basket_principal_raw", "funding_principal_raw"):
        if any(balance[field] != before[field] for balance in (distributed, aged, settled, allocated, after)):
            raise ValueError(f"aged opening changed outstanding principal ({field})")
    return dict(aged_days=aged_days, passive_buck=passive_buck, passive_funding=funding,
                before=before, distributed=distributed, aged=aged, settled=settled, allocated=allocated,
                aged_investment=investment, targeted_opening=targeted, after=after)


@contextmanager
def aged_opening(aged_days, passive_buck, passive_holders, *, seed=41404, after_age=None):
    """Install only around sequential fresh worlds in a dedicated study worker.

    Snapshotter is constructed after deployment and pool bootstrap. The
    normal loop chooses its opening timestamps afterward from the aged EVM.
    Ordinary household decisions and live price shocks start at the new
    day zero. During the opening pause, all outstanding BUCK contributes to
    global fund accrual. Settling named wallets does not credit the fund again.
    """
    snapshotter = loop.Snapshotter
    audits = []

    class AgedSnapshotter(snapshotter):
        def __init__(self, deployment, scenario):
            audit = apply_aged_opening(deployment, aged_days, passive_buck, passive_holders, seed,
                                       after_age=after_age)
            super().__init__(deployment, scenario)
            audits.append(audit)
            self.meta["aged_opening"] = audit

    loop.Snapshotter = AgedSnapshotter
    try:
        yield audits
    finally:
        loop.Snapshotter = snapshotter
