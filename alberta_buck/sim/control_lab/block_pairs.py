"""Shared real-EVM pair observer and optional basket response.

This campaign deliberately has its own cache. Its six-hour market driver is
not a claim to simulate every L1 block. Solidity tests cover 12-second and
irregular service; these long runs assess signal lag and error allowances.
"""
from concurrent.futures import ProcessPoolExecutor, as_completed
import argparse
import copy
import json
import math
import multiprocessing
from pathlib import Path

from .cli import atomic_json, fingerprint
from .model import DEFAULTS, STAGES, digest, load_config, pair_sizing, validate

PAIRS = ((0, 1), (0, 2), (1, 2))
LOG_TICK = math.log(1.0001)
# Twelve-hour response, one-day error memory, two-day warmup; no fitted cycles.
PARAMS = dict(response=43200, error=86400, max_gap=86400, warmup=172800,
              floor=round(.0002 / LOG_TICK / 86400 * 1e18))
PROFILES = [dict(name="three-phase", perturbations=[])] + [
    dict(name=name, perturbations=[dict(kind="noise", amplitude=sigma, independent=True)])
    for name, sigma in (("noise20", .002), ("noise80", .008), ("flat-noise80", .008))]


def simulate_observed(cfg, profile, stage, enabled=True, basket_mode=None, diagnostics=False, maintenance_only=False):
    """Shared observer/response wiring for the walkthrough and comparisons."""
    from . import engine

    class ObserverWorld(engine.World):
        def __init__(self, config, selected_stage):
            super().__init__(config, selected_stage)
            d = self.d
            self.pair_observer = self.chain.deploy("BlockPairSignals",
                [t.address for t in d.tokens], [d.buck.address, d.usdc.address],
                [*d.pool_buck, *d.pool_usdc], *PARAMS.values())
            self.route_metadata = [dict(token0=bool(r[1]), decimal_offset_ticks=int(r[2])/1e18)
                for r in (self.pair_observer.functions.routes(i).call() for i in range(6))]
            self.pair_wheel = self.chain.deploy("BasketMachineWheel", d.buck.address, d.usdc.address, d.gov, 0, 0)
            self.chain.send(self.pair_wheel.functions.setSample(self.pair_observer.address), sender=d.gov)
            self.observation_gas = []
            self.pair_policy = None
            self.pair_plans = []
            self.pair_decisions = []
            self.pair_response_check = None
            warm_days = config["bootstrap"].get("basket_warmup_days",0)
            if warm_days:
                self.warm_basket(warm_days)
            if basket_mode:
                self.chain.send(d.basket.functions.setResponse(self.signals.address,
                    (config["basket"]["leash_bp"], config["monitor"]["interval"], 0)), sender=d.gov)
                if basket_mode == "pair":
                    from alberta_buck.sim.chain import load_artifact
                    from web3.logs import DISCARD
                    from eth_utils import keccak
                    # Accepted historical means, never the first live price or
                    # any selected perturbation. Observer levels normalize decimals.
                    reference = [round(math.log(p) / LOG_TICK * 1e18) for p in config["feed"]["base"]]
                    self.pair_policy = self.chain.deploy("PairRebalancePolicy", self.pair_observer.address,
                        d.basket.address, reference, pair_sizing(config["pair"]))
                    response_name = ("PairBasketMaintenanceControl" if maintenance_only else
                                     "PairBasketDecisionTrace" if diagnostics else "PairBasketEquityResponse")
                    facet = self.chain.deploy(response_name, self.pair_policy.address)
                    self.chain.send(d.basket.functions.setEquityWheel(facet.address), sender=d.gov)
                    abi, _ = load_artifact(response_name)
                    events = d.w3.eth.contract(address=d.basket.address, abi=abi).events
                    original = self.chain.send
                    def send(fn, **kwargs):
                        rc = original(fn, **kwargs)
                        if fn.address == d.wheel.address and fn.fn_name == "tick":
                            self.pair_plans.extend(dict(e["args"]) for e in events.PairResponse().process_receipt(rc, errors=DISCARD))
                            if diagnostics:
                                def plain(x):
                                    if isinstance(x, bool): return x
                                    if isinstance(x, int): return str(x)
                                    if isinstance(x, (list, tuple)): return [plain(v) for v in x]
                                    return {k: plain(v) for k,v in x.items()}
                                self.pair_decisions.extend(plain(e["args"]) for e in events.PairDecision().process_receipt(rc, errors=DISCARD))
                                if not self.pair_decisions:
                                    # pyrevm rejects contract-code callers. In a
                                    # discarded snapshot only, authorize the EOA
                                    # governor as wheel and replay that component.
                                    # No simulated receipt, gas or book survives.
                                    probe = d.w3.eth.contract(address=d.basket.address, abi=abi)
                                    checkpoint = self.evm.evm.snapshot()
                                    try:
                                        setter = keccak(text="setWheel(address)")[:4] + d.w3.codec.encode(["address"], [d.gov])
                                        self.evm.evm.message_call(d.gov, d.basket.address, calldata=setter)
                                        output = self.evm.evm.message_call(d.gov, d.basket.address,
                                            calldata=bytes.fromhex(probe.functions.wheelStep(2, 0)._encode_transaction_data()[2:]))
                                        self.pair_response_check = dict(work=int.from_bytes(output,"big"), error=None)
                                    except RuntimeError as exc:
                                        self.pair_response_check = dict(work=None, error=str(exc))
                                    finally:
                                        self.evm.evm.revert(checkpoint)
                                    self.pair_response_check["method"] = "discarded EOA-wheel replay"
                        return rc
                    self.chain.send = send

        def warm_basket(self, days):
            """Identical funded starting book, with no knowledge of live prices.

            Use the corrected response's actual LP placement at accepted mean
            prices, before either experimental response is installed. Daily
            deposit-flow accounting settles too; K and private actors are idle.
            """
            from alberta_buck.sim.chain import load_artifact
            d = self.d; old_facet = d.basket.functions.equityWheel().call()
            venue = d.w3.eth.contract(address=d.basket.address,abi=load_artifact("ValidatedBasketVenue")[0])
            references = [round(math.log(p)/LOG_TICK*1e18) for p in self.cfg["feed"]["base"]]
            # The artificial fixed-mean LP prelude is not live price history.
            # Keep its observer separate so the main estimator starts at the
            # selected opening phase, without a fictitious mean-to-spot jump.
            prelude_observer = self.chain.deploy("BlockPairSignals",
                [t.address for t in d.tokens], [d.buck.address, d.usdc.address],
                [*d.pool_buck, *d.pool_usdc], *PARAMS.values())
            # Prices rest at their means here, so no pair is separated and the
            # sizing is never consulted; every arm shares the default regardless.
            policy = self.chain.deploy("PairRebalancePolicy",prelude_observer.address,d.basket.address,references,
                pair_sizing(DEFAULTS["pair"]))
            facet = self.chain.deploy("PairBasketEquityResponse",policy.address)
            response = d.w3.eth.contract(address=d.basket.address,abi=load_artifact("PairBasketEquityResponse")[0])
            self.chain.send(d.basket.functions.setEquityWheel(facet.address),sender=d.gov)
            self.chain.send(d.basket.functions.setResponse(engine.ZERO,
                (self.cfg["basket"]["leash_bp"],self.cfg["monitor"]["interval"],0)),sender=d.gov)
            self.chain.send(venue.functions.setWheel(d.gov),sender=d.gov)
            self.drive(self.cfg["feed"]["base"],replay=True)
            start = int(d.w3.eth.get_block("latest")["timestamp"]) + 601
            for day in range(days):
                self.warp(start + day*86400)
                self.chain.send(response.functions.wheelStep(0,0),sender=d.gov)
                for i in range(3): self.chain.send(response.functions.wheelStep(1,i),sender=d.gov)
                self.chain.send(response.functions.wheelStep(2,0),sender=d.gov)
            self.chain.send(venue.functions.setWheel(d.wheel.address),sender=d.gov)
            self.chain.send(d.basket.functions.setEquityWheel(old_facet),sender=d.gov)
            self.chain.send(d.basket.functions.setResponse(self.signals.address,
                (self.cfg["basket"]["leash_bp"],self.cfg["monitor"]["interval"],
                 round(self.cfg["basket"]["tilt"]*1e18))),sender=d.gov)
            self.start = (int(d.w3.eth.get_block("latest")["timestamp"])//86400+1)*86400
            self.initial_market_book = self.market_book()
            self.bootstrap.update(prices=self.prices(),market=self.initial_market_book,
                basket_prelude=dict(days=days,prices=self.cfg["feed"]["base"],
                    equity_buck=int(d.basket.functions.equity().call())/1e6,
                    account_buck=int(d.buck.functions.signedBalanceOf(d.basket.address).call())/1e6,
                    weights=list(d.basket.functions.responseWeightsBp().call()),
                    idle=[str(d.basket.functions.idleToken(i).call()) for i in range(3)],
                    liquidity=[str(d.basket.functions.liquidityOf(i).call()) for i in range(3)]))

        def observe_pairs(self):
            rc = self.chain.send(self.pair_wheel.functions.tick(1, 0))
            self.observation_gas.append(int(rc["gasUsed"]))

        def before_policy(self):
            if basket_mode and enabled:
                self.pair_plans = []
                self.pair_decisions = []
                self.pair_response_check = None
                self.observe_pairs()

        def arbitrage(self, phase):
            super().arbitrage(phase)
            if enabled and phase == "after-policy" and not basket_mode and self.flags.get("monitor"):
                # A separate wheel keeps the legacy slot order unchanged.
                self.observe_pairs()

        def snapshot(self, row, before, invalid_after_move):
            frame = super().snapshot(row, before, invalid_after_move)
            if not enabled or not self.flags.get("monitor"):
                return frame
            s = self.pair_observer
            routes = []
            for q in range(2):
                v = s.functions.venues(q).call()
                routes.append(dict(endpoint=int(v[0]), midpoint_day=(int(v[1]) / 2 - self.start) / 86400
                    if v[3] else None, samples=int(v[3]), readable=bool(v[4]), elapsed_seconds=int(v[5])/2))
            pairs = []
            for pair_number, (i, j) in enumerate(PAIRS):
                p = s.functions.pair(i, j).call()
                entries = []
                for q in range(2):
                    level, rate, allowance, error = s.functions.routePair(q, i, j).call()
                    _, acceleration = s.functions.errors(q * 3 + pair_number).call()
                    entries.append(dict(level_log=int(level) / 1e18 * LOG_TICK,
                        rate_bp_day=int(rate) / 1e18 * LOG_TICK * 86400 * 10000,
                        allowance_bp_day=int(allowance) / 1e18 * LOG_TICK * 86400 * 10000,
                        error_bp=int(error) / 1e18 * LOG_TICK * 10000,
                        acceleration_bp_day2=int(acceleration) / 1e18 * LOG_TICK * 86400**2 * 10000))
                # Synthetic smooth signal is scoring truth ONLY, never sent
                # to the contract. No truth is claimed for a free market arm.
                mid = routes[0]["midpoint_day"]
                truth = None
                if self.flags.get("replay") and mid is not None:
                    a = self.cfg["feed"]["amplitude"]
                    w = 2 * math.pi / self.cfg["feed"]["period_days"]
                    def velocity(k):
                        phase = mid * w + k * 2 * math.pi / 3
                        return a * w * math.cos(phase) / (1 + a * math.sin(phase))
                    truth = (velocity(i) - velocity(j)) * 10000
                pairs.append(dict(tokens=[i, j], level_log=int(p[0]) / 1e18 * LOG_TICK,
                    rate_bp_day=int(p[1]) / 1e18 * LOG_TICK * 86400 * 10000,
                    low_bp_day=int(p[2]) / 1e18 * LOG_TICK * 86400 * 10000,
                    high_bp_day=int(p[3]) / 1e18 * LOG_TICK * 86400 * 10000,
                    error_bp=int(p[4]) / 1e18 * LOG_TICK * 10000,
                    level_spread_bp=int(p[5]) / 1e18 * LOG_TICK * 10000,
                    rate_spread_bp_day=int(p[6]) / 1e18 * LOG_TICK * 86400 * 10000,
                    ready_mask=int(p[7]), routes=entries, truth_bp_day=truth,
                    raw=[str(x) for x in p]))
            frame["pair_signal"] = dict(epoch=int(s.functions.epoch().call()),
                available_mask=int(s.functions.availableMask().call()),
                ready_mask=int(s.functions.readyMask().call()), venues=routes, pairs=pairs,
                gas=self.observation_gas[-1],
                pool_samples=[dict(cumulative=str(p[0]), tick=int(p[1]), sqrt_price_x96=str(p[2]), liquidity=str(p[3]))
                    for p in (s.functions.spots(k).call() for k in range(6))])
            if self.step == 0:
                frame["pair_signal"]["route_metadata"] = self.route_metadata
            if self.pair_policy is not None:
                frame["pair_policy"] = dict(plans=self.pair_plans,
                    desire=[[0 if i == j else int(self.pair_policy.functions.desire(i, j).call()) / 1e18
                        for j in range(3)] for i in range(3)],
                    own_impact_ticks=[int(self.pair_policy.functions.remainingImpact(i).call()) / 1e18 for i in range(3)])
                if diagnostics:
                    frame["pair_decisions"] = self.pair_decisions
                    frame["pair_response_check"] = self.pair_response_check
            return frame

    # Disabled-observer controls still receive the identical funded LP prelude.
    result = engine.simulate(cfg, profile, stage, world_factory=ObserverWorld)
    result.update(observer_params=PARAMS, observer_enabled=enabled)
    if "pair_signal" in result["frames"][0]:
        result["observer_routes"] = result["frames"][0]["pair_signal"].pop("route_metadata")
    if basket_mode:
        result.update(basket_mode=basket_mode)
        if basket_mode == "pair":
            result["pair_reference"] = dict(source="configured historical baseline means, excluding live perturbations",
                prices=cfg["feed"]["base"], fractional_ticks=[str(round(math.log(p)/LOG_TICK*1e18)) for p in cfg["feed"]["base"]])
    return result


def run_case(cfg, profile, stage, key, code, path, enabled=True, basket_mode=None, diagnostics=False):
    result = simulate_observed(cfg, profile, stage, enabled, basket_mode, diagnostics)
    result.update(cache_key=key, implementation=code)
    atomic_json(path, result)
    return dict(profile=profile["name"], stage=stage, cache_key=key, file=Path(path).name, summary=result["summary"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="doc/control-lab.toml")
    ap.add_argument("--out", default="doc/control-lab/block-pairs")
    ap.add_argument("--days", type=int)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--render-only", action="store_true")
    args = ap.parse_args(argv)
    if args.jobs < 1:
        ap.error("--jobs must be positive")
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    if args.render_only:
        from .block_pairs_render import render
        return render(out)
    from alberta_buck.sim.chain import load_artifact
    cfg = load_config(args.config)
    if args.days is not None:
        cfg["days"] = args.days
    cfg["feed"]["walk_amplification"] = 0
    validate(cfg)
    code = fingerprint()
    code["observer"] = digest(dict(source=Path(__file__).read_text(), artifact=load_artifact("BlockPairSignals"), params=PARAMS))
    stages = ("monitor", "linked", "basket")
    manifest = dict(schema=1, status="running", kind="block-pairs", requested_config=cfg,
        implementation=code, stages={s: STAGES[s] for s in stages}, entries={}, controls={},
        axes=[dict(id="profile", label="Price signal", values=[dict(id=p["name"], label=p["name"]) for p in PROFILES]),
              dict(id="walk", label="Random walk", values=[dict(id="0", label="0×", value=0)]),
              *[dict(id=n, label=n.title(), requires=f, values=[dict(id="medium", label="Medium", value=5000)])
                for n, f in (("buyer", "demand"), ("seller", "supply"))]],
        cases={s: {} for s in stages}, unique_runs=len(stages)*len(PROFILES), visible_cases=len(stages)*len(PROFILES),
        method="Passive BlockPairSignals beside the legacy EMAs. Six-hour market updates for 120 days; the contract also has 12-second cadence tests. Noise is a per-observation perturbation of TOKEN prices, shared by both replay routes, not independent evidence. Grey reference rates use the synthetic smooth input only for scoring. No new signal has trading authority.")
    tasks = []
    for profile in PROFILES:
        for stage in stages:
            selected = copy.deepcopy(cfg)
            if profile["name"] == "flat-noise80":
                selected["feed"]["amplitude"] = 0
            for enabled in ([True, False] if profile["name"] == "three-phase" else [True]):
                key = digest(dict(config=selected, profile=profile, stage=stage, observer=enabled, implementation=code))
                path = out / f"{profile['name']}-{stage}-{'on' if enabled else 'off'}-{key[:16]}.json"
                if enabled:
                    manifest["cases"][stage][f"{profile['name']}:0:medium:medium"] = key
                tasks.append((selected, profile, stage, key, code, str(path), enabled))
    with ProcessPoolExecutor(max_workers=args.jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = {}
        for task in tasks:
            if Path(task[5]).exists():
                r = json.loads(Path(task[5]).read_text())
                entry = dict(profile=task[1]["name"], stage=task[2], cache_key=task[3], file=Path(task[5]).name, summary=r["summary"])
                manifest["entries" if task[6] else "controls"][task[3]] = entry
            else:
                futures[pool.submit(run_case, *task)] = task[6]
        for future in as_completed(futures):
            entry = future.result()
            manifest["entries" if futures[future] else "controls"][entry["cache_key"]] = entry
            atomic_json(out / "progress.json", manifest)
            print(f"{entry['profile']}/{entry['stage']} observer={futures[future]}: {entry['summary']['seconds']:.1f}s", flush=True)
    manifest["status"] = "complete"
    atomic_json(out / "manifest.json", manifest)
    from .block_pairs_render import render
    render(out)


if __name__ == "__main__":
    main()
