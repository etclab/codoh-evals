"""Acceptance tests for trace_loader.

Real-data regression (asserts the loader reproduces the expected page
count and `|Q_w|` distribution from the validation pass) plus
synthetic-generator plumbing checks.
Run with: `python -m sim.tests.test_trace_loader`.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sim.trace_loader import (
    Entry, generate_synthetic, load_real,
)

REAL_CSV = REPO / "runs" / "crux-combined-r1-r3" / "combined_entries_har_vanilla.csv"


def test_real_regression():
    """Reproduce numbers from validate-trace.py (2026-05-05 run)."""
    trace = load_real(REAL_CSV)
    s = trace.stats()

    # Page total — matches validate-trace [4].
    assert s["n_pages"] == 27025, s["n_pages"]
    # Day field — matches validate-trace [1].
    assert s["days"] == ["2026-05-05"], s["days"]
    # Run IDs — matches validate-trace [2].
    assert s["runs"] == [1, 2, 3], s["runs"]
    # Per-page entry-count distribution — matches validate-trace [5].
    pe = s["per_page_entries"]
    assert pe["min"] == 1 and pe["median"] == 8, pe
    assert pe["p99"] == 142 and pe["max"] == 540, pe
    # Failed-DNS row count — matches validate-trace [7].
    assert s["n_failed_dns"] == 96791, s["n_failed_dns"]
    print(f"  ok: real regression ({s['n_pages']} pages, "
          f"med/p99/max={pe['median']}/{pe['p99']}/{pe['max']}, "
          f"failed-DNS={s['n_failed_dns']})")


def test_failed_dns_first_class():
    """Failed-DNS entries (dns_ms=0) must be present in the loaded pages."""
    trace = load_real(REAL_CSV)
    found = False
    for entries in trace.pages.values():
        for e in entries:
            if e.failed:
                assert isinstance(e, Entry) and e.dns_ms == 0.0
                found = True
                break
        if found:
            break
    assert found, "no dns_ms=0 entry found in real trace"
    print("  ok: failed-DNS entries are first-class (Entry.failed flag)")


def test_monotonic_offsets():
    """Per-page started_offset_ms must be non-decreasing after load."""
    trace = load_real(REAL_CSV)
    bad = 0
    for entries in trace.pages.values():
        prev = -1.0
        for e in entries:
            if e.offset_ms + 1e-9 < prev:
                bad += 1
                break
            prev = e.offset_ms
    assert bad == 0, f"{bad} pages have non-monotonic offsets"
    print("  ok: all per-page offsets monotonic")


def test_all_runs_intact_filter():
    """Filter drops sites missing any run, so advertised `n` is honest
    (~9% of (site×run) pairs are gaps, unevenly distributed across sites)."""
    trace = load_real(REAL_CSV)
    full = trace.filter_all_runs_intact()
    rps = full.runs_per_site()
    assert all(rs == {1, 2, 3} for rs in rps.values())
    n_full_sites = len(full.site_rank)
    # Every kept site contributes exactly 3 pages.
    assert full.n_pages == n_full_sites * 3, (full.n_pages, n_full_sites)
    # Sanity: filtering is non-trivial (at least some sites dropped).
    assert n_full_sites < len(trace.site_rank)
    drop = len(trace.site_rank) - n_full_sites
    print(f"  ok: all-runs filter kept {n_full_sites} sites "
          f"(dropped {drop}), {full.n_pages} pages")


def test_magnitude_band_sample():
    trace = load_real(REAL_CSV).filter_all_runs_intact()
    sample = trace.magnitude_band_sample(random.Random(0), mid_n=200, tail_n=200)
    assert len(sample["top-1k"]) > 900  # most of 994 should survive filter
    assert len(sample["1k-5k"]) == 200
    assert len(sample["5k-10k"]) == 200
    print(f"  ok: bucket sample sizes "
          f"top-1k={len(sample['top-1k'])} 1k-5k=200 5k-10k=200")


def test_synthetic_schema():
    trace = generate_synthetic(n_sites=20, n_runs=3, seed=42)
    s = trace.stats()
    assert s["n_pages"] == 60, s["n_pages"]
    assert s["runs"] == [1, 2, 3]
    assert s["days"] == ["2026-05-05"]
    # Failed-DNS rate roughly matches request (loose bound, small N).
    failed_frac = s["n_failed_dns"] / max(1, s["n_entries"])
    assert 0.05 <= failed_frac <= 0.40, failed_frac
    # Offsets monotonic.
    for entries in trace.pages.values():
        prev = -1.0
        for e in entries:
            assert e.offset_ms >= prev, "synthetic offsets non-monotonic"
            prev = e.offset_ms
    print(f"  ok: synthetic generator ({s['n_pages']} pages, "
          f"failed-DNS frac={failed_frac:.2f})")


if __name__ == "__main__":
    print("[test_trace_loader]")
    test_real_regression()
    test_failed_dns_first_class()
    test_monotonic_offsets()
    test_all_runs_intact_filter()
    test_magnitude_band_sample()
    test_synthetic_schema()
    print("all passed.")
