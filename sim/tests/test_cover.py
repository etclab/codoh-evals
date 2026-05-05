"""Cover-universe loader: CrUX-schema autodetect, band weighting, dedupe."""

from __future__ import annotations

import math

import pytest

from sim.cover import (
    DEFAULT_COVER_PATH,
    DEFAULT_REFERENCE_PATH,
    CoverUniverse,
    _CRUX_BANDS,
    _crux_band_weight,
)


def test_crux_band_weights_decreasing():
    """Per-origin weight strictly decreases across bands."""
    ranks = sorted(_CRUX_BANDS.keys())
    weights = [_crux_band_weight(r) for r in ranks]
    for a, b in zip(weights, weights[1:]):
        assert a > b, (a, b, ranks)


def test_crux_band_weights_match_zipf_integral():
    """Spot-check: weight(top-1k) = ln(1000)/999."""
    expected = math.log(1000) / 999
    assert _crux_band_weight(1000) == pytest.approx(expected)


def test_crux_loader_strips_scheme_and_dedupes(tmp_path):
    csv_path = tmp_path / "crux.csv"
    csv_path.write_text(
        "origin,rank\n"
        "https://a.example,1000\n"
        "http://a.example,5000\n"  # dup host, higher rank → must lose
        "https://b.example,1000\n"
        "https://c.example,10000\n",
        encoding="utf-8",
    )
    u = CoverUniverse.from_file(csv_path)
    assert len(u) == 3
    assert "a.example" in u.domains
    assert "b.example" in u.domains
    assert "c.example" in u.domains
    # a.example's surviving rank should be 1000, not 5000
    a_idx = u.domains.index("a.example")
    c_idx = u.domains.index("c.example")
    assert u.weights[a_idx] == pytest.approx(_crux_band_weight(1000))
    assert u.weights[c_idx] == pytest.approx(_crux_band_weight(10000))
    # no scheme leaked through
    assert all("://" not in d for d in u.domains)


def test_legacy_loader_still_works(tmp_path):
    """`rank,domain` headerless format (e.g., `crux-top10k-resolvable.csv`)
    still loads via the legacy path."""
    csv_path = tmp_path / "legacy.csv"
    csv_path.write_text(
        "1,a.example\n2,b.example\n3,c.example\n",
        encoding="utf-8",
    )
    u = CoverUniverse.from_file(csv_path)
    assert len(u) == 3
    assert u.domains == ["a.example", "b.example", "c.example"]
    assert u.weights[0] > u.weights[-1]  # 1/r decreases


def test_default_reference_path_loads():
    if not DEFAULT_REFERENCE_PATH.exists():
        pytest.skip(f"missing {DEFAULT_REFERENCE_PATH}")
    u = CoverUniverse.from_file(DEFAULT_REFERENCE_PATH)
    # CrUX 10k resolvable: ~9869 entries (commit ef9f844).
    assert 9_000 <= len(u) <= 10_000
    assert all("://" not in d for d in u.domains)


def test_default_cover_path_loads():
    """The headline cover universe — full CrUX 1M."""
    if not DEFAULT_COVER_PATH.exists():
        pytest.skip(f"missing {DEFAULT_COVER_PATH}")
    u = CoverUniverse.default()
    # Allow some shrinkage from http+https dedupe but expect ~1M.
    assert 900_000 <= len(u) <= 1_000_000
    assert all("://" not in d for d in u.domains)
    # Within the CrUX 1M, the smallest-rank tier (top-1k) should yield
    # the maximum per-origin weight.
    assert max(u.weights) == pytest.approx(_crux_band_weight(1000))
    assert min(u.weights) == pytest.approx(_crux_band_weight(1_000_000))