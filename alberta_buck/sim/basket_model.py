"""Free-standing basket-model simulation -- no anvil, pure CPMM math.

Demonstrates the BuckBasket economic mechanics:
  * Direct-mint agents deposit tokens, mint BUCK, LP into underweight pools
  * External arb agents keep pool prices tracking reference prices
  * Redemptions withdraw from overweight pools ("sell high")
  * BUCK flows between pools as arbs rebalance
  * Treasury compounding from retained profit

Pools are constant-product (x*y=k) — a good approximation of full-range V3.
Prices follow a GBM walk; arbs trade against reference vs pool price deltas.

Output: test/vectors/basket-model.json  (compatible with plot_basket_model.py)
"""

from __future__ import annotations

import json, math, random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = REPO / "test" / "vectors" / "basket-model.json"
PLOT = REPO / "images" / "basket-model.png"

E18 = 10 ** 18

# ── configuration ───────────────────────────────────────────────────────

TOKENS = [
    # (sym, decimals, init_price_usd, drift, vol, basket_weight_bp)
    ("PAXG",  18, 2600.0,  0.08, 0.16, 3333),
    ("cbBTC",  8, 65000.0, 0.15, 0.55, 3333),
    ("AOIL", 18,   78.0,  0.02, 0.35, 3334),  # 3334 to sum to 10000
]

DAYS = 365
TICKS_PER_DAY = 4
SEED = 0xA1BC

# Agent counts
N_ARBS = 6          # external BUCK-aware arb agents
N_DM = 8            # direct-mint agents (staggered entry)
DM_SEED_USD = 2e6   # $2M per DM agent
DM_HOLD_MIN = 90    # minimum hold (days)
DM_HOLD_MAX = 180

# Arb parameters
ARB_POOL_FRAC = 0.01   # trade at most 1% of pool depth
ARB_MARGIN = 0.006     # 60bp required edge
ARB_CAPITAL = 1e7      # $10M per arb agent (USDC)

# Rebalancing (P-controller: proportional to value-weight error)
BASKET_TOTAL_BP = 10000
TREASURY_RETAIN = 0.50  # treasury keeps 50% of profit BUCKs
KP = 1.0                # P-gain: fraction of deviation acted on per tick


# ── price simulation ────────────────────────────────────────────────────

def generate_prices(days: int, seed: int) -> list[list[float]]:
    """GBM daily close prices (USD) for each token."""
    rng = random.Random(seed)
    out = []
    for sym, dec, start, mu, sigma, _ in TOKENS:
        dt = 1.0 / 365.25
        series = [start]
        rng2 = random.Random(rng.randint(0, 2**31 - 1))
        for _ in range(1, days):
            z = rng2.gauss(0.0, 1.0)
            series.append(series[-1] * math.exp(
                (mu - 0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * z))
        out.append(series)
    return out


# ── pool (constant-product AMM) ─────────────────────────────────────────

@dataclass
class CPMMPool:
    token: str
    dec: int
    tok_res: float = 0.0     # raw token units
    buck_res: float = 0.0    # raw BUCK (18-dec equivalent)

    @property
    def price(self) -> float:
        """BUCK per 1 whole token."""
        if self.tok_res == 0:
            return 0.0
        return self.buck_res / (self.tok_res / (10 ** self.dec))

    @property
    def k(self) -> float:
        return self.tok_res * self.buck_res

    def swap_exact_in(self, token_in: str, amount_in: float) -> float:
        """Return amount_out for exact-input swap.  amount_in is raw units."""
        if token_in == "BUCK":
            ri, ro = self.buck_res, self.tok_res
        else:
            ri, ro = self.tok_res, self.buck_res
        fee = 0.997  # 0.3% fee
        eff = amount_in * fee
        amount_out = (ro * eff) / (ri + eff)
        # Update reserves
        if token_in == "BUCK":
            self.buck_res += amount_in
            self.tok_res -= amount_out
        else:
            self.tok_res += amount_in
            self.buck_res -= amount_out
        return amount_out

    def add_liquidity(self, tok_amt: float, buck_amt: float) -> float:
        """Add (tok_amt, buck_amt) to the pool. Returns L added."""
        self.tok_res += tok_amt
        self.buck_res += buck_amt
        return math.sqrt(tok_amt * buck_amt)

    def remove_liquidity(self, frac: float) -> tuple[float, float]:
        """Remove `frac` of total LP. Returns (tok_out, buck_out)."""
        tok_out = self.tok_res * frac
        buck_out = self.buck_res * frac
        self.tok_res -= tok_out
        self.buck_res -= buck_out
        return tok_out, buck_out


# ── agents ──────────────────────────────────────────────────────────────

@dataclass
class ArbAgent:
    """BUCK-aware arb: holds USDC + all TOKENs.  Trades pool vs reference."""
    usdc: float = 0.0
    tokens: dict[str, float] = field(default_factory=dict)

    def act(self, pools: dict[str, CPMMPool], ref_prices: list[float],
            tok_syms: list[str], rng: random.Random):
        """Find pool-vs-reference discrepancies and trade to close them."""
        for i, sym in enumerate(tok_syms):
            pool = pools[sym]
            p_pool = pool.price
            p_ref = ref_prices[i]
            if p_pool == 0 or p_ref == 0:
                continue
            # Price discrepancy: pool vs reference (BUCK per token)
            # If pool is cheap (p_pool < p_ref): buy token with BUCK
            # If pool is expensive (p_pool > p_ref): sell token for BUCK
            dev = abs(p_pool - p_ref) / p_ref
            if dev < ARB_MARGIN:
                continue
            # Size: fraction of pool, capped at agent balance
            amt = min(pool.tok_res, pool.buck_res / p_ref) * ARB_POOL_FRAC
            amt = min(amt, self.tokens.get(sym, 0) * 0.5)  # cap at 50% holdings
            if amt == 0:
                continue
            if p_pool > p_ref:
                # Pool is expensive: sell token for BUCK
                tok_raw = amt
                if self.tokens.get(sym, 0) < tok_raw:
                    continue
                buck_out = pool.swap_exact_in(sym, tok_raw)
                self.tokens[sym] = self.tokens.get(sym, 0) - tok_raw
                # BUCK received; immediately buy USDC (simplification: hold BUCK)
                # For now just track BUCK received
            else:
                # Pool is cheap: buy token with BUCK
                buck_raw = amt * p_ref
                tok_out = pool.swap_exact_in("BUCK", buck_raw)
                self.tokens[sym] = self.tokens.get(sym, 0) + tok_out


@dataclass
class DMDeposit:
    token: str
    principal_tok: float      # raw token units deposited
    principal_buck: float      # BUCK minted
    pool: str                  # which pool LP'd into
    entry_day: int
    exit_day: int
    exited: bool = False


# ── P-controller allocation ────────────────────────────────────────────

def _weight_errors(pools, tok_syms, tok_dec, basket_amount):
    """Return {sym: error} where error = actualWeight - targetWeight.
    Positive = overweight (sell), negative = underweight (buy)."""
    prices = {s: pools[s].price for s in tok_syms}
    target_val = {s: basket_amount[s] * prices[s]
                  for s in tok_syms if prices[s] > 0}
    tv_sum = sum(target_val.values())
    target_w = {s: v / tv_sum for s, v in target_val.items()} if tv_sum else {}
    actual_val = {s: pools[s].tok_res / (10 ** tok_dec[s]) * prices[s]
                  for s in tok_syms if prices[s] > 0}
    av_sum = sum(actual_val.values())
    actual_w = {s: v / av_sum for s, v in actual_val.items()} if av_sum else {}
    return {s: actual_w.get(s, 0) - target_w.get(s, 0) for s in tok_syms}


def _alloc_fractions(errors, direction: str):
    """Proportional allocation fractions across pools.
    direction='sell': only positive errors (overweight pools).
    direction='buy': only negative errors (underweight pools).
    Returns {sym: fraction} summing to 1.0 (or 0 if no eligible pools)."""
    if direction == 'sell':
        vals = {s: max(0.0, e) for s, e in errors.items()}
    else:
        vals = {s: max(0.0, -e) for s, e in errors.items()}
    total = sum(vals.values())
    if total == 0:
        return {}
    return {s: v / total for s, v in vals.items()}


# ── simulation ──────────────────────────────────────────────────────────

def run():
    rng = random.Random(SEED)
    tok_syms = [t[0] for t in TOKENS]
    tok_dec = {t[0]: t[1] for t in TOKENS}

    # Generate reference prices (USD per whole token).
    ref_prices = generate_prices(DAYS, SEED)
    ref_by_sym = {tok_syms[i]: ref_prices[i] for i in range(len(tok_syms))}

    # Create pools (start empty).
    pools: dict[str, CPMMPool] = {
        sym: CPMMPool(token=sym, dec=tok_dec[sym])
        for sym in tok_syms
    }

    # Basket amounts: basketAmount_i = weightBp_i / initialPrice_i.
    # These are static after addBasketToken.  targetVal_i = basketAmount_i * price_i.
    basket_amount = {}
    for i, (sym, dec, start, _, _, w_bp) in enumerate(TOKENS):
        basket_amount[sym] = (w_bp / BASKET_TOTAL_BP) / start

    # Bootstrap: seed each pool with initial LP.
    # Each pool gets ~$100M in TOKEN and equivalent BUCK at reference price.
    INITIAL_BUCK = 100_000_000.0  # $100M BUCK per pool
    for i, sym in enumerate(tok_syms):
        p = pools[sym]
        price0 = ref_prices[i][0]
        tok_raw = INITIAL_BUCK / price0 * (10 ** tok_dec[sym])
        buck_raw = INITIAL_BUCK
        p.add_liquidity(tok_raw, buck_raw)

    # Track BUCK supply.
    total_buck_minted = sum(p.buck_res for p in pools.values())
    treasury_buck = 0.0
    deposits: list[DMDeposit] = []

    # Create arb agents.
    arbs = [ArbAgent() for _ in range(N_ARBS)]
    for a in arbs:
        a.usdc = ARB_CAPITAL
        for sym in tok_syms:
            idx = tok_syms.index(sym)
            price0 = ref_prices[idx][0]
            a.tokens[sym] = ARB_CAPITAL / price0 * (10 ** tok_dec[sym])

    # Schedule DM agents.
    for seq in range(N_DM):
        entry = min(seq, 2) if seq < 3 else 3 + (seq - 3) * 30
        hold = DM_HOLD_MIN + (seq * 37 + 13) % (DM_HOLD_MAX - DM_HOLD_MIN)
        exit_day = min(entry + hold, DAYS)
        # Token: cycle through tokens
        tok_sym = tok_syms[seq % len(tok_syms)]
        deposits.append(DMDeposit(
            token=tok_sym, principal_tok=0, principal_buck=0,
            pool=tok_sym, entry_day=entry, exit_day=exit_day))

    frames = []
    daily_rng = random.Random(SEED)

    for day in range(DAYS):
        # --- DM agent entries/exits (tick 0) ---
        for d in deposits:
            if d.exited:
                continue
            if d.principal_buck == 0 and day >= d.entry_day:
                # Enter: deposit token into most underweight pool.
                sym = d.token
                # Allocate deposit across underweight pools (P-controller).
                errors = _weight_errors(pools, tok_syms, tok_dec, basket_amount)
                alloc = _alloc_fractions(errors, 'buy')
                if not alloc:
                    continue  # no underweight pools — skip entry

                # Total LP value to deposit (in BUCK terms).
                prices = {s: pools[s].price for s in tok_syms}
                lp_buck_value = DM_SEED_USD  # ~$2M per agent

                for tgt_sym, frac in alloc.items():
                    if frac < 0.01:
                        continue
                    tgt_pool = pools[tgt_sym]
                    buck_share = lp_buck_value * frac * KP

                    if tgt_sym == sym:
                        # Direct LP: deposit token + mint BUCK.
                        tok_raw = buck_share / prices[sym] * (10 ** tok_dec[sym])
                        buck_to_mint = tok_raw / (10 ** tok_dec[sym]) * prices[sym]
                        tgt_pool.add_liquidity(tok_raw, buck_to_mint)
                    else:
                        # Swap: deposit token -> BUCK -> target token.
                        tok_raw = buck_share / prices[sym] * (10 ** tok_dec[sym])
                        src_pool = pools[sym]
                        buck_got = src_pool.swap_exact_in(sym, tok_raw)
                        tgt_tok = tgt_pool.swap_exact_in("BUCK", buck_got)
                        buck_to_mint = tgt_tok / (10 ** tok_dec[tgt_sym]) * prices[tgt_sym]
                        tgt_pool.add_liquidity(tgt_tok, buck_to_mint)

                    total_buck_minted += buck_to_mint
                    d.principal_buck += buck_to_mint
                    d.principal_tok += (buck_share / prices[sym] * (10 ** tok_dec[sym]))
                d.pool = ",".join(alloc.keys())  # track all pools

            elif d.principal_buck > 0 and day >= d.exit_day:
                # Exit: redeem from overweight pools (P-controller allocation).
                errors = _weight_errors(pools, tok_syms, tok_dec, basket_amount)
                alloc = _alloc_fractions(errors, 'sell')
                if not alloc:
                    continue  # no overweight pools

                total_out = sum(dp.principal_buck for dp in deposits
                                if dp.principal_buck > 0 and not dp.exited)
                total_redeem = d.principal_buck / total_out if total_out > 0 else 0

                prices = {s: pools[s].price for s in tok_syms}
                for ov_sym, frac in alloc.items():
                    if frac < 0.01:
                        continue
                    ov_pool = pools[ov_sym]
                    # Fraction of THIS pool's LP to withdraw.
                    pool_frac = total_redeem * frac * KP

                    tok_out, buck_out = ov_pool.remove_liquidity(pool_frac)

                    burned = min(buck_out, d.principal_buck * frac)
                    total_buck_minted -= burned

                    profit_buck = buck_out - burned
                    treasury_buck += profit_buck * TREASURY_RETAIN

                    # Reinvest retained BUCK into underweight pools.
                    if profit_buck > 0:
                        reinvest = profit_buck * TREASURY_RETAIN
                        buy_alloc = _alloc_fractions(errors, 'buy')
                        for un_sym, bf in buy_alloc.items():
                            if bf < 0.01:
                                continue
                            un_pool = pools[un_sym]
                            rb = reinvest * bf
                            tgt_tok = un_pool.swap_exact_in("BUCK", rb)
                            new_buck = tgt_tok / (10 ** tok_dec[un_sym]) * prices[un_sym]
                            un_pool.add_liquidity(tgt_tok, new_buck)
                            total_buck_minted += new_buck

                d.exited = True

        # --- External arb (every tick) ---
        for _ in range(TICKS_PER_DAY):
            daily_rng.shuffle(arbs)
            for a in arbs:
                a.act(pools, [ref_prices[i][day] for i in range(len(tok_syms))],
                      tok_syms, daily_rng)

        # --- Snapshot ---
        errors = _weight_errors(pools, tok_syms, tok_dec, basket_amount)
        prices = {s: pools[s].price for s in tok_syms}
        target_val = {s: basket_amount[s] * prices[s]
                      for s in tok_syms if prices[s] > 0}
        tv_sum = sum(target_val.values())
        target_w = {s: v / tv_sum for s, v in target_val.items()} if tv_sum else {}
        actual_val = {s: pools[s].tok_res / (10 ** tok_dec[s]) * prices[s]
                      for s in tok_syms if prices[s] > 0}
        av_sum = sum(actual_val.values())
        actual_w = {s: v / av_sum for s, v in actual_val.items()} if av_sum else {}

        dm_active = sum(1 for dp in deposits if dp.principal_buck > 0 and not dp.exited)
        dm_exited = sum(1 for dp in deposits if dp.exited)
        total_out = sum(dp.principal_buck for dp in deposits
                        if dp.principal_buck > 0 and not dp.exited)

        nav = sum(p.buck_res for p in pools.values())

        frames.append({
            "day": day,
            "refPrices": {s: ref_prices[i][day] for i, s in enumerate(tok_syms)},
            "poolPrices": prices,
            "poolTokRes": {s: pools[s].tok_res for s in tok_syms},
            "poolBuckRes": {s: pools[s].buck_res for s in tok_syms},
            "actualWeights": actual_w,
            "targetWeights": target_w,
            "nav": nav,
            "totalOutstanding": total_out,
            "treasuryBuck": treasury_buck,
            "dmActive": dm_active,
            "dmExited": dm_exited,
            "totalBuckMinted": total_buck_minted,
        })

    # Write output.
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "tokens": tok_syms,
        "frames": frames,
    }, indent=2))
    print(f"[model] wrote {OUT.relative_to(REPO)}  ({len(frames)} frames)")
    print(f"  final treasury: {treasury_buck:,.0f} BUCK"
          f"  active DM: {dm_active}  exited: {dm_exited}")
    return frames


if __name__ == "__main__":
    run()
