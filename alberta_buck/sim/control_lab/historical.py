"""Pinned historical inputs; endpoint adjustment belongs to the generator only."""
import csv
import hashlib
import math
from pathlib import Path

PRICE_DIR = Path(__file__).resolve().parents[1] / "prices"
DEFAULT_FILES = ["hist-paxg.csv", "hist-food.csv", "hist-nrgc.csv"]


def historical_inputs(cfg, profile):
    spec = profile["history"]
    files = spec.get("files", DEFAULT_FILES)
    if len(files) != 3 or len(set(files)) != 3:
        raise ValueError("historical scenario requires three distinct CSVs")
    series, sources = [], []
    for name in files:
        if Path(name).name != name or not name.endswith(".csv"):
            raise ValueError("historical CSV must be a filename in the saved prices directory")
        path = PRICE_DIR / name
        with path.open() as stream:
            rows = list(csv.DictReader(stream))
        if [int(r["day"]) for r in rows] != list(range(len(rows))):
            raise ValueError("historical days must be consecutive from zero")
        values = [int(r["close_usd_micro"]) / 1e6 for r in rows]
        if not values or any(not math.isfinite(p) or p <= 0 for p in values):
            raise ValueError("historical prices must be finite and positive")
        series.append(values)
        sources.append(dict(file=name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), rows=len(rows)))
    days, warm = cfg["days"], cfg["bootstrap"]["history_days"]
    start = spec.get("start_day", min(map(len, series)) - days - 1)
    if not isinstance(start, int) or start < warm or start + days >= min(map(len, series)):
        raise ValueError("historical window needs complete prehistory and live endpoints")
    # These reference means use only pre-test samples, never the bridged future.
    means = [sum(v[start-warm:start]) / warm for v in series]
    scales = [base / mean for base, mean in zip(cfg["feed"]["base"], means)]
    history = [dict(day=d, prices=[round(v[start+d]*s, 6) for v,s in zip(series,scales)])
               for d in range(-warm, 0)]
    endpoint = [math.log(v[start+days]/v[start]) for v in series]
    rows = []
    for step in range(days*cfg["ticks_per_day"]+1):
        day = step/cfg["ticks_per_day"]
        # Daily closes are held between days; no invented intraday observations.
        raw = [v[start+int(day)]*s for v,s in zip(series,scales)]
        prices = [round(p*math.exp(-day/days*end), 6) for p,end in zip(raw,endpoint)]
        rows.append(dict(day=day, target_usd=prices, historical_unadjusted_usd=[round(p,6) for p in raw]))
    return dict(rows=rows, history=history, provenance=dict(kind="historical-bridge",
        labels=spec.get("labels", [Path(f).stem for f in files]), sources=sources,
        start_day=start, end_day=start+days, history_days=warm,
        historical_means_usd=means, normalized_means=cfg["feed"]["base"],
        removed_log_change=endpoint,
        method="log P(t) - t/T log(P(T)/P(0)); each TOKEN ends at its opening price. "
               "Prior-only reference means; daily stored quotes, including synthetic bridges between monthly anchors. "
               "Endpoint knowledge constructs the scenario and is never supplied to an estimator."))
