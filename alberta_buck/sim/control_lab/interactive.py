"""Build an offline HTML/JavaScript presentation from cached EVM JSON only."""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil

from .book import recorded, reference_value
from .cli import atomic_json
from .event_view import MARKER_NOTE, execution_counts, execution_log, price_markers

COLORS = ["#2463c4", "#b86107", "#158067"]

EXPLANATIONS = {
    "feeds": "Only the external market maker acts. Dashed lines are the selected feeds; dotted lines are TOKEN/USDC spot. The internal TOKEN/BUCK and BUCK/USDC pools are left alone. Their failure to follow is intentional: observing an external price does not transmit it into another pool.",
    "monitor": "This is an instrument test, not a market model. The feed driver explicitly replays the same numeric trace into TOKEN/BUCK by real swaps, with no arbitrage, desk or private flow. The monitor reads those pools. The lower panes show the published reference, whether each sample was accepted, and the pair observer's relative-price estimates.",
    "monitor-k": "Calibration: service the real K controller against the same replay, with no arbitrage or private/desk response. This isolates reference normalization and oracle precision from market flow. In the unperturbed three-phase case, the reference remains near one within integer-weight and V3-tick precision. K can integrate that small quantization error; its input and integral are not forcibly recentered. Compare this with linked/K before interpreting a larger drift as a filter defect.",
    "linked": "Direct replay into TOKEN/BUCK is now removed. One bounded, profit-seeking consistency arbitrage actor links TOKEN/USDC, TOKEN/BUCK and BUCK/USDC. Only TOKEN/USDC is externally driven. Internal prices can lag the feed or remain separated by fees. K is held fixed and the basket's investment policy remains off.",
    "k": "Service the real K controller after enabling the basket. Its input is the live basket TWAP. K changes basket credit headroom; the basket can respond through its own bounded actions. There is no desk or persistent buyer/seller yet. Compare this arm with basket at fixed K.",
    "desk": "Add the separate equity desk to the K arm. Its outer entry/release bands are 3%/2% by default. Operations are financed by its own inventory or permitted credit, with a finite turnover programme and capital floor. The capital pane distinguishes remaining spending authority from the actual signed BUCK account. Before/after-policy pool prices are retained in JSON; K sees a completed operation through a later TWAP.",
    "demand": "Branch from K without the desk. One buyer has a steady desire to accumulate BUCK, funded by a finite USDC budget. Its rate is positive at fair value and backs off exponentially at a premium; a discount increases the rate up to a daily cap. The reference is the basket's current external value. There is no private supplier, resale rule or replenishment. Buying outstanding BUCK is absorption into holdings, not a burn of total supply.",
    "supply": "Branch from K without the desk or buyer. One issuer steadily sells BUCK for USDC using activated, fixed-face credit and current spendable balance. Its rate is positive at fair value and backs off exponentially at a discount; a premium increases the rate up to a daily cap. K changes available headroom. This simple flow agent never buys back to deleverage: a falling limit can leave an existing lien above the new limit. It is not a mortgage or liquidation model.",
    "both": "Enable the buyer and issuer together, still without the desk. Each acts at most once per market step. Their call order alternates, both rate limits are per day, and neither receives new funding during the run. This distinguishes two-sided private response from the isolated demand and supply cases.",
    "all": "Add the same desk to the two-sided private market. Compare both versus all for its incremental effect, and desk versus all for the private agents' effect. Every arm uses the same feed, initial pool liquidity and starting K; basket rebalancing remains enabled. The actors trade against current external basket value, not an assumed permanent USDC peg.",
    "basket": "Enable BasketUpkeepFacet immediately after linking the pools. K remains fixed; the desk and persistent buyers/sellers are absent. Both quote routes inform the relative-price estimator. Separated pairs with slowing departure or credible closure can trigger bounded transfers from overweight to underweight TOKENs; historical references guard trade direction. All stages share a settled opening LP book. Inspect sensor quality for rates, errors, participation and own-impact allowances. Later stages retain this basket while adding other feedback loops.",
}


def basket_performance(result):
    """Use the recorded net equity, without subtracting its lien twice.

    The fixture contributes equal TOKEN values at the baseline means while
    BUCK/USDC is one. Their three-phase sum makes this configured grant its
    initial BUCK and USD value, before the LP prelude and live perturbations.
    Dollar marks convert net equity at the sample's BUCK/USDC spot price;
    USDC is the simulation's dollar reference. return_pct remains in BUCK.
    Where the book itself was recorded, the reference mark values its TOKEN
    at the external feed and its net BUCK at the reference basket's cost.
    """
    initial = result["config"]["market"]["basket_equity_usd"]
    end = result["frames"][-1]
    final = end["basket"]["equity"]
    final_usd = final * end["buck_usdc"]
    income = end["basket"].get("arb_income", {})
    arb_buck, arb_usd = income.get("net_buck", 0.), income.get("net_usd", 0.)
    performance = dict(initial_buck=initial, final_net_buck=final,
                gain_buck=final - initial, return_pct=100 * (final / initial - 1),
                initial_usd=initial, final_net_usd=final_usd,
                gain_usd=final_usd - initial, return_usd_pct=100 * (final_usd / initial - 1),
                arb_beneficiary=income.get("beneficiary", "external"),
                arb_income_buck=arb_buck, arb_income_usd=arb_usd,
                other_gain_buck=final-initial-arb_buck, other_gain_usd=final_usd-initial-arb_usd)
    if recorded(end):
        at_reference = reference_value(end)
        performance.update(final_net_reference=at_reference, gain_reference=at_reference - initial,
                           return_reference_pct=100 * (at_reference / initial - 1))
    return performance


def plot_data(result):
    """Plot-ready columns; raw JSON retains every original value and EVM field."""
    frames, flags, cfg = result["frames"], result["flags"], result["config"]
    shadow_rows = result.get("shadow_diagnostics", {}).get("frames", [])
    shadow_by_day = {f["day"]:s for f,s in zip(frames,shadow_rows)}
    owned_arb = cfg["market"].get("arb_beneficiary", "external") == "basket"
    panes = []

    def line(label, color, fn, dash=None):
        values = [fn(f) for f in frames]
        return dict(label=label, color=color, dash=dash,
                    values=[float(f"{v:.10g}") if v is not None and math.isfinite(v) else None for v in values])

    def pane(id, title, unit, lines, note="", group=None):
        panes.append(dict(id=id, title=title, unit=unit, lines=lines, note=note, group=group))

    base = cfg["feed"]["base"]
    lines = []
    for i, color in enumerate(COLORS):
        name = "ABC"[i]
        lines.extend([
            line(f"{name} · internal", color, lambda f: f["token_buck"][i] * f["buck_usdc"] / base[i]),
            line(f"{name} · feed", color, lambda f: f["target_usd"][i] / base[i], "7 4"),
            line(f"{name} · USDC pool", color, lambda f: f["token_usdc"][i] / base[i], "2 4"),
        ])
    trend_note = ""
    if "inflation_usd" in frames[0]:
        lines.append(line("USD inflation trend", "#87949c", lambda f: f["inflation_usd"], "6 4"))
        trend_note = " Grey: the USD price level every TOKEN follows, each through its own lag."
    pane("price", "Token prices in dollars", "USD / baseline mean", lines,
         "Solid: TOKEN/BUCK × BUCK/USDC. Dashed: external feed. Dotted: TOKEN/USDC spot. Overlapping lines mean the markets agree." + trend_note)
    panes[-1]["markers"] = price_markers(result)
    if panes[-1]["markers"]:
        panes[-1]["note"] += " " + MARKER_NOTE
    if flags.get("monitor"):
        pane("native", "Token prices in BUCK", "BUCK / baseline mean", [
            line(f"TOKEN {'ABC'[i]}", color, lambda f: f["token_buck"][i] / base[i]) for i, color in enumerate(COLORS)],
            "Native pool quotes expose the monetary response before conversion into dollars.")
        panes[-1]["markers"] = price_markers(result, native=True)
        if panes[-1]["markers"]:
            panes[-1]["note"] += " " + MARKER_NOTE
        pane("reference", "Reference value and BUCK price", "Reference costs / USD per BUCK", [
            line("Live bvib", "#172d39", lambda f: f["bvib"]),
            line("Published bvib", "#b63e45", lambda f: f["signal"]["raw"] if f["signal"]["fresh"] else None, "3 3"),
            line("BUCK/USDC", "#7653ab", lambda f: f["buck_usdc"], "7 4"),
            line("Reference basket USD", "#657781", lambda f: f["reference"]["market_usd"], "2 4"),
            line("Parity", "#a1a9ad", lambda f: 1., "2 4"),
            *[line(f"Desk entry {sign:+d}", "#cea1a5", lambda f, sign=sign: 1 + sign * cfg["desk"]["enter_bp"] / 10000, "5 5")
              for sign in (-1, 1) if flags.get("desk")],
        ], "bvib is the fixed reference basket’s cost in BUCK. The external basket’s USD cost is its fair-value comparison, not a permanent $1 target.")
        pane("sensor", "Reference sample quality", "Indicator", [
            line("Rejected just after move", "#b63e45", lambda f: int(f["signal"]["invalid_after_move"])),
            line("Fresh at end of tick", "#172d39", lambda f: int(f["signal"]["fresh"]), "2 4"),
        ], "Rejection is an observation-quality guard; it does not prohibit arbitrage. Freshness is measured after the 601-second observation window.", "monitor-detail")
    if flags.get("k"):
        k0 = cfg["controller"]["k0"]
        pane("k", "K and its controller contributions", "Credit fraction", [
            line("K", "#172d39", lambda f: f["k"]),
            line("K₀ + P", "#2463c4", lambda f: k0 + f["p_contribution"], "7 4"),
            line("K₀ + I", "#b63e45", lambda f: k0 + f["i_contribution"], "2 4"),
            *[line(label, "#a1a9ad", lambda f, value=value: value, "3 4") for label, value in
              [("Initial K", k0), ("Lower rail", cfg["controller"]["kmin"]), ("Upper rail", cfg["controller"]["kmax"])]]],
            "P and I are plotted as offsets from initial K. A change in a credit limit alone does not place a market order.")
    if flags.get("arb"):
        initial = result["bootstrap"]["market"]
        pane("custody", "Where the market’s BUCK goes", "Thousands of BUCK", [
            line("Arb cash awaiting credit" if owned_arb else "Arbitrage profit held", "#7653ab", lambda f: f["market"]["arb_held_buck"] / 1000),
            line("Δ pool BUCK outside active depth", "#b86107", lambda f: (f["market"]["pool_minus_virtual_buck"] - initial["pool_minus_virtual_buck"]) / 1000),
            line("Decrease in active reserves", "#172d39", lambda f: (initial["active_buck"] - f["market"]["active_buck"]) / 1000, "3 4"),
        ], ("Arb income is credited to the basket; the arb wallet should retain no distributable profit. " if owned_arb else
            "The external arbitrage actor retains its receipts; they are excluded from basket ROI. ") +
            "Gas costs are excluded. At fixed liquidity, the second line records uncollected BUCK fees; changing LP positions also changes the reserve offset.")
    if flags.get("desk"):
        pane("desk", "Desk capital and spending authority", "Millions of BUCK", [
            *[line(label, color, lambda f, key=key: f["desk"][key] / 1e6) for key, label, color in
              [("capital", "Net capital (low mark)", "#158067"), ("budget", "Remaining turnover budget", "#7653ab"), ("account", "Signed BUCK account", "#b86107")]],
            line("Capital floor", "#b63e45", lambda f: cfg["desk"]["capital_floor_buck"] / 1e6, "3 4"),
        ], "The turnover budget limits gross activity. It is separate from owned capital and the signed credit account. The floor constrains operations; market repricing can still reduce inventory value.")
    if flags.get("demand") or flags.get("supply"):
        lines = []
        if flags.get("demand"):
            lines.extend([line("USDC spent buying BUCK", COLORS[0], lambda f: f["cumulative"]["demand_usdc"] / 1000),
                          line("Buyer cash remaining", COLORS[0], lambda f: f["agents"]["demand_cash"] / 1000, "3 4")])
        if flags.get("supply"):
            lines.extend([line("BUCK sold", COLORS[1], lambda f: f["cumulative"]["supply_buck"] / 1000),
                          line("Seller spendable BUCK", COLORS[1], lambda f: f["agents"]["supply_headroom"] / 1000, "3 4")])
        pane("actors", "Buyer and seller flows", "Thousands of native units", lines,
             "The selected quantity is the desired daily rate at fair value. Adverse prices reduce it exponentially. Cash and credit are finite; the seller does not repay during this experiment.")
    if flags.get("basket"):
        pane("allocation", "Basket allocation", "Percent", [
            line(f"TOKEN {'ABC'[i]}", color, lambda f: f["basket"]["weights"][i] / 100) for i, color in enumerate(COLORS)],
            "Allocation follows the replacement basket’s bounded Daily / Sync / Respond work. The vertical axis fits the visible allocations.")
        performance = basket_performance(result)
        initial = performance["initial_buck"]
        booked = recorded(frames[0])
        reference_note = (" The reference mark revalues the recorded book itself: TOKEN at the external feed and net BUCK at the reference basket’s USD cost. It does not move when this stage’s own BUCK/USDC price drifts." if booked else "")
        pane("basket-value", "Basket net value in BUCK and USD", "BUCK / USD", [
            line("Net value · BUCK", "#158067", lambda f: f["basket"]["equity"]),
            line("Net value · USD", "#7653ab", lambda f: f["basket"]["equity"] * f["buck_usdc"], "7 4"),
            line("Initial investment · BUCK / USD", "#87949c", lambda f: initial, "6 4"),
            *([line("Net value · at reference", "#b86107", reference_value, "2 3")] if booked else []),
        ], f"Initial contribution: {initial:,.0f} BUCK = ${initial:,.0f} at bootstrap parity before the LP prelude. Net BUCK value = LP and idle TOKEN value + held BUCK − issued BUCK liability + accrued relief, using the contract’s TWAP equity mark. The liability is already deducted. USD value multiplies this net mark by each sample’s BUCK/USDC spot price, treating USDC as the dollar reference. The initial investment stays fixed in each currency." + reference_note)
        pane("basket-return", "Basket return on initial investment", "Percent", [
            line("Return · BUCK", "#158067", lambda f: 100 * (f["basket"]["equity"] / initial - 1)),
            line("Return · USD", "#7653ab", lambda f: 100 * (f["basket"]["equity"] * f["buck_usdc"] / performance["initial_usd"] - 1), "7 4"),
            line("Break-even", "#87949c", lambda f: 0., "6 4"),
            *([line("Return · at reference", "#b86107", lambda f: 100 * (reference_value(f) / initial - 1), "2 3")] if booked else []),
        ], "Return = (net value / original investment − 1) × 100% in the same currency. An appreciating BUCK can produce a negative BUCK return alongside a positive USD return. Both include the LP prelude and live run, inventory repricing, fees and accrued relief; neither isolates rebalancing profit. These are marked returns, excluding caller gas and liquidation costs." + reference_note)
        if owned_arb:
            for currency in ("buck", "usd"):
                def total_return(f, c=currency):
                    return 100*(f["basket"]["equity"]*(f["buck_usdc"] if c=="usd" else 1)/initial-1)
                def arb_return(f, c=currency):
                    return 100*f["basket"]["arb_income"]["net_"+c]/initial
                pane("basket-income-"+currency, "Return components · "+currency.upper(), "Percent of initial investment", [
                    line("Total basket return", "#172d39", total_return),
                    line("Arb income received", "#7653ab", arb_return, "7 4"),
                    line("Other portfolio return", "#158067", lambda f: total_return(f)-arb_return(f), "3 3"),
                    line("Break-even", "#87949c", lambda f: 0., "6 4"),
                ], "Arb receipts already enter the basket's actual equity: total = arb income + other return, with no second addition. Credits are net of BUCK arrival fees; USD credits use receipt-time prices. Other return includes inventory repricing, LP fees, relief and rebalancing. The matched four-arm comparison separately measures the effect of enabling rebalancing.")
        comparison = result.get("research_comparison")
        if comparison:
            by_day = {f["day"]:f for f in comparison["frames"]}
            for currency in [c for c in ("buck", "usd", "reference") if c in comparison["frames"][0]]:
                pane("basket-effects-"+currency, "Matched strategy effects · "+currency.upper(), "Percentage points of initial investment", [
                    line(label, color, lambda f,k=key,c=currency: 100*by_day[f["day"]][c][k]/comparison["initial"],dash)
                    for key,label,color,dash in (("baseline","Maintenance baseline", "#87949c","3 3"),
                        ("rebalancing","Effect of rebalancing", "#158067",None),
                        ("arb_ownership","Effect of owning arb income", "#7653ab","7 4"),
                        ("interaction","Interaction", "#b86107","3 3"),
                        ("total","Combined gain", "#172d39",None))], comparison["method"])
    if "pair_signal" in frames[0]:
        pane("pair-quality", "Pair observer readiness and cost", "Indicator / millions of gas", [
            line("Ready BUCK route", COLORS[0], lambda f: int(bool(f["pair_signal"]["ready_mask"] & 1))),
            line("Ready USDC route", COLORS[1], lambda f: int(bool(f["pair_signal"]["ready_mask"] & 2)), "3 3"),
            line("Observation wheel gas / 1M", "#7653ab", lambda f: f["pair_signal"]["gas"] / 1e6),
        ], "Readiness requires aligned elapsed samples and two days of warmup. Gas is the actual pyrevm transaction receipt, including the observation wheel.")
        for pair_number, (i, j) in enumerate(((0, 1), (0, 2), (1, 2))):
            name = f"{'ABC'[i]}/{'ABC'[j]}"
            def pair(f, p=pair_number):
                return f["pair_signal"]["pairs"][p]
            pane(f"pair-level-{pair_number}", f"{name} · relative price", "Log basis points from baseline ratio", [
                *[line(label, color, lambda f, key=key: 10000 * math.log((f[key][i] / f[key][j]) / (base[i] / base[j])), dash)
                  for key, label, color, dash in (("token_buck", "BUCK spot route", COLORS[0], "3 3"),
                    ("token_usdc", "USDC spot route", COLORS[1], "7 4"))],
                line("Filtered consensus", "#172d39", lambda f: 10000 * (pair(f)["level_log"] - math.log(base[i]/base[j])) if pair(f)["ready_mask"] else None),
            ], "A common movement of the quote currency cancels in each TOKEN ratio. Levels are centered on the historical mean ratio. The observer estimates motion; the pair basket also uses this historical reference to guard trade direction.")
            pane(f"pair-rate-{pair_number}", f"{name} · departure and closure rate", "Log basis points per day", [
                line("Estimated rate", "#172d39", lambda f: pair(f)["rate_bp_day"] if pair(f)["ready_mask"] else None),
                *[line(label, color, lambda f, q=q: pair(f)["routes"][q]["rate_bp_day"] if pair(f)["ready_mask"] & (1 << q) else None, "3 3")
                  for q, label, color in ((0, "BUCK route", COLORS[0]), (1, "USDC route", COLORS[1]))],
                *[line(label, "#af7278", lambda f, key=key: pair(f)[key] if pair(f)["ready_mask"] else None, "2 4")
                  for key, label in (("low_bp_day", "Allowance lower"), ("high_bp_day", "Allowance upper"))],
                line("Smooth input reference at interval midpoint", "#87949c", lambda f: pair(f)["truth_bp_day"], "7 4"),
                line("Zero rate", "#a1a9ad", lambda f: 0., "2 4"),
                line("Route rate spread", "#7653ab", lambda f: pair(f)["rate_spread_bp_day"] if pair(f)["ready_mask"] else None, "7 4"),
            ], "Positive means the numerator gains relative to the denominator. Above its historical ratio this is departure; negative motion is closure. Below that ratio the directions reverse. Allocation is a separate trade condition. Allowances are empirical, not confidence intervals. The smooth reference is scoring-only, available in replay arms.")
            if shadow_rows:
                def shadow(f, p=pair_number):
                    s=shadow_by_day.get(f["day"])
                    return s["pairs"][p] if s and s["ready"] else {}
                panes[-1]["lines"].extend([
                    line(label,color,lambda f,key=key: shadow(f).get(key),dash)
                    for key,label,color,dash in (("rate_bp_day","Kalman rate · shadow","#158067","7 4"),
                        ("low_bp_day","Kalman lower envelope","#8aae9c","2 4"),
                        ("high_bp_day","Kalman upper envelope","#8aae9c","2 4"))])
                panes[-1]["note"] += " Shadow Kalman estimates use the same interval observations; the envelope spans both routes' model ±2σ bounds without assuming independence. They do not authorize trades. The active basket adds its own-impact allowance separately."
                pane(f"pair-noise-{pair_number}", f"{name} · price-noise proxies and model error", "Log basis points", [
                    line("Current absolute prediction error", "#b63e45", lambda f: pair(f)["error_bp"] if pair(f)["ready_mask"] else None),
                    line("Second-difference roughness σ", "#2463c4", lambda f: shadow(f).get("roughness_bp")),
                    line("Cross-route disagreement proxy", "#b86107", lambda f: shadow(f).get("disagreement_bp"), "7 4"),
                    line("Kalman adaptive measurement σ", "#158067", lambda f: shadow(f).get("measurement_bp"), "3 3"),
                ], "Roughness estimates noise only under locally linear motion and white observation errors; curvature and jumps also contribute. Route disagreement cannot detect shared noise. Kalman measurement covariance adapts from bounded prior roughness, so these are related diagnostics, not independent votes. Prediction error is an absolute residual, not a standard deviation. All diagnostics are causal Python calculations with no trading authority.", "monitor-detail")
            pane(f"pair-error-{pair_number}", f"{name} · prediction error and route disagreement", "Log basis points", [
                line("Mean absolute prediction error", "#b63e45", lambda f: pair(f)["error_bp"] if pair(f)["ready_mask"] else None),
                line("Filtered route level spread", "#7653ab", lambda f: pair(f)["level_spread_bp"] if pair(f)["ready_mask"] else None),
            ], "Prediction error includes noise, real news and model lag. Agreement between venues does not remove shared noise.")
    if "pair_policy" in frames[0]:
        pane("pair-intent", "Pair participation after the current actions", "Fraction of eligible gap per day", [
            line(f"{'ABC'[i]} → {'ABC'[j]}", color, lambda f, i=i, j=j: f["pair_policy"]["desire"][i][j], dash)
            for (i,j),color,dash in zip(((0,1),(0,2),(1,0),(1,2),(2,0),(2,1)),
                (COLORS[0],COLORS[0],COLORS[1],COLORS[1],COLORS[2],COLORS[2]), (None,"4 4",None,"4 4",None,"4 4"))],
            "These are post-action directional rates before allocation/depth caps. Own-impact uncertainty can reduce them after a fill. Executed requests are recorded separately in JSON and price markers.")
        pane("pair-impact", "Basket's remaining own-impact allowance", "Log basis points", [
            line(f"TOKEN {'ABC'[i]}", color, lambda f: f["pair_policy"]["own_impact_ticks"][i] * math.log(1.0001) * 10000)
            for i,color in enumerate(COLORS)],
            "Measured immediate tick movement from this basket's operations, accumulated and linearly decayed over two days. It widens uncertainty; it is not subtracted as a known counterfactual price path.")
        patience = 100 * result["config"].get("pair", {}).get("patience", 0)
        if patience and "urgency_point_days" in frames[0]["pair_policy"]:
            slack = 100 * result["config"]["pair"]["slack"]
            pane("pair-urgency", "Urgency to rebalance each TOKEN", "Point-days", [
                *[line(f"TOKEN {'ABC'[i]}", color, lambda f, i=i: f["pair_policy"]["urgency_point_days"][i])
                  for i, color in enumerate(COLORS)],
                line("Patience", "#87949c", lambda f: patience, "3 3")],
                f"Each TOKEN counts how far its share of the book is from target, beyond a slack of {slack:g} points, over "
                f"time. Once the count reaches the patience of {patience:g} point-days the TOKEN may be rebalanced, and a "
                "transfer starts its count again. A large gap gets there soon; a small one gets there if it lasts. This "
                "decides only whether a gap may be worked: pair participation still decides when and how much.")
    if shadow_rows:
        def innovation(f, q):
            s=shadow_by_day.get(f["day"])
            return s["innovation_nis"][q] if s and s["ready"] else None
        pane("pair-innovation", "Kalman model checks · shadow only", "Normalized squared innovation per coordinate", [
            line("BUCK route", COLORS[0], lambda f: innovation(f,0)),
            line("USDC route", COLORS[1], lambda f: innovation(f,1), "7 4"),
            line("Model reference", "#87949c", lambda f: 1., "3 3"),
        ], "Values near one are a model-based reference, not a guaranteed calibration. Large innovations expose surprise before the sample can adjust its own noise estimate. Sustained departures can reveal wrong dynamics, correlated errors or misestimated noise.", "monitor-detail")
    if result.get("sensor_details"):
        for item in panes:
            if item["id"].startswith("pair-"):
                item["group"] = "monitor-detail"
    return dict(days=[f["day"] for f in frames], panes=panes, summary=result["summary"],
                execution_counts=execution_counts(frames), executions=execution_log(result),
                mean_bvib=sum(f["bvib"] for f in frames) / len(frames),
                basket_performance=basket_performance(result) if flags.get("basket") else None,
                feed_source=result.get("feed_source"),
                config={k: cfg[k] for k in ("feed", "controller", "desk", "demand", "supply")},
                stage=result["stage"], profile=result["profile"]["name"], cache_key=result["cache_key"])


def render(root):
    root = Path(root)
    source = root / "manifest.json"
    if not source.exists():
        source = root / "progress.json"
    manifest = json.loads(source.read_text())
    data_dir = root / "plots"
    data_dir.mkdir(exist_ok=True)
    limits = {}
    entries = {}
    for key, entry in manifest["entries"].items():
        result = json.loads((root / entry["file"]).read_text())
        if key in manifest.get("diagnostics", {}):
            result["shadow_diagnostics"] = json.loads((root / manifest["diagnostics"][key]).read_text())
        if key in manifest.get("comparisons", {}):
            result["research_comparison"] = json.loads((root / manifest["comparisons"][key]).read_text())
        data = plot_data(result)
        # Script transport makes the presentation work from file:// too.
        # The payload contains numbers/prose, never executable user code.
        payload = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        (data_dir / f"{key[:16]}.js").write_text(f"window.controlLabReceive({json.dumps(key)},{payload});\n")
        entries[key] = {**entry, "plot_file": f"plots/{key[:16]}.js",
                        "execution_counts": data["execution_counts"]}
        group = result["profile"]["name"] + ":" + f"{result['config']['feed']['walk_amplification']:g}"
        group_limits = limits.setdefault(group, {})
        for pane in data["panes"]:
            values = [v for line in pane["lines"] for v in line["values"] if v is not None]
            values.extend(p["value"] for group in pane.get("markers", []) for p in group["points"])
            if not values:
                continue
            lo, hi = min(values), max(values)
            prior = group_limits.get(pane["id"], (lo, hi))
            group_limits[pane["id"]] = [min(lo, prior[0]), max(hi, prior[1])]
    public = {k: manifest[k] for k in ("schema", "status", "axes", "stages", "cases", "unique_runs", "visible_cases", "implementation")}
    if "kind" in manifest:
        public["kind"] = manifest["kind"]   # which campaign a page shows; its browser check reads this
    public.update(entries=entries, limits=limits, explanations=manifest.get("explanations", EXPLANATIONS),
                  days=manifest["requested_config"]["days"],
                  seed=manifest["requested_config"]["seed"],
                  walk_daily_sigma=manifest["requested_config"]["feed"]["walk_daily_sigma"],
                  elapsed_seconds=manifest.get("elapsed_seconds", 0))
    if "method" in manifest:
        public["method"] = manifest["method"]
    (root / "manifest.js").write_text("window.controlLabManifest=" + json.dumps(public, ensure_ascii=True, separators=(",", ":")) + ";\n")
    web = Path(__file__).with_name("web")
    for name in ("index.html", "app.js", "style.css"):
        if name == "index.html":
            template = "basket-research.html" if manifest.get("kind") == "basket-research" else name
            (root / name).write_text((web / template).read_text().replace("manifest.json", source.name))
        else:
            shutil.copyfile(web / name, root / name)
    atomic_json(root / "render-summary.json", dict(runs=len(entries), status=manifest["status"],
        plot_bytes=sum(p.stat().st_size for p in data_dir.glob("*.js"))))
    print(f"Rendered {len(entries)} cached runs → {root / 'index.html'}")
