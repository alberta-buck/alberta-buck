"""Build an offline HTML/JavaScript presentation from cached EVM JSON only."""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil

from .cli import atomic_json
from .render import EXPLANATIONS
from .event_view import MARKER_NOTE, execution_counts, execution_log, price_markers

COLORS = ["#2463c4", "#b86107", "#158067"]


def basket_performance(result):
    """Use the recorded net equity, without subtracting its lien twice.

    The fixture contributes equal TOKEN values at the baseline means while
    BUCK/USDC is one. Their three-phase sum makes this configured grant its
    initial BUCK value, before the LP prelude and live perturbations.
    """
    initial = result["config"]["market"]["basket_equity_usd"]
    final = result["frames"][-1]["basket"]["equity"]
    return dict(initial_buck=initial, final_net_buck=final,
                gain_buck=final - initial, return_pct=100 * (final / initial - 1))


def plot_data(result):
    """Plot-ready columns; raw JSON retains every original value and EVM field."""
    frames, flags, cfg = result["frames"], result["flags"], result["config"]
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
    pane("price", "Token prices in dollars", "USD / baseline mean", lines,
         "Solid: TOKEN/BUCK × BUCK/USDC. Dashed: external feed. Dotted: TOKEN/USDC spot. Overlapping lines mean the markets agree.")
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
        scale = math.log(1.0001) * 100
        pane("excursion", "Daily relative-price excursions", "Log percentage points", [
            line(f"TOKEN {'ABC'[i]}", color, lambda f: f["signal"]["excursion"][i] * scale) for i, color in enumerate(COLORS)],
            "Relative prices remove common basket motion. Excursion is a log-price displacement, not a realised trading return.")
        pane("votes", "Sensor quality and turn votes", "Count / indicator", [
            *[line(f"{'ABC'[i]} votes", color, lambda f: f["signal"]["votes"][i]) for i, color in enumerate(COLORS)],
            line("Turn quorum", "#89969c", lambda f: 4, "3 4"),
            line("Rejected just after move", "#b63e45", lambda f: int(f["signal"]["invalid_after_move"])),
            line("Fresh at end of tick", "#172d39", lambda f: int(f["signal"]["fresh"]), "2 4"),
        ], "Four of six votes form a turn quorum. Rejection is an observation-quality guard; it does not prohibit arbitrage. Freshness is measured after the 601-second observation window.", "monitor-detail")
        means = result["history"]["mean_relative_ticks"]
        for i in range(3):
            ladder_colors = ["#153c72", "#2463c4", "#238cad", "#158067", "#ab8236", "#bd6332", "#9a456e"]
            pane(f"ema-{i}", f"TOKEN {'ABC'[i]} · historical EMA ladder", "Log percentage points from known mean", [
                line("Raw relative price", "#87949c", lambda f: (f["signal"]["relative"][i] - means[i]) * scale),
                *[line(f"EMA {days}d", color, lambda f, j=j: (f["signal"]["ema"][i][j] - means[i]) * scale)
                  for j, (days, color) in enumerate(zip([5, 10, 20, 40, 80, 160, 1280], ladder_colors))]],
                "Every level starts from the unperturbed 1,280-day history. Noise, random walk, ramps and shocks begin only in the live experiment.", "monitor-detail")
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
            line("Arbitrage profit held", "#7653ab", lambda f: f["market"]["arb_held_buck"] / 1000),
            line("Δ pool BUCK outside active depth", "#b86107", lambda f: (f["market"]["pool_minus_virtual_buck"] - initial["pool_minus_virtual_buck"]) / 1000),
            line("Decrease in active reserves", "#172d39", lambda f: (initial["active_buck"] - f["market"]["active_buck"]) / 1000, "3 4"),
        ], "The arbitrage actor retains its receipts after pool fees; gas costs are excluded. At fixed liquidity, the second line records uncollected BUCK fees; changing LP positions also changes the reserve offset.")
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
        pane("basket-value", "Basket net value versus initial investment", "BUCK", [
            line("Net basket value", "#158067", lambda f: f["basket"]["equity"]),
            line("Initial investment", "#87949c", lambda f: initial, "6 4"),
        ], f"Initial contribution: {initial:,.0f} BUCK, valued at bootstrap parity before the LP prelude. Net value = LP and idle TOKEN value + held BUCK − issued BUCK liability + accrued relief, using the contract’s TWAP equity mark. The liability is already deducted. Returns include bootstrap and live results; this is a mark, not liquidation proceeds, and caller gas is excluded.")
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
    if result.get("sensor_details"):
        for item in panes:
            if item["id"].startswith("pair-"):
                item["group"] = "monitor-detail"
    return dict(days=[f["day"] for f in frames], panes=panes, summary=result["summary"],
                execution_counts=execution_counts(frames), executions=execution_log(result),
                mean_bvib=sum(f["bvib"] for f in frames) / len(frames),
                basket_performance=basket_performance(result) if flags.get("basket") else None,
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
            template = manifest["kind"] + ".html" if manifest.get("kind") in ("block-pairs", "pair-basket", "basket-first") else name
            (root / name).write_text((web / template).read_text().replace("manifest.json", source.name))
        else:
            shutil.copyfile(web / name, root / name)
    atomic_json(root / "render-summary.json", dict(runs=len(entries), status=manifest["status"],
        plot_bytes=sum(p.stat().st_size for p in data_dir.glob("*.js"))))
    print(f"Rendered {len(entries)} cached runs → {root / 'index.html'}")
