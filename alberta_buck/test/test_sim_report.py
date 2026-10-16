"""Completed vectors produce PDFs; checkpoints and failed rendering cannot masquerade as finished reports."""

import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from alberta_buck.sim import report
from alberta_buck.sim.snapshot import Snapshotter


@pytest.fixture
def snapshot():
    # Use the real saved schema and writer, with just two observations to keep rendering fast.
    source                      = Path(__file__).resolve().parents[2] / "test/vectors/rebalancing-sim.json"
    data                        = json.loads(source.read_text())
    snap                        = Snapshotter.__new__(Snapshotter)
    snap.tokens                 = data["tokens"]
    snap.d                      = SimpleNamespace(dec=data["decimals"])
    snap.s                      = SimpleNamespace(days=1827)
    snap.meta                   = {"run": {"completed": False, "days": 1827}}
    snap.frames                 = [data["frames"][0], data["frames"][-1]]
    for day, frame in zip((0, 1826), snap.frames):
        frame["day"]            = day
        frame["octl"]           = [{"idx": 0, "theta": 1., "pattern": "salary", "nw": 0, "hypo": 0,
                                    "mortgage": 0, "drawn": 1000000, "limit": 2000000, "deploys": 1,
                                    "active": True}]
    return snap


def test_five_year_completion_writes_a_real_multipage_pdf(snapshot, tmp_path):
    path, pdf                   = report.finish(snapshot, tmp_path / "five.years.partial")
    assert json.loads(path.read_text())["meta"]["run"]["completed"] is True
    assert pdf == tmp_path / "five.years.pdf"
    raw                         = pdf.read_bytes()
    assert raw.startswith(b"%PDF-") and raw.rstrip().endswith(b"%%EOF")
    assert len(re.findall(rb"/Type /Page\b", raw)) == 4
    assert b"Vector SHA256:" in raw
    assert report.pdf_path(path.with_suffix(".json")) == pdf


def test_checkpoint_is_not_a_completed_report(snapshot, tmp_path):
    path                        = snapshot.write(tmp_path / "checkpoint.json")
    with pytest.raises(ValueError, match="no completed run"):
        report.render(path)
    assert not report.pdf_path(path).exists()


def test_adaptive_households_get_the_policy_page(snapshot, tmp_path):
    for frame in snapshot.frames:
        frame["octl"][0]["surplus_policy"] = "adaptive"
    _, pdf                      = report.finish(snapshot, tmp_path / "households.json")
    assert len(re.findall(rb"/Type /Page\b", pdf.read_bytes())) == 5


def test_old_savings_vector_needs_its_full_configured_horizon(snapshot, tmp_path):
    snapshot.meta               = {"experiment": {"scenario": {"years": 5., "day_step": 1}}}
    snapshot.frames[-1]["day"]   = 1825
    path                        = snapshot.write(tmp_path / "old.json")
    with pytest.raises(ValueError, match="no completed run"):
        report.render(path)
    snapshot.frames[-1]["day"]   = 1826
    snapshot.write(path)
    assert report.render(path)["pages"] == 4


def test_failed_pdf_preserves_completed_json_and_previous_report(snapshot, tmp_path, monkeypatch):
    from alberta_buck.sim import plot_equilibrium

    def broken(*args, **kwargs):
        raise RuntimeError("renderer failed")

    monkeypatch.setattr(plot_equilibrium, "render", broken)
    path                        = tmp_path / "saved.json"
    pdf                         = report.pdf_path(path)
    pdf.write_bytes(b"previous report")
    with pytest.raises(RuntimeError, match="without rerunning the simulation"):
        report.finish(snapshot, path)
    assert json.loads(path.read_text())["meta"]["run"]["completed"] is True
    assert pdf.read_bytes() == b"previous report"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["saved.json", "saved.pdf"]


def test_short_and_unsaved_runs_do_not_render(snapshot, tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("short or unsaved run tried to render")

    monkeypatch.setattr(report, "render", unexpected)
    assert report.finish(snapshot, None) == (None, None)
    snapshot.s.days             = 1824
    path, pdf                   = report.finish(snapshot, tmp_path / "short.json")
    assert path.exists() and pdf is None


def test_index_links_stay_relative_and_escape_filenames(tmp_path):
    data                        = tmp_path / 'a & b.json'
    record                      = {"vector": str(data), "pdf": str(report.pdf_path(data)), "pages": 3,
                                   "last_day": 1826, "bv_tail": 1.01, "k": .75, "fat_limit": 1234,
                                   "refinanced": 2, "cohort": 3}
    target                      = tmp_path / "index.html"
    report.write_index([record], target)
    text                        = target.read_text()
    assert 'href="a%20%26%20b.pdf"' in text
    assert "a &amp; b" in text
    assert "1.0100" in text and "2/3" in text
