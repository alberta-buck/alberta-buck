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
              "equity_upkeep", "v3_factory", "pair_policy", "monetary_policy", "signals")}
    roles.update(jubilee=stack.fund, quoter=stack.quoter)
    addresses = {name: contract.address for name, contract in roles.items() if contract is not None}
    addresses.update(claim_book=d.buck.functions.claimBook().call(), receipt=d.basket.functions.receipt().call())
    libraries = {name: getattr(stack, name).address for name in
                 ("cash_library", "transfer_library", "facilities_library", "renewal_library")}
    libraries["basket_valuation"] = d.net_basket.valuation_library.address
    return {"model": Web3.to_hex(model), "chain_id": d.w3.eth.chain_id, "scale": str(Q),
            "roles": {name: identity(address) for name, address in addresses.items()},
            "libraries": {name: identity(address) for name, address in libraries.items()},
            "jubilee_policy": [str(v) for v in d.wheel.functions.jubileePolicy().call()],
            "basket_nav_basis": "owned net-marked inventory plus cash; unpaid relief reported separately",
            "position_mark_basis": "receipt valueOf at one block, before exit costs",
            "lp_fee_basis": "net-deliverable BUCK and day-zero TOKEN/USD prices"}


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
            if ids or savings is None:
                raise ValueError("ambiguous basket receipt owner")
            owner, ids = savings.address, household_ids
        if not ids:
            continue
        if seen.intersection(ids):
            raise ValueError("duplicate observed basket receipt")
        seen.update(ids)
        positions.append({"agent": f"{type(ag).__name__}#{ag.idx}", **marks(owner, ids)})
    return out, positions
