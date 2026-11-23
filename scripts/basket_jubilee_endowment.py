"""Opening Jubilee capitalization for the household-economy research runs."""

from web3.logs import DISCARD

from buck_core.basket import NetBasketOps, RECEIPT_ABI
from buck_core.net_world import net_transfer_value


RAW = 10**6
Q = 10**36  # Fractional units per raw BUCK unit.
BP = 10_000
NOMINAL_MARGIN_BP = 1
SHARE_MARGIN_BP = 50  # The ordinary household basket-deposit allowance.


def opening_balances(d, fund):
    position_id = fund.functions.positionId().call()
    shares, basis = d.basket.functions.holdings(position_id).call() if position_id else (0, 0)
    funding_facilities = tuple(
        d.credit.functions.tokenOfOwnerByIndex(d.simlp.address, i).call()
        for i in range(d.credit.functions.balanceOf(d.simlp.address).call())
    )
    counters = d.buck.functions.fundFlows().call()
    return dict(
        gross_demurrage_scaled=counters[0][0] * Q + counters[0][1],
        net_demurrage_scaled=counters[1][0] * Q + counters[1][1],
        claim_seconds=d.buck.functions.claimSeconds().call(),
        total_issued_raw=d.buck.functions.totalIssued().call(),
        basket_principal_raw=d.basket.functions.lien().call(),
        funding_principal_raw=sum(d.credit.functions.issuanceOf(i).call()[0] for i in funding_facilities),
        simlp_cash_scaled=d.buck.functions.netBalanceScaled(d.simlp.address).call(),
        jubilee_cash_scaled=d.buck.functions.netBalanceScaled(fund.address).call(),
        jubilee_receipt_id=position_id,
        jubilee_receipt_basis_raw=basis,
        jubilee_shares=shares,
        total_shares=d.basket.functions.totalShares().call(),
    )


def apply_opening_endowment(deployment, net_buck: int, *, cash_buck: int = 0):
    """Allocate existing SimLP cash to the fund's basket position before day-zero trading.

    `net_buck` is whole BUCK. This is a research transfer from the seeder's finite
    Ordinary cash balance, which came from standard bootstrap. It represents a
    different allocation of that cash at opening and changes the seeder's
    subsequent cash capacity. Governance explicitly invests the donation in the
    existing Jubilee receipt, subject to the normal basket and investment guards.

    Call once per fresh world after bootstrap. A rejected operation invalidates
    that world, as the public donation and governance investment are separate
    transactions.
    """
    if any(type(v) is not int or v < 0 for v in (cash_buck, net_buck)):
        raise ValueError("opening Jubilee endowment must be a nonnegative whole BUCK amount")
    d = deployment
    fund = d.net_stack.fund
    amount = net_buck * RAW
    donation = amount + cash_buck * RAW
    before = opening_balances(d, fund)
    audit = dict(requested_cash_buck=cash_buck, requested_buck=net_buck, requested_raw=amount, funding_holder=d.simlp.address,
                 before=before, after=before, transfer=None, investment=None)
    if donation == 0:
        return audit

    if d.reg.functions.isCarrying(d.simlp.address).call():
        raise ValueError("opening endowment requires the canonical Ordinary SimLP")
    if fund.functions.governance().call().lower() != d.gov.lower():
        raise ValueError("opening endowment requires the existing Jubilee governance signer")
    if before["simlp_cash_scaled"] < donation * Q:
        available = before["simlp_cash_scaled"] // Q
        raise ValueError(f"opening endowment exceeds SimLP's existing cash ({available} raw BUCK units)")

    transfer = d.simlp.functions.exec(
        d.buck.address, d.buck.encode_abi("transfer", args=[fund.address, donation]))
    # Ordinary cash transfers deliver full value. Preflight the exact existing
    # credit-free send and ensure the basket has a usable HIGH-mark share quote.
    transfer.call({"from": d.gov})
    ops = NetBasketOps(d.chain, d.basket, d.buck, execute=None)
    if amount and ops.quote_deposit_shares(amount) <= 0:
        raise ValueError("opening endowment has no positive basket share quote")
    sent = d.chain.send(transfer, sender=d.gov)
    delivered = net_transfer_value(d.buck, sent, d.simlp.address, fund.address)
    if delivered.count != 1 or delivered.nominal != donation or delivered.net_scaled != donation * Q:
        raise ValueError("opening endowment transfer did not deliver the quoted BUCK")

    audit["transfer"] = dict(tx=sent["transactionHash"].hex(), nominal_raw=delivered.nominal,
                             delivered_scaled=delivered.net_scaled)
    if amount == 0:
        after = opening_balances(d, fund)
        for field in ("total_issued_raw", "funding_principal_raw", "basket_principal_raw",
                      "total_shares", "jubilee_shares"):
            if after[field] != before[field]:
                raise ValueError(f"cash-only opening changed {field}")
        audit["after"] = after
        return audit

    nominal_quote = d.buck.functions.fundNominalForNet(amount).call()
    # The transfer can change the fund's marked claim on the basket.
    shares_quote = ops.quote_deposit_shares(amount)
    max_nominal = (nominal_quote * (BP + NOMINAL_MARGIN_BP) + BP - 1) // BP
    min_shares = max(1, shares_quote * (BP - SHARE_MARGIN_BP) // BP)
    invest = fund.functions.invest(amount, max_nominal, amount, min_shares)
    invest.call({"from": d.gov})
    receipt = d.chain.send(invest, sender=d.gov)
    events = [log["args"] for log in fund.events.Invested().process_receipt(receipt, errors=DISCARD)
              if log["address"].lower() == fund.address.lower()]
    if len(events) != 1:
        raise ValueError("opening endowment investment lacks a unique Jubilee receipt event")
    event = events[0]
    after = opening_balances(d, fund)
    position_id = after["jubilee_receipt_id"]
    nft = d.w3.eth.contract(address=d.basket.functions.receipt().call(), abi=RECEIPT_ABI)
    if (not position_id or position_id != event["positionId"]
            or nft.functions.ownerOf(position_id).call().lower() != fund.address.lower()
            or before["jubilee_receipt_id"] not in (0, position_id)
            or after["jubilee_shares"] - before["jubilee_shares"] != event["sharesAdded"]
            or after["total_shares"] - before["total_shares"] != event["sharesAdded"]
            or after["jubilee_receipt_basis_raw"] - before["jubilee_receipt_basis_raw"] != event["credited"]
            or event["sharesAdded"] < min_shares or event["credited"] < amount
            or event["netSpentScaled"] < amount * Q or event["nominal"] > max_nominal):
        raise ValueError("opening Jubilee receipt and investment event do not reconcile")
    for field in ("total_issued_raw", "funding_principal_raw", "basket_principal_raw"):
        if after[field] != before[field]:
            raise ValueError(f"opening endowment changed outstanding principal ({field})")
    if before["simlp_cash_scaled"] - after["simlp_cash_scaled"] < delivered.net_scaled:
        raise ValueError("SimLP cash did not fund the opening endowment")
    audit.update(after=after,
                 investment=dict(tx=receipt["transactionHash"].hex(), receipt_id=position_id,
                                 nominal_raw=event["nominal"], spent_scaled=event["netSpentScaled"],
                                 credited_raw=event["credited"], shares_added=event["sharesAdded"],
                                 max_nominal_raw=max_nominal, min_shares=min_shares))
    return audit
