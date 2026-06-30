"""Inspectable tree: maps live sim state to selectable summary/detail views.

The TUI never reaches into contracts or agents directly.  Instead the whole
sim is presented as a tree of `Node`s, each of which knows how to render a
one-line `summary()` (for the navigable list) and a multi-line `details()`
(for the detail pane), given the live `SimDriver`.

This is the extension seam: to surface a new component or a new agent field,
add a `Node` (or extend a builder below) -- the curses front-end is agnostic
to what it is displaying.

Every chain read goes through `drv.proxy` (see proxy.py): a memoized, async
read-through cache.  Renders return last-known values instantly and the
underlying `eth_call`s happen off the UI thread, so navigation stays snappy.

All BUCK/USDC magnitudes are 6-dec (E6); buckK is 18-dec (E18); per-token
amounts use that token's decimals (`d.dec[i]`).
"""

from __future__ import annotations

from typing import Callable

E6 = 10 ** 6
E18 = 10 ** 18


# --------------------------------------------------------------------------- #
#  Node: the one abstraction the UI consumes.
# --------------------------------------------------------------------------- #

class Node:
    """A selectable row.  Leaves render component/agent state; group nodes
    hold children and can be expanded/collapsed in the list.

    `summary`/`details` are callables of the live `SimDriver` so every render
    reflects current chain state without rebuilding the tree.
    """

    def __init__(self, key: str, label: str,
                 summary: Callable[..., str] | None = None,
                 details: Callable[..., list[str]] | None = None,
                 children: list["Node"] | None = None,
                 expanded: bool = False):
        self.key = key
        self.label = label
        self._summary = summary
        self._details = details
        self.children = children or []
        self.expanded = expanded

    @property
    def is_group(self) -> bool:
        return bool(self.children)

    def summary(self, drv) -> str:
        try:
            return self._summary(drv) if self._summary else ""
        except Exception as e:                       # never let a read crash UI
            return f"<read error: {e!r}>"[:80]

    def details(self, drv) -> list[str]:
        try:
            return self._details(drv) if self._details else [self.label]
        except Exception as e:
            return [self.label, "", f"read error: {e!r}"]


# --------------------------------------------------------------------------- #
#  Small formatting + read helpers.
# --------------------------------------------------------------------------- #

def _short(addr: str) -> str:
    return addr[:6] + ".." + addr[-4:] if addr and len(addr) > 12 else (addr or "-")


def _usd(v: int) -> str:
    return f"${v / E6:,.2f}"


# All reads go through the async proxy (drv.proxy); they return last-known
# values immediately and refresh in the background.

def _bal(drv, token, holder) -> int:
    return drv.proxy.balance_of(token, holder)


def _view(drv, contract, fn, *args, default=0):
    return drv.proxy.view(contract, fn, *args, default=default)


def _implied(drv, pool, token, dec: int, quote) -> int:
    """Implied quote-units per 1 whole token from live pool reserves."""
    rt = _bal(drv, token, pool)
    rq = _bal(drv, quote, pool)
    return rq * (10 ** dec) // rt if rt else 0


def _pool_price_buck(drv, i: int) -> int:
    """Implied BUCK per whole token from the TOKEN/BUCK pool (0 if empty).

    BUCK is 6-dec, so the result is 6-dec BUCK-per-token (divide by E6 to
    display whole BUCK)."""
    d = drv.d
    rt = _bal(drv, d.tokens[i], d.pool_buck[i])
    rb = _bal(drv, d.buck, d.pool_buck[i])
    return rb * (10 ** d.dec[i]) // rt if rt else 0


def _buck_usd(drv) -> int:
    """Implied USDC-micro per 1 BUCK from the floating BUCK/USDC pool."""
    d = drv.d
    if not d.pool_ub:
        return 0
    ru = _bal(drv, d.usdc, d.pool_ub)
    rb = _bal(drv, d.buck, d.pool_ub)
    return ru * E6 // rb if rb else 0


def _basket_nav(drv) -> int:
    """Total BUCK value (6-dec) of every TOKEN/BUCK LP position (both sides)."""
    d = drv.d
    total = 0
    for i in range(len(d.tokens)):
        rt = _bal(drv, d.tokens[i], d.pool_buck[i])
        rb = _bal(drv, d.buck, d.pool_buck[i])
        if rt == 0 or rb == 0:
            continue
        p = rb * (10 ** d.dec[i]) // rt          # 6-dec BUCK per whole token
        total += rt * p // (10 ** d.dec[i]) + rb  # token side + BUCK side
    return total


def _pool_value_weights(drv) -> list[tuple[float, float]]:
    """Per-token (actual_weight, target_weight) from the TOKEN/BUCK pools.

    Target = basketAmount*initPx^2/spot (a fixed-quantity index holds less
    BUCK value of a token as it appreciates); actual = tokenReserve*spot.
    Both normalised to sum to 1.0."""
    d = drv.d
    N = len(d.tokens)
    prices = [_pool_price_buck(drv, i) for i in range(N)]
    target_val = []
    for i in range(N):
        con = _view(drv, d.basket, "constituents", i, default=None)
        ba = con[2] if con else 0          # basketAmount
        ip = con[3] if con else 0          # initialPriceInBuck
        base = ba * ip // (10 ** 18) if ba and ip else 0
        target_val.append(base * ip // prices[i] if base and prices[i] else base)
    tv = sum(target_val)
    actual_val = [
        _bal(drv, d.tokens[i], d.pool_buck[i]) * prices[i] // (10 ** d.dec[i])
        if prices[i] else 0
        for i in range(N)
    ]
    av = sum(actual_val)
    return [(actual_val[i] / av if av else 0.0,
             target_val[i] / tv if tv else 0.0) for i in range(N)]


# --------------------------------------------------------------------------- #
#  Component nodes.
# --------------------------------------------------------------------------- #

def _overview_node() -> Node:
    def summ(drv):
        c = drv.ctr
        return (f"day {drv.day}/{drv.days} t{drv.tick}/{drv.ticks_per_day}  "
                f"cyc {c.get('cycleTrades', 0)}  reb {c.get('rebalanceTrades', 0)}")

    def det(drv):
        d, c = drv.d, drv.ctr
        rt = c.get("dmRoundTrips", 0)
        dd = c.get("dmDollarDays", 0)
        apr = (365.0 * c.get("dmProfitUsd", 0) / dd) if dd else 0.0
        return [
            f"Scenario       {drv.scenario.name}  ({drv.basket_impl} basket)",
            f"Progress       day {drv.day}/{drv.days}  tick {drv.tick}/{drv.ticks_per_day}"
            f"   ({100 * drv.progress:.1f}%)",
            f"Agents         {len(drv.agents)}  ({len(drv.arbs)} arb/DM, "
            f"{len(drv.whales)} whale)",
            "",
            "-- trade activity --",
            f"BUCK-routed cycles    {c.get('cycleTrades', 0):>8}",
            f"  via BUCK/USDC pool  {c.get('ubTrades', 0):>8}",
            f"whale TOKEN/USDC snaps{c.get('directTrades', 0):>8}",
            f"rebalance trades      {c.get('rebalanceTrades', 0):>8}",
            f"arb throughput        {_usd(c.get('cycleVolumeUsdc', 0)):>12}",
            "",
            "-- direct-mint depositors --",
            f"entries / exits       {c.get('dmEntries', 0)} / {c.get('dmExits', 0)}"
            f"  (fails {c.get('dmExitFails', 0)})",
            f"outstanding BUCK      {_usd(c.get('dmOutstandingBuck', 0))}",
            f"treasury BUCK         {_usd(c.get('treasuryBuck', 0))}",
            f"realized return       {100 * apr:+.2f}% APR  "
            f"({rt} round-trips, {_usd(c.get('dmProfitUsd', 0))} profit)",
        ]

    return Node("overview", "Overview", summ, det)


def _buck_node() -> Node:
    def summ(drv):
        d = drv.d
        supply = _view(drv, d.buck, "totalSupply")
        return f"supply {supply / E6:,.0f}   1 BUCK = {_usd(_buck_usd(drv))}"

    def det(drv):
        d = drv.d
        supply = _view(drv, d.buck, "totalSupply")
        out = [
            f"Buck (ERC-20)  {_short(d.buck.address)}",
            f"total supply   {supply / E6:,.2f} BUCK",
            "",
            "-- floating BUCK/USDC pool (not a peg) --",
            f"address        {_short(d.pool_ub)}" if d.pool_ub else "  (no pool)",
        ]
        if d.pool_ub:
            rb = _bal(drv, d.buck, d.pool_ub)
            ru = _bal(drv, d.usdc, d.pool_ub)
            out += [
                f"reserves       {rb / E6:,.0f} BUCK / {ru / E6:,.0f} USDC",
                f"implied price  1 BUCK = {_usd(_buck_usd(drv))}",
            ]
        return out

    return Node("buck", "BUCK", summ, det)


def _controller_node() -> Node:
    def summ(drv):
        d = drv.d
        k = _view(drv, d.kctrl, "buckK")
        bv = _view(drv, d.basket, "basketValueInBuck")
        return f"K {k / E18:.5f}   index {bv / E18:.4f}"

    def det(drv):
        d = drv.d
        k = _view(drv, d.kctrl, "buckK")
        bv = int(_view(drv, d.basket, "basketValueInBuck"))
        return [
            f"BuckKController {_short(d.kctrl.address)}",
            f"buckK          {k / E18:.6f}  (18-dec PID output)",
            f"basket index   {bv / E18:.6f}  (18-dec; ~1.0 at init)",
            "",
            "buckK is the PID controller's stabilization multiplier over the",
            "commodity basket index value; settled once per day via compute().",
        ]

    return Node("controller", "Controller", summ, det)


def _basket_node() -> Node:
    def summ(drv):
        nav = _basket_nav(drv)
        treas = drv.ctr.get("treasuryBuck", 0)
        return f"NAV {nav / E6:,.0f}   treasury {treas / E6:,.0f}"

    def det(drv):
        d = drv.d
        nav = _basket_nav(drv)
        c = drv.ctr
        weights = _pool_value_weights(drv)
        out = [
            f"{'BuckBasketProRata' if d.basket_impl == 'prorata' else 'BuckBasket'}"
            f"  {_short(d.basket.address)}",
            f"impl           {d.basket_impl}"
            + (f"   venue {_short(d.venue.address)}" if d.venue else ""),
            f"NAV            {nav / E6:,.2f} BUCK  (all TOKEN/BUCK LP)",
            f"treasury BUCK  {_usd(c.get('treasuryBuck', 0))}  (retained profit)",
            f"outstanding    {_usd(c.get('dmOutstandingBuck', 0))}  (depositor liability)",
            "",
            "constituent     init $    poolPx(BUCK)   actual/target wt",
        ]
        for i in range(len(d.tokens)):
            sym = drv.scenario.tokens[i][0]
            init = drv.scenario.prices.day0(i)        # 6-dec USDC/token at t0
            px = _pool_price_buck(drv, i)             # 6-dec BUCK/token (live)
            aw, tw = weights[i]
            out.append(
                f"  {sym:<6} {init / E6:>9,.2f} {px / E6:>13,.4f}   "
                f"{aw:0.3f} / {tw:0.3f}")
        return out

    return Node("basket", "Basket", summ, det)


def _token_node(i: int) -> Node:
    def summ(drv):
        d, s = drv.d, drv.scenario
        ref = s.prices.ref(i, drv.day)
        su = _implied(drv, d.pool_usdc[i], d.tokens[i], d.dec[i], d.usdc)
        err = (su - ref) / ref if ref else 0.0
        aw, tw = _pool_value_weights(drv)[i]
        return (f"spot {_usd(su)} ref {_usd(ref)} ({err:+.1%})  "
                f"wt {aw:.3f}/{tw:.3f}")

    def det(drv):
        d, s = drv.d, drv.scenario
        sym = s.tokens[i][0]
        ref = s.prices.ref(i, drv.day)
        su = _implied(drv, d.pool_usdc[i], d.tokens[i], d.dec[i], d.usdc)
        err = (su - ref) / ref if ref else 0.0
        aw, tw = _pool_value_weights(drv)[i]
        pxb = _pool_price_buck(drv, i)
        ut = _bal(drv, d.tokens[i], d.pool_usdc[i])
        uu = _bal(drv, d.usdc, d.pool_usdc[i])
        bt = _bal(drv, d.tokens[i], d.pool_buck[i])
        bb = _bal(drv, d.buck, d.pool_buck[i])
        return [
            f"{sym} ({s.tokens[i][1]})  {d.dec[i]}-dec  {_short(d.tokens[i].address)}",
            "",
            "-- TOKEN/USDC truth pool (whale-snapped to CSV ref) --",
            f"address        {_short(d.pool_usdc[i])}",
            f"reserves       {ut / (10 ** d.dec[i]):,.6g} {sym} / {uu / E6:,.0f} USDC",
            f"spot price     {_usd(su)}   ref {_usd(ref)}   track err {err:+.2%}",
            "",
            "-- TOKEN/BUCK basket pool (direct-mint LP) --",
            f"address        {_short(d.pool_buck[i])}",
            f"reserves       {bt / (10 ** d.dec[i]):,.6g} {sym} / {bb / E6:,.0f} BUCK"
            if bt or bb else "reserves       (empty -- no DM liquidity yet)",
            f"pool price     1 {sym} = {pxb / E6:,.4f} BUCK",
            f"value weight   actual {aw:.4f}   target {tw:.4f}",
            "",
            "target weight = basketAmount*initPx^2/spot (a fixed-quantity index",
            "holds less BUCK value of a token as it appreciates).",
        ]

    return Node(f"token{i}", "", summ, det)


def _identity_node() -> Node:
    def summ(drv):
        d = drv.d
        n = sum(1 for a in drv.agents
                if getattr(a, "is_eoa", False) and a.account is not None)
        return f"{n} registered EOA identities"

    def det(drv):
        d = drv.d
        eoa = [a for a in drv.agents
               if getattr(a, "is_eoa", False) and a.account is not None]
        ok = sum(1 for a in eoa
                 if _view(drv, d.reg, "isVerified", a.address, default=False))
        return [
            f"IdentityRegistry {_short(d.reg.address)}",
            f"issuer           {_short(d.issuer_addr)}",
            f"registered EOAs  {len(eoa)}",
            f"verified         {ok}/{len(eoa)}",
            "",
            "BUCK transfers are identity-gated: a recipient must be public or",
            "have approved the sender.  DM depositors are paid in TOKEN only,",
            "so commodity LPs need no identity binding.",
        ]

    return Node("identity", "Identity", summ, det)


def _components_group(drv) -> Node:
    d = drv.d
    children = [_buck_node(), _controller_node(), _basket_node()]
    for i in range(len(d.tokens)):
        node = _token_node(i)
        node.label = drv.scenario.tokens[i][0]      # symbol as the row label
        children.append(node)
    children.append(_identity_node())
    return Node("components", "COMPONENTS", children=children, expanded=True)


# --------------------------------------------------------------------------- #
#  Agent nodes.
# --------------------------------------------------------------------------- #

def _agent_balances(drv, a) -> tuple[int, list[tuple[int, int]], int]:
    """(usdc, [(tok_idx, raw)...nonzero], buck) for an agent's holder addr."""
    d = drv.d
    holder = a.address
    usdc = _bal(drv, d.usdc, holder)
    buck = _bal(drv, d.buck, holder)
    toks = []
    for i, tc in enumerate(d.tokens):
        raw = _bal(drv, tc, holder)
        if raw:
            toks.append((i, raw))
    return usdc, toks, buck


def _agent_summary(drv, a) -> str:
    d = drv.d
    di = a.deposit_info(d) if hasattr(a, "deposit_info") else None
    state = ""
    if di is not None:
        tok_idx, ptok, pbuck = di
        sym = drv.scenario.tokens[tok_idx][0] if tok_idx is not None else "?"
        if ptok > 0:
            state = f"  IN {ptok / (10 ** d.dec[tok_idx]):,.4g} {sym}"
        else:
            state = f"  IN {_usd(pbuck)} BUCK"
    elif getattr(a, "_exited", False):
        state = "  (exited)"
    usdc = _bal(drv, d.usdc, a.address)
    return f"{_short(a.address)}  USDC {usdc / E6:,.0f}{state}"


def _agent_details(drv, a) -> list[str]:
    d = drv.d
    usdc, toks, buck = _agent_balances(drv, a)
    out = [
        f"{type(a).__name__}-{a.idx}",
        f"address        {a.address}",
        f"is_eoa         {getattr(a, 'is_eoa', True)}",
        "",
        "-- balances --",
        f"USDC           {usdc / E6:,.2f}",
        f"BUCK           {buck / E6:,.2f}",
    ]
    for i, raw in toks:
        sym = drv.scenario.tokens[i][0]
        out.append(f"{sym:<14} {raw / (10 ** d.dec[i]):,.6g}")
    if not toks:
        out.append("(no TOKEN balances)")

    di = a.deposit_info(d) if hasattr(a, "deposit_info") else None
    if di is not None or getattr(a, "_receipt_id", None) is not None:
        out += ["", "-- basket deposit --"]
        rid = getattr(a, "_receipt_id", None)
        out.append(f"receiptId      {rid}")
        if di is not None:
            tok_idx, ptok, pbuck = di
            sym = drv.scenario.tokens[tok_idx][0] if tok_idx is not None else "?"
            out += [
                f"token          {sym}",
                f"principalTok   {ptok / (10 ** d.dec[tok_idx]):,.6g}"
                if tok_idx is not None else f"principalTok   {ptok}",
                f"principalBuck  {_usd(pbuck)}",
            ]
        dvu = getattr(a, "_deposit_value_usd", 0)
        if dvu:
            out += [
                f"deposit value  {_usd(dvu)}  (day {getattr(a, '_deposit_day', 0)})",
            ]
    # Rebalancer cached basket amounts, if present.
    if hasattr(a, "_basket_amount") and a._basket_amount:
        out += ["", "-- rebalancer targets --"]
        for i, ba in enumerate(a._basket_amount):
            sym = drv.scenario.tokens[i][0]
            out.append(f"basketAmount[{sym}] {ba / (10 ** d.dec[i]):,.6g}")
    return out


def _agent_node(a) -> Node:
    return Node(
        f"agent-{type(a).__name__}-{a.idx}",
        f"{type(a).__name__[:3].lower()}-{a.idx}",
        summary=lambda drv, a=a: _agent_summary(drv, a),
        details=lambda drv, a=a: _agent_details(drv, a),
    )


def _agent_group(cls_name: str, members: list) -> Node:
    def summ(drv):
        n = len(members)
        # Cheap python-state aggregate (no per-member chain reads).
        entered = sum(1 for a in members if getattr(a, "_entered", False))
        exited = sum(1 for a in members if getattr(a, "_exited", False))
        if any(hasattr(a, "_entered") for a in members):
            return f"x{n}   entered {entered}   exited {exited}"
        return f"x{n}"

    def det(drv):
        d = drv.d
        n = len(members)
        entered = sum(1 for a in members if getattr(a, "_entered", False))
        exited = sum(1 for a in members if getattr(a, "_exited", False))
        out = [
            f"{cls_name}",
            f"count          {n}",
        ]
        if any(hasattr(a, "_entered") for a in members):
            active = [a for a in members if getattr(a, "_entered", False)]
            out += [
                f"entered now    {entered}",
                f"exited (ever)  {exited}",
            ]
            # Outstanding principal across this class's active deposits.
            out_buck = sum(getattr(a, "_principal_buck", 0) for a in active)
            out.append(f"outstanding    {_usd(out_buck)} BUCK")
        out += ["", "(expand to inspect individual agents)"]
        return out

    children = [_agent_node(a) for a in members]
    # Default-collapse big populations; expand small ones for at-a-glance use.
    return Node(f"group-{cls_name}", cls_name, summ, det,
                children=children, expanded=len(members) <= 4)


# Stable display order for agent classes (most "system" first).
_CLASS_ORDER = [
    "MarketMakerWhale", "BuckBasketRebalancerAgent", "AnonymousArbAgent",
    "TokenAccumulatorAgent", "BootstrapDMAgent", "DirectMintAgent",
    "DirectMintBuckAgent",
]


def _agents_group(drv) -> Node:
    by_cls: dict[str, list] = {}
    for a in drv.agents:
        by_cls.setdefault(type(a).__name__, []).append(a)
    ordered = sorted(
        by_cls, key=lambda c: (_CLASS_ORDER.index(c)
                               if c in _CLASS_ORDER else len(_CLASS_ORDER), c))
    children = [_agent_group(c, by_cls[c]) for c in ordered]
    return Node("agents", f"AGENTS ({len(drv.agents)})",
                children=children, expanded=True)


# --------------------------------------------------------------------------- #
#  Tree assembly + flattening.
# --------------------------------------------------------------------------- #

def build_tree(drv) -> Node:
    """Construct the inspector tree for a live `SimDriver`.

    The tree's *structure* is built once; its *content* is recomputed live
    by the per-node summary/detail closures on every render.
    """
    root = Node("root", "root", expanded=True, children=[
        _overview_node(),
        _components_group(drv),
        _agents_group(drv),
    ])
    return root


def flatten(root: Node) -> list[tuple[int, Node]]:
    """Visible (depth, node) rows honoring each group's `expanded` flag.

    The root itself is not shown; its children render at depth 0.
    """
    rows: list[tuple[int, Node]] = []

    def walk(node: Node, depth: int) -> None:
        for child in node.children:
            rows.append((depth, child))
            if child.is_group and child.expanded:
                walk(child, depth + 1)

    walk(root, 0)
    return rows
