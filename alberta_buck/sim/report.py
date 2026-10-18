"""Completed simulation -> one PDF beside the vector; batch rendering also makes a linked HTML index.

    python -m alberta_buck.sim.report build/sim/mended/runs/*.json --jobs 4 --index build/sim/mended/index.html

No EVM work is repeated. The loop calls finish() after teardown for every saved run of at least five years.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stdout
from hashlib import sha256
from html import escape
import io
import json
import math
import multiprocessing
import os
from pathlib import Path
import tempfile
from urllib.parse import quote

FIVE_YEARS                      = 5 * 365


def preflight() -> None:
    """Check the plotting extra before spending hours on a simulation."""
    cache                       = Path(tempfile.gettempdir()) / "alberta-buck-mpl"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.backends.backend_pdf import PdfPages  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Five-year reports require the plotting extra: install 'alberta-buck[plots]'.") from exc


def pdf_path(vector: Path) -> Path:
    """Keep the same PDF name when a campaign promotes name.partial to name.json."""
    name                        = vector.name.removesuffix(".partial").removesuffix(".json")
    return vector.with_name(name + ".pdf")


def finish(snap, out_path) -> tuple[Path | None, Path | None]:
    """Close the vector first; a render failure preserves it and fails the run visibly."""
    snap.meta["run"]["completed"] = True
    path                        = snap.write(out_path) if out_path else None
    pdf                         = None
    if path and snap.s.days >= FIVE_YEARS:
        try:
            pdf                 = Path(render(path)["pdf"])
        except Exception as exc:
            raise RuntimeError(
                f"Simulation completed and saved to {path}, but its PDF failed. "
                f"Render the saved vector without rerunning the simulation: "
                f"python -m alberta_buck.sim.report '{path}'") from exc
    return path, pdf


def _credit_page(data, name, digest, pdf) -> None:
    """A readable first page showing who can still issue, before the detailed plots."""
    import matplotlib.pyplot as plt

    frames                      = data["frames"]
    days                        = [f["day"] for f in frames]
    last                        = frames[-1]
    household                   = "households" in last
    counts                      = Counter(a["cls"] for a in data.get("meta", {}).get("telemetry", {}).get("agents", []))
    fig, axes                   = plt.subplots(3, 2, figsize=(11.7, 8.3), sharex=True)
    fig.suptitle(name, fontsize=11)

    def series(ax, key, label, scale=1e12):
        if any(key in f for f in frames):
            ax.plot(days, [f.get(key, 0) / scale for f in frames], label=label, linewidth=1.1)

    ax                          = axes[0, 0]
    series(ax, "basketVal", "basket cost in BUCK", 1e18)
    ax.axhline(1, color="black", linewidth=.7, linestyle="--")
    ax.set_title("Above 1: the BUCK is cheap; below 1: dear")
    ax                          = axes[0, 1]
    if household:
        for key, label in (("limit", "activated limit"), ("drawn", "drawn")):
            ax.plot(days, [sum(a[key] for a in f.get("octl", []) if a.get("active")) / 1e12
                           for f in frames], label=label)
        ax.set_title("Household credit (million BUCKs)")
    else:
        series(ax, "fat_limit", "activated limit")
        series(ax, "fat_drawn", "drawn")
        ax.set_title("Large synthetic borrowers (million BUCKs)")
    ax                          = axes[1, 0]
    if household:
        series(ax, "rtr_buck_sold", "BUCK sold", 1e6)  # this legacy counter uses whole BUCKs
        series(ax, "bcd_retired", "BUCK bought back")
        ax.set_title("Household cumulative flows (million BUCKs)")
    else:
        series(ax, "fat_issued", "market issuance")
        series(ax, "fat_placed", "opening placement")
        series(ax, "fat_retired", "buybacks")
        series(ax, "fat_burned", "burn requests")
        ax.set_title("Large borrowers: cumulative flows (million BUCKs)")
    ax                          = axes[1, 1]
    for key, label in (("mortgage", "external mortgage ($M)"), ("drawn", "BUCK drawn (M)"),
                       ("limit", "activated limit (M BUCKs)")):
        if last.get("octl"):
            ax.plot(days, [sum(a[key] for a in f.get("octl", []) if not household or a.get("active")) / 1e12
                           for f in frames], label=label)
    ax.set_title("Mortgage cohort: amounts (separate currencies)")
    ax                          = axes[2, 0]
    for label, predicate in (("arrived", lambda a: a.get("active", False)),
                              ("ever deployed", lambda a: a["deploys"] > 0),
                              ("still has mortgage", lambda a: a["mortgage"] > 1e6
                               and (not household or a.get("active", False)))):
        if last.get("octl"):
            ax.plot(days, [sum(predicate(a) for a in f.get("octl", [])) for f in frames], label=label)
    ax.set_title("Household population: people" if household else "Finite mortgage cohort: people")
    ax                          = axes[2, 1]
    series(ax, "bcd_atomic_refis", "completed refinances", 1)
    series(ax, "bcd_atomic_declined", "declined quotes (repeat attempts)", 1)
    series(ax, "rtr_refis", "first carrying-cost refinance", 1)
    series(ax, "rtr_top_ups", "further carrying-cost refinances", 1)
    ax.set_title("Refinancing decisions: cumulative events")
    for ax in axes.flat:
        ax.grid(alpha=.2)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=7)
    for ax in axes[-1]:
        ax.set_xlabel("simulation day (frames follow that day's trading)")
    cast                        = "; ".join(f"{n} {cls}" for cls, n in counts.items())
    import textwrap
    footer                      = "\n".join(textwrap.wrap(cast or "Cast metadata unavailable", 150))
    fig.text(.025, .018, footer + f"\nVector SHA256: {digest}", fontsize=6.5)
    fig.tight_layout(rect=(0, .12, 1, .96))
    pdf.savefig(fig)
    plt.close(fig)


def _policy_page(data, pdf) -> None:
    """Cash-flow policies and basket value, kept separate from the par-valued household ledger."""
    import matplotlib.pyplot as plt

    frames                      = data["frames"]
    days                        = [f["day"] for f in frames]
    fig, axes                   = plt.subplots(2, 2, figsize=(11.7, 8.3), sharex=True)
    fig.suptitle("Household surplus policies: retirement and investment")
    for key, label in (("bcd_retired", "liability buybacks"), ("bcd_invested", "basket deposits")):
        axes[0, 0].plot(days, [f.get(key, 0) / 1e12 for f in frames], label=label)
    axes[0, 0].set_title("Cumulative household flows (million BUCKs)")
    axes[0, 1].plot(days, [f.get("bcd_investments", 0) for f in frames], label="successful deposits")
    axes[0, 1].set_title("Recurring investment, not one opening deposit")
    for policy in ("adaptive", "basket", "retire", "discount"):
        if not any(a.get("surplus_policy") == policy for f in frames for a in f.get("octl", [])):
            continue
        axes[1, 0].plot(days, [sum(a["drawn"] for a in f.get("octl", [])
                                  if a.get("surplus_policy") == policy) / 1e12 for f in frames], label=policy)
    axes[1, 0].set_title("Liabilities carried by policy (million BUCKs)")
    for key, label in (("basket", "principal"), ("basket_mark", "equity share value (before exit costs)")):
        values                  = [sum(a.get(key) or 0 for a in f.get("octl", [])) / 1e12 for f in frames]
        if key == "basket" or any(a.get(key) is not None for f in frames for a in f.get("octl", [])):
            axes[1, 1].plot(days, values, label=label)
    axes[1, 1].set_title("Households' basket assets (million BUCKs)")
    for ax in axes.flat:
        ax.grid(alpha=.2)
        ax.legend(fontsize=8)
        ax.set_xlabel("simulation day")
    fig.tight_layout(rect=(0, 0, 1, .95))
    pdf.savefig(fig)
    plt.close(fig)


def _population_page(data, pdf) -> None:
    """Who arrived and why: the averaged price gap, the arrival multipliers, and each role's numbers."""
    import matplotlib.pyplot as plt

    frames                      = [f for f in data["frames"] if f.get("pop")]
    days                        = [f["day"] for f in frames]
    roles                       = ("debtor", "saver", "investor")
    fig, axes                   = plt.subplots(2, 2, figsize=(11.7, 8.3), sharex=True)
    fig.suptitle("The household population: arrivals drawn by the price gap")
    ax                          = axes[0, 0]
    ax.plot(days, [f["basketVal"] / 1e18 for f in frames], linewidth=.6, alpha=.6, label="basket cost in BUCK")
    ax.plot(days, [f["pop"]["average"] for f in frames], label="its arrival average")
    ax.axhline(1, color="black", linewidth=.7, linestyle="--")
    ax.set_title("Below 1 the BUCK is dear (draws issuers); above, cheap (draws investors)")
    ax                          = axes[0, 1]
    for role, label in (("debtor", "debtors and savers"), ("investor", "investors")):
        ax.plot(days, [f["pop"]["multiplier"][role] for f in frames], label=label)
    ax.set_title("Arrival pace multiplier")
    ax                          = axes[1, 0]
    for role in roles:
        line,                   = ax.plot(days, [f["pop"][role]["arrived"] for f in frames], label=f"{role}s arrived")
        ax.plot(days, [f["pop"][role]["active"] for f in frames], color=line.get_color(), linestyle=":",
                label=f"{role}s present")
    ax.set_title("Households (dotted: present, not yet withdrawn)")
    ax                          = axes[1, 1]
    drawn                       = [sum(a.get("drawn", 0) for a in f.get("octl", []) if a.get("active")) / 1e12
                                   for f in frames]
    held                        = [sum((a.get("basket_mark") if a.get("basket_mark") is not None else a.get("basket", 0))
                                       for a in f.get("octl", []) if a.get("role") == "investor" and a.get("active"))
                                   / 1e12 for f in frames]
    money                       = [sum(a.get("held_buck", 0) for a in f.get("octl", [])
                                       if a.get("role") == "investor" and a.get("active")) / 1e12 for f in frames]
    ax.plot(days, drawn, label="BUCKs drawn by debtors and savers")
    ax.plot(days, money, label="BUCKs investors hold")
    ax.plot(days, held, label="investors' basket holdings (BUCKs)")
    ax.plot(days, [f.get("supply", 0) / 1e12 / 10 for f in frames], linestyle="--", label="BUCK supply / 10")
    ax.set_title("Supply and demand (million BUCKs)")
    for ax in axes.flat:
        ax.grid(alpha=.2)
        ax.legend(fontsize=7)
        ax.set_xlabel("simulation day")
    fig.tight_layout(rect=(0, 0, 1, .95))
    pdf.savefig(fig)
    plt.close(fig)


def render(vector: Path) -> dict:
    """Render atomically; accept old closed-book vectors as well as explicit completion metadata."""
    preflight()
    from matplotlib.backends.backend_pdf import PdfPages
    import matplotlib.pyplot as plt
    from alberta_buck.sim import plot_equilibrium, plot_rebalancing, plot_octl

    vector                      = Path(vector)
    raw                         = vector.read_bytes()
    data                        = json.loads(raw)
    meta                        = data.get("meta", {})
    frames                      = data.get("frames", [])
    # Older savings worlds had no direct-mint teardown, hence no completion flag. Their full
    # configured horizon is the remaining evidence; new checkpoints explicitly say completed=False.
    scenario                    = meta.get("experiment", {}).get("scenario", {})
    horizon                     = scenario.get("days") or math.ceil(scenario.get("years", 0) * 365.25)
    step                        = max(1, scenario.get("day_step", 1))
    full_horizon                = bool(frames and horizon and frames[-1]["day"] + step >= horizon)
    completed                   = meta.get("run", {}).get("completed", "teardown" in meta or full_horizon)
    if not completed or not frames:
        raise ValueError(f"{vector}: no completed run; checkpoints are not final reports")
    target                      = pdf_path(vector)
    digest                      = sha256(raw).hexdigest()
    fd, temporary               = tempfile.mkstemp(prefix=target.stem + "-", suffix=".pdf", dir=target.parent)
    os.close(fd)
    try:
        with PdfPages(temporary, metadata={"Title": vector.stem, "Subject": f"Vector SHA256: {digest}"}) as pdf:
            _credit_page(data, vector.stem, digest, pdf)
            # Keep the existing PNG renderers as the detailed pages; their console tables are redundant here.
            with redirect_stdout(io.StringIO()):
                plot_equilibrium.render(vector, target, pdf=pdf)
                plot_rebalancing.render(vector, target, pdf=pdf)
                if any(f.get("octl") for f in frames):
                    plot_octl.render(vector, target, pdf=pdf)
            if any(f.get("pop") for f in frames):
                _population_page(data, pdf)
            if any(a.get("surplus_policy") in ("adaptive", "basket", "retire", "discount")
                   for f in frames for a in f.get("octl", [])):
                _policy_page(data, pdf)
            pages               = pdf.get_pagecount()
        Path(temporary).replace(target)
    finally:
        Path(temporary).unlink(missing_ok=True)
        plt.close("all")
    last                        = frames[-1]
    tail                        = [f for f in frames if f["day"] > last["day"] - 90]
    return {"vector": str(vector), "pdf": str(target), "pages": pages, "last_day": last["day"],
            "bv_tail": sum(f["basketVal"] / 1e18 for f in tail) / len(tail),
            "k": last["buckK"] / 1e18, "fat_limit": last.get("fat_limit", 0) / 1e6,
            "refinanced": sum(a["deploys"] > 0 for a in last.get("octl", [])),
            "cohort": len(last.get("octl", []))}


def write_index(records: list[dict], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)

    def link(path, label):
        relative                = os.path.relpath(path, target.parent)
        return f'<a href="{escape(quote(relative))}">{escape(label)}</a>'

    rows                        = []
    for r in sorted(records, key=lambda r: r["vector"]):
        name                    = os.path.relpath(r["vector"], target.parent)
        rows.append(f'<tr><td>{link(r["pdf"], name.removesuffix(".json"))}</td>'
                    f'<td>{link(r["vector"], "JSON")}</td><td>{r["pages"]}</td><td>{r["last_day"]}</td>'
                    f'<td>{r["bv_tail"]:.4f}</td><td>{r["k"]:.3f}</td><td>{r["fat_limit"]:,.0f}</td>'
                    f'<td>{r["refinanced"]}/{r["cohort"]}</td></tr>')
    target.write_text('''<!doctype html><html lang="en"><meta charset="utf-8"><title>Simulation reports</title>
<style>body{font:15px system-ui;margin:2rem}table{border-collapse:collapse}td,th{padding:.4rem;border-bottom:1px solid #ddd}
th{text-align:left;position:sticky;top:0;background:white}td:nth-child(n+3){text-align:right}a{color:#1857a4}</style>
<h1>Completed simulation reports</h1>
<p>Each run links to its PDF and source JSON. Open this index directly in a browser; no server is needed.</p>
<p>BVIB is the final 90-calendar-day mean: above 1 means the BUCK is cheap against the basket, below 1 means dear.
K and the synthetic borrowers' activated limit are final-frame values. Refinanced counts people who ever deployed
credit, not continuing issuers. Frames precede the forced end-of-run teardown.</p>
''' + f'<p>{len(records)} reports.</p><table><thead><tr><th>PDF / run</th><th>Data</th><th>Pages</th><th>Last day</th>'
                      '<th>BVIB</th><th>K</th><th>Large borrowers: limit (BUCKs)</th><th>Ever issued / cohort</th>'
                      '</tr></thead><tbody>' + "\n".join(rows) + '</tbody></table></html>\n')


def main(argv=None) -> int:
    ap                          = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("vectors", nargs="+", type=Path)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--index", type=Path)
    args                        = ap.parse_args(argv)
    if args.jobs < 1:
        ap.error("--jobs must be positive")
    preflight()
    records, failures           = [], []
    with ProcessPoolExecutor(max_workers=args.jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures                 = {pool.submit(render, p): p for p in dict.fromkeys(args.vectors)}
        for future in as_completed(futures):
            try:
                record          = future.result()
                records.append(record)
                print(f"Wrote {record['pdf']} ({record['pages']} pages)", flush=True)
            except Exception as exc:
                failures.append(futures[future])
                print(f"FAILED {futures[future]}: {exc}", flush=True)
    if args.index:
        write_index(records, args.index)
        print(f"Wrote {args.index}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
