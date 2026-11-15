"""Canonical economy observations; cash, debt and receipt marks stay separate.

Required monetary reads fail the capture rather than fabricating zero balances.
Only a guarded, unpublished relief mark may be unavailable. Scaled amounts and
BUCK-seconds are decimal strings so browser readers cannot round uint256 values.
These observations neither authorize an exit nor promise executable liquidity.
"""

from web3 import Web3

from buck_core import mark_net_basket_positions
from buck_core.net_world import Q


def deployment_manifest(d):
    """Record actual public wiring/code, without credentials or signing keys.

    Pre: the canonical stack is configured. Post: the model and code identities
    can be attributed in a saved report; this is not a restore verification.
    """
    stack = d.net_stack
    model = d.buck.functions.ACCOUNTING_MODEL().call()
    if model != Web3.keccak(text="Buck/compounded-cash/per-facility-jubilee/v1"):
        raise ValueError("unexpected economy accounting model")

    def identity(address):
        code = d.w3.eth.get_code(address)
        if not code:
            raise ValueError(f"missing economy contract code: {address}")
        return {"address": address, "code_hash": Web3.to_hex(Web3.keccak(code))}

    roles = {name: getattr(d, name, None) for name in
             ("buck", "credit", "reg", "kctrl", "basket", "desk", "wheel", "venue",
              "equity_upkeep", "v3_factory", "pair_policy", "monetary_policy", "signals",
              "router", "permit2", "swap_guard", "usdc")}
    roles.update(jubilee=stack.fund, quoter=stack.quoter)
    addresses = {name: contract.address for name, contract in roles.items() if contract is not None}
    addresses.update(claim_book=d.buck.functions.claimBook().call(), receipt=d.basket.functions.receipt().call())
    libraries = {name: getattr(stack, name).address for name in
                 ("cash_library", "transfer_library", "facilities_library", "renewal_library")}
    libraries["basket_valuation"] = d.net_basket.valuation_library.address
    libraries["basket_market_ops"] = d.net_basket.market_library.address
    return {"model": Web3.to_hex(model), "chain_id": d.w3.eth.chain_id, "scale": str(Q),
            "roles": {name: identity(address) for name, address in addresses.items()},
            "libraries": {name: identity(address) for name, address in libraries.items()},
            "jubilee_policy": [str(v) for v in d.wheel.functions.jubileePolicy().call()],
            "basket_nav_basis": "owned net-marked inventory plus cash; unpaid relief reported separately",
            "position_mark_basis": "receipt valueOf at one block, before exit costs",
            "lp_fee_basis": "net-deliverable BUCK and day-zero TOKEN/USD prices",
            "basket_cash_mode": "carrying" if d.reg.functions.isCarrying(d.basket.address).call() else "ordinary",
            "arb_return_basis": "net wallet plus owned LP principal and new fees; TOKEN marks frozen per trade"}


def observe(d, agents):
    """Read one block's principal/cash/fund book and observed agent receipts.

    Pre: owners' tracked receipt IDs are live and unique. Post: every mark is
    the contract's exact valueOf, with remaining basis; unknown third-party
    receipts are not represented as covered. Jubilee's live investor mark and
    guarded last publication are explicitly separate observations.
    """
    block = d.w3.eth.block_number
    read = lambda fn: fn.call(block_identifier=block)
    buck, fund = d.buck, d.net_stack.fund
    total = int(read(buck.functions.totalIssued()))
    book = d.chain.at("JubileeClaimBook", read(buck.functions.claimBook()))
    deferred = int(read(book.functions.deferredPrincipal()))
    if deferred > total:
        raise ValueError("deferred principal exceeds total issuance")
    principal = 0
    for i in range(int(read(d.credit.functions.balanceOf(d.simlp.address)))):
        token_id = read(d.credit.functions.tokenOfOwnerByIndex(d.simlp.address, i))
        principal += int(read(d.credit.functions.issuanceOf(token_id))[0])
    gross, net = read(buck.functions.accountBalancesScaled(fund.address))
    pending = bool(read(buck.functions.maturitiesPending()))
    out = {"block": block, "total_issued": total, "active_principal": total - deferred,
           "deferred_principal": deferred, "simlp_principal": principal,
           "simlp_cash": int(read(buck.functions.balanceOf(d.simlp.address))),
           "maturities_pending": pending,
           "claim_seconds": None if pending else str(read(buck.functions.claimSeconds())),
           "available_relief_scaled": str(read(buck.functions.availableReliefScaled())),
           "jubilee": {"nominal_scaled": str(gross), "net_scaled": str(net),
                       "nominal": gross // Q, "cash": net // Q}}

    def carrying(address):
        nominal, actual = map(int, read(buck.functions.accountBalancesScaled(address)))
        return {"address": address, "nominal_scaled": str(nominal), "net_scaled": str(actual),
                "net_ratio_scaled": str(actual * Q // nominal) if nominal else None}

    # One block's exact fee bases for plots and per-trade event interpretation.
    # Empty cash has no ratio. A high-traffic pool is not assumed to be fresh.
    out["carrying"] = {"basket": carrying(d.basket.address), "buck_usdc": carrying(d.pool_ub),
                       "buck_tokens": [{"token": token.address, **carrying(pool)}
                                       for token, pool in zip(d.tokens, d.pool_buck)]}

    def marks(owner, ids):
        observed = mark_net_basket_positions(d.chain, d.basket, buck, owner, ids, block_number=block)
        return {"owner": owner, "basis": observed.basis, "mark": observed.mark, "roi": observed.roi,
                "positions": [{"id": p.token_id, "shares": str(p.shares), "basis": p.basis, "mark": p.mark}
                              for p in observed.positions]}

    fund_id = int(read(fund.functions.positionId()))
    out["jubilee"]["position"] = marks(fund.address, [fund_id] if fund_id else [])
    try:
        out["jubilee"]["relief_position_mark"] = int(read(fund.functions.reliefPositionMark()))
    except Exception as exc:
        out["jubilee"]["relief_position_mark"] = None
        out["jubilee"]["relief_mark_error"] = str(exc)[:400]
    if d.wheel is not None:
        source, mark, key, height, timestamp, ready = read(d.wheel.functions.publishedValuation())
        out["jubilee"]["publication"] = {"source": source, "mark": int(mark), "key": Web3.to_hex(key),
                                         "block": int(height), "timestamp": int(timestamp), "ready": bool(ready)}

    positions, seen = [], set()
    for ag in agents:
        rid = getattr(ag, "_receipt_id", None)
        ids = ([rid] if rid is not None and not getattr(ag, "_exited", False) else [])
        owner = ag.address if ids else None
        savings = getattr(ag, "_savings_proxy", None)
        household_ids = list(getattr(ag, "_basket_receipts", []))
        if household_ids:
            if ids:
                raise ValueError("ambiguous basket receipt owner")
            owner, ids = (savings.address if savings is not None else ag.address), household_ids
        if not ids:
            continue
        if seen.intersection(ids):
            raise ValueError("duplicate observed basket receipt")
        seen.update(ids)
        positions.append({"agent": f"{type(ag).__name__}#{ag.idx}", **marks(owner, ids)})
    return out, positions


def _tdiv(a, b):
    """Integer division truncated toward zero, as JS BigInt divides."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def buck_bid_ask(d, read, spot):
    """The BUCK's bid and ask in USDC, each (numerator, denominator) raw USDC per raw net BUCK.

    The bid is what selling a full-value BUCK fetches; the ask what a net BUCK costs (bought, it
    arrives at the dollar pool's net / gross). The price book's (BasketReference) while its sample
    is fresh; otherwise the dollar pool's spot (`spot()`, the same ratio) and its carry.
    """
    book = getattr(d, "signals", None)
    if book is not None and read(book.functions.fresh()):
        bid = read(book.functions.buckBid())
        if bid:
            one = 10 ** read(d.buck.functions.decimals())
            return (bid, one), (read(book.functions.buckAsk()), one)
    gross, net = read(d.buck.functions.accountBalancesScaled(d.pool_ub))
    num, den = spot()
    return (num, den), ((num * gross, den * net) if net and gross != net else (num, den))


def household_mark(d, agent):
    """Mark one household's financial portfolio at one block, before exits.

    Pre: distinct ordinary custody accounts, tracked owned NFT/receipt IDs,
    liquid initialized reference pools and available guarded relief quotes.
    Post: no state changes; exact integer money/price arithmetic. Count all
    constituent TOKENs and USDC in borrower, savings and working wallets.
    Separate full-value BUCK cash, actual principal, marked relief and each
    insurer-pool refund mark. The insured asset is common to both mortgage
    comparisons and excluded from financial net worth; arrears are deducted.
    """
    block = d.w3.eth.block_number
    read = lambda fn: fn.call(block_identifier=block)
    owner = agent.address
    # A day's mark must not stop the world: a relief quote the fund's own price guard refuses
    # is recorded as unavailable (and left out of net worth, flagged), never as a zero.
    book = d.net_credit_ops.mark_household(owner, agent._token_ids, block_number=block,
                                           tolerate_unavailable_relief=True)
    relief = book.relief_scaled or 0
    savings, wallet = agent._savings_proxy, agent._wallet
    accounts = [owner] + [p.address for p in (savings, wallet) if p is not None]
    if len(set(a.lower() for a in accounts)) != len(accounts):
        raise ValueError("household custody accounts must be distinct")
    cash, bucks = {}, {}
    for address in accounts:
        if read(d.reg.functions.isCarrying(address)) or not read(d.reg.functions.isVerified(address)):
            raise ValueError("household custody must be verified and non-Carrying")
        cash[address] = int(read(d.usdc.functions.balanceOf(address)))
        bucks[address] = int(read(d.buck.functions.netBalanceScaled(address)))

    def price_ratio(pool, token):
        venue = d.chain.at("UniswapV3Pool", pool)
        t0, t1 = read(venue.functions.token0()), read(venue.functions.token1())
        if {t0.lower(), t1.lower()} != {token.address.lower(), d.usdc.address.lower()}:
            raise ValueError("household mark requires a canonical settlement pair")
        sqrt = int(read(venue.functions.slot0())[0])
        if sqrt <= 0 or not read(venue.functions.liquidity()):
            raise ValueError("household mark price unavailable")
        # Raw USDC per raw token; no per-unit rounding or floating point.
        return (sqrt * sqrt, 1 << 192) if t0.lower() == token.address.lower() else (1 << 192, sqrt * sqrt)

    pending = 0
    for token, pool in zip(d.tokens, d.pool_usdc):
        amount = sum(int(read(token.functions.balanceOf(a))) for a in accounts)
        if amount:
            numerator, denominator = price_ratio(pool, token)
            pending += amount * numerator // denominator
    bid, ask = buck_bid_ask(d, read, lambda: price_ratio(d.pool_ub, d.buck))
    receipt_owner = savings.address if savings is not None else owner
    position = mark_net_basket_positions(d.chain, d.basket, d.buck, receipt_owner,
                                         agent._basket_receipts, block_number=block)
    scaled = sum(bucks.values()) + position.mark * Q + book.premium_net_scaled - book.state.issued * Q + relief
    # Owner, 2026-11-15: worth what retiring it now fetches. The household's BUCK (cash, receipts,
    # premium, relief) first retires its BUCK debt at par; a surplus sells at the BUCK's bid, a
    # shortfall is bought at its ask (a bought BUCK arrives at the dollar pool's ratio).
    # Match JS BigInt's signed division (truncate toward zero) once, after
    # combining full-precision BUCK values. A negative book is valid data.
    numerator, denominator = bid if scaled >= 0 else ask
    valued = _tdiv(scaled * numerator, Q * denominator)
    arrears = agent.living_arrears + agent.premium_arrears
    usd = sum(cash.values()) + pending - agent.mortgage - arrears
    # The same worth in net BUCK: dollars bought into BUCK at the ask, or a dollar debt paid by
    # selling BUCK at the bid.
    numerator, denominator = ask if usd >= 0 else bid
    worth_buck = _tdiv(scaled, Q) + _tdiv(usd * denominator, numerator)
    return {"mark_block": block, "cash": cash[owner], "aux_cash": sum(cash.values()) - cash[owner],
            "held_buck": bucks[owner] // Q, "savings_held": bucks.get(receipt_owner, 0) // Q if savings else 0,
            "wallet_buck": bucks[wallet.address] // Q if wallet else 0,
            "drawn": book.state.issued, "limit": book.credit_limit,
            "deposit": book.state.premium_nominal, "premium_net": book.premium_net_scaled // Q,
            "insurance_age": book.state.premium_nominal - book.premium_net_scaled // Q,
            "jub": None if book.relief_scaled is None else book.relief_scaled // Q,
            "relief_scaled": None if book.relief_scaled is None else str(book.relief_scaled),
            "relief_unavailable": book.relief_unavailable,
            "relief_liquidity_limited": book.exceeds_liquidity_limit,
            "available_relief_scaled": str(book.available_relief_scaled),
            "basket": position.basis, "basket_in": position.basis, "basket_at_risk": position.basis,
            "basket_shares": str(sum(p.shares for p in position.positions)),
            "basket_mark": position.mark, "basket_receipts": len(position.positions), "basket_roi": position.roi,
            "pending_usd": pending, "nw": usd + valued, "nw_buck": worth_buck}
