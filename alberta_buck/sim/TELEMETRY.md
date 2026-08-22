# Per-agent telemetry (schema v1)

The language-neutral contract between the Python simulation and any
consumer -- the matplotlib plots, analysis scripts, and the JS+SVG
dashboards fed by the sim server.  A dashboard needs three things per
agent: who it is (identity + resolved knobs), what it holds, and how it
is doing (P&L).  This file defines exactly where those live in a sim
vector / frame stream.  It is written for a reader who has never opened
the Python.

## Where telemetry lives

A sim vector is one JSON document:

```json
{"tokens": [...], "decimals": [...],
 "meta":   {..., "telemetry": {...}},        <- static, ONCE
 "frames": [{"day": 0, ..., "ag": {...}},    <- per-frame records
            ...]}
```

The sim server's `ws://.../s/<sid>/frames` channel streams the same
frame objects one per message, so a live dashboard and a saved vector
read identically.  Frames are also checkpointed to disk every 25
simulated days, so a partial run is always readable.

## `meta.telemetry` -- the roster (static, emitted once)

```json
{"version": 1,
 "units": "usd6",
 "agents": [
   {"id": "ExcursionArbAgent#0",
    "cls": "ExcursionArbAgent",
    "idx": 0,
    "stride": 1,
    "knobs": {"capital": "usdc", "entry_dev": 0.03, "exit_dev": 0.01,
              "min_edge": 0.01, "halflife": 5.0, "max_impact_bp": 112,
              "budget": 4123456789, "face": 0, "nw0": 4123456789}},
   ...]}
```

- `id` -- stable agent identity, `"<class>#<index>"`, where index is the
  agent's ordinal in the WHOLE population (not per-class), e.g.
  `"ExcursionArbAgent#285"`.  The key used in every frame's `ag` object;
  never parse it -- match it against the roster.
- `stride` -- this agent emits a per-frame record only on days where
  `day % stride == 0`.
- `knobs` -- the RESOLVED per-agent parameters (drawn values, not the
  configuration ranges).  Class-specific; see the class tables below.
- `units: "usd6"` -- unless stated otherwise, monetary fields are
  integers in 6-decimal micro-USD (1_000_000 == $1).  BUCK uses the
  same 6-decimal convention (1 BUCK == $1 at par).

Only classes that opt in appear.  Large background populations (the
DirectMint depositor crowd, arbs, whales-as-market-makers) emit nothing
and cost nothing.

## `frames[i].ag` -- the per-frame records

```json
"ag": {"ExcursionArbAgent#0": {"u": 3990000000, "b": 120000000,
                               "s": 120000000, "nw": 4110000000,
                               "ewma": 1.0312, "basis": 130000000},
       "WhaleRaidAgent#0": {"u": ..., "b": ..., "s": ..., "nw": ...,
                            "phase": 2, "accum": ..., "dump": ...,
                            "reacq": ..., "target": ..., "pnl": ...}}
```

The `ag` key is present only when at least one agent was due; an agent
id is present only on its stride days.  Consumers must treat both as
sparse (the `alberta_buck.sim.telemetry.load()` accessor aligns series
with explicit `null`s).

### Common fields (every emitting class)

| field | meaning |
|-------|---------|
| `u`   | USDC held |
| `b`   | `Buck.balanceOf` -- spendable BUCK.  NB: includes unused K-scaled credit headroom, by the contract's own semantics |
| `s`   | `Buck.signedBalanceOf` -- negative = drawn credit (an outstanding claim on own assets) |
| `nw`  | par-marked net worth, `u + s` |

### Class extras

**ExcursionArbAgent** and its pinned variants (ExcursionCreditArbAgent,
ExcursionBasketArbAgent, ExcursionBuckArbAgent) -- knobs: `neutral`
("usdc"|"basket"|"credit"; `capital` is the legacy alias), `buck_frac`
(rest-state BUCK share of the initial wealth), `entry_dev`, `exit_dev`,
`cover_dev` (credit base buyback gate), `min_edge`, `halflife`,
`fast_halflife`, `max_impact_bp`, `budget`, `face`, `base_buck`, `nw0`.
Frame extras: `ewma`/`fast` (the two bvib filters, float), `basis`
(neutral committed to the open long), `pos` (= `b` - rest-state base:
+ long, - short), and for the basket base `bk` (basket holdings in PAR
units, usd6: TOKEN value at pool prices / bvib; `nw` includes it).
Population P&L = sum of (`nw` - `nw0`).  Frame-level `exc_q` =
[absorb, retire, supply, issue] cumulative directional volumes (usd6)
of the whole excursion population.

**WhaleRaidAgent** -- knobs: `budget`, `side` ("sell" = dump =
discount excursion | "buy" = squeeze = premium excursion),
`accum_days`, `reacquire_days`, `raid_day`, `raid_days` (1 = raid
speed, N = grind).  Frame extras: `phase` (0 idle, 1 accumulate,
2 inject, 3 unwind, 4 done), `accum`/`dump`/`reacq` (sell-side USDC
legs), `raid`/`unwind` (buy-side USDC legs), `target` (BUCK position
to rebuild / unwind), `pnl` (sell: `dump` - `reacq`; buy: `unwind` -
`raid`).  Frame-level `raid_side` carries the sign.

**CommodityRebalArbAgent** -- knobs: `budget`, `halflife`, `band`,
`min_edge`, `foresight_days`, `max_impact_bp`.  Frame record (no
common fields: an EOA holding TOKEN): `inv` (inventory value at pool
prices, raw BUCK), `hold` (the initial inventory at today's prices),
`pnl` (= `inv` - `hold`: the rebalancing premium/penalty vs
buy-and-hold), `trades`.  Frame-level `crb_pnl` / `crb_trades` sum
the class.

**BuckCreditDebtorAgent** (stride 4) -- knobs: `theta`, `apr`,
`pattern`, `face` (insured value), `mortgage0`, `income`, `payment`,
`premium_bp`, `refi_mode`, `arrive_day`.  Frame record replaces the
common fields with the debtor ledger: `u` (cash), `nw` (chain-truth net
worth incl. basket deposit, net of mortgage and drawn less Jubilee
relief), `mortgage` (remaining fiat), `drawn`, `jub`, `basket`,
`deploys`, `throttled`, `hypo` (counterfactual cash - mortgage),
`active` (0|1).

**SaverAgent** -- knobs: `base_rate` (per-day), `disc_gain`,
`prem_gain`, `reserve_frac`, `budget`, `savings_goal`, `arrive_day`.
Frame extras: `spent` (net USDC deployed into BUCK).

## Sampling rules and size math

Emission is spaced by the class attribute `TELEMETRY_STRIDE` (days).
The rule of thumb for new classes: populations larger than ~16 should
set `stride >= ceil(count / 16)`.

A common record serialises to ~110-150 bytes.  The current featured
cast at full cadence (1827 daily frames):

    8 excursion + 1 raid + 4 savers   @ stride 1  ~= 13 rec/frame
    24-30 debtors                     @ stride 4  ~=  7 rec/frame
    -> ~20 records/frame * ~140 B ~= 2.8 kB/frame
    -> ~5.1 MB over 1827 frames, on a 25-30 MB vector  (~ +20%)

Worst tolerated case (the <2x budget): ~75 records/frame ~= 19 MB.
Emitting 300 agents at stride 1 (~65 MB) is out of budget -- set
strides.

## Versioning

`meta.telemetry.version` is bumped on breaking changes to this layout.
A vector without `meta.telemetry` predates the schema (version 0);
`telemetry.load()` returns `{"version": 0, "agents": {}}` for it.

## Python access

    from alberta_buck.sim.telemetry import load
    t = load("test/vectors/<vector>.json")
    a = t["agents"]["ExcursionArbAgent#0"]
    a["meta"]["knobs"]; a["days"]; a["series"]["nw"]

## Opting a class in (Python side)

Implement both methods (see `Agent` in `agents.py`):

    TELEMETRY_STRIDE = 1                  # or >= ceil(count/16)
    def telemetry_static(self) -> dict    # resolved knobs, once
    def telemetry(self, d) -> dict        # per-frame record

`_ProxyAgent._telemetry_common(d)` supplies the common u/b/s/nw block.
Return None from either to stay silent.  Telemetry must never alter
economic behavior, defaults, or rng draw order.

## Keyed RNG (`[scenario] rng = "keyed"`) -- the draw contract

The portcast arms replace Python's Mersenne agent RNG with a
language-neutral keyed-hash stream so a JS port can reproduce every draw
exactly (implementation: `alberta_buck/sim/rng.py`, class `KeyedRandom`).

Recipe (all integers big-endian, blake2b = RFC 7693, unkeyed):

    key   = blake2b( seed_be32 || utf8(class_name) || idx_be8,
                     digest_size = 16 )                     # 16 bytes
    h(n)  = blake2b( key || n_be8, digest_size = 8 )        # n = 0,1,2,...
    x(n)  = ( u64(h(n)) >> 11 ) * 2**-53                    # float64 in [0,1)

- `seed` is the scenario seed (e.g. 0xA1BC); `idx` is the agent's global
  population ordinal (the roster id ordinal), EXCEPT the DirectMint
  family, which keys with the literal class name "DirectMintAgent" /
  "DirectMintBuckAgent" and the per-family sequence counter `_seq`
  (ArrivingDMAgent shares the "DirectMintAgent" stream family).
- Every supported method consumes exactly ONE x(n), in call order:
  `random() = x`; `uniform(a,b) = a + (b-a)*x`;
  `randint(a,b) = a + floor(x*(b-a+1))` (inclusive);
  `randrange(n) = floor(x*n)`; `randrange(a,b) = a + floor(x*(b-a))`;
  `choice(seq) = seq[randrange(len(seq))]`.
  All derived arithmetic is plain float64 -- reproducible bit-for-bit in
  JS (`Number`); recover the u64 from the 8 digest bytes via BigInt, then
  `Number(u64 >> 11n) * 2**-53`.
- Knob draws happen in each class's setup() in source order; in-run
  randomness continues the same stream.  A port must make the same calls
  in the same order to stay on-stream -- golden-scenario vectors are the
  check, and any unsupported method is deliberately absent from
  KeyedRandom so a gap fails loudly instead of diverging silently.
- The loop's world machinery (whale scheduling, identity nonces) is NOT
  on this contract; it stays server-side.
