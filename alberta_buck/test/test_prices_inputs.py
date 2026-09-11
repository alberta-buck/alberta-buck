"""WAVE3.org decision 21: the price loader refuses partial or inconsistent
window files instead of silently running a shorter simulation, and the
generator's helpers write atomically."""

import csv
import json

import pytest

from alberta_buck.sim import gen_historical as G
from alberta_buck.sim import prices as P


def _write(d, name, n, short_row_at=None, scale=1_000_000):
    with (d / name).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "close_usd_micro"])
        for k in range(n):
            if short_row_at == k:
                w.writerow([k])
            else:
                w.writerow([k, scale * (k + 1)])


A, B = "hist-a-2025-09-01-5d.csv", "hist-b-2025-09-01-5d.csv"


def test_equal_lengths_load(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "CSV_DIR", tmp_path)
    _write(tmp_path, A, 5)
    _write(tmp_path, B, 5)
    p = P.Prices([A, B])
    assert p.days == 5
    assert p.ref(0, 4) == 5_000_000 // P.PRICE_SCALE


def test_unequal_lengths_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "CSV_DIR", tmp_path)
    _write(tmp_path, A, 5)
    _write(tmp_path, B, 3)
    with pytest.raises(P.PricesIncomplete) as ei:
        P.Prices([A, B])
    assert A in str(ei.value) and "=3" in str(ei.value)


def test_short_row_refused_with_location(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "CSV_DIR", tmp_path)
    _write(tmp_path, A, 5, short_row_at=2)
    with pytest.raises(P.PricesIncomplete) as ei:
        P.Prices([A])
    assert "line 4" in str(ei.value)      # header is line 1, day 2 is line 4


def test_manifest_length_mismatch_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "CSV_DIR", tmp_path)
    _write(tmp_path, A, 5)
    _write(tmp_path, B, 5)
    (tmp_path / "hist-manifest-2025-09-01-5d.json").write_text(
        json.dumps({"n_days": 7, "files": [A, B]}))
    with pytest.raises(P.PricesIncomplete) as ei:
        P.Prices([A, B])
    assert "manifest says 7" in str(ei.value)


def test_manifest_match_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "CSV_DIR", tmp_path)
    _write(tmp_path, A, 5)
    _write(tmp_path, B, 5)
    (tmp_path / "hist-manifest-2025-09-01-5d.json").write_text(
        json.dumps({"n_days": 5, "files": [A, B]}))
    assert P.Prices([A, B]).days == 5


def test_unstamped_files_need_no_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "CSV_DIR", tmp_path)
    _write(tmp_path, "paxg.csv", 4)
    assert P.Prices(["paxg.csv"]).days == 4


def test_atomic_write_leaves_no_temp(tmp_path):
    target = tmp_path / "hist-manifest-x.json"
    G._atomic_write_text(target, "{}")
    assert target.read_text() == "{}"
    assert [p.name for p in tmp_path.iterdir()] == ["hist-manifest-x.json"]
