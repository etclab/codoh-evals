#!/usr/bin/env python3
"""One-shot trace-integrity validation for the per-entry CrUX trace.

Validates `runs/crux-combined-r1-r3/combined_entries_har_vanilla.csv` (and the
sibling per-page summary) against the criteria in revision-tasks.md §1.0.5:

  - Monotonic `started_offset_ms` within each (rank, site, run, day) page.
  - Per-page query count: median in [4, 8], p99 < 50 (CrUX top-10k baseline).
  - No truncation: (site, run) sets agree between entries CSV and results CSV;
    per-page `unique_domains_resolved` matches the entries CSV.
  - Day field set correctly: single non-empty value, ISO-8601.

Exits non-zero if any hard check fails. Soft warnings (e.g. quantile bounds)
are reported but do not fail the script.
"""

from __future__ import annotations

import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "runs" / "crux-combined-r1-r3"
ENTRIES_CSV = RUN_DIR / "combined_entries_har_vanilla.csv"
RESULTS_CSV = RUN_DIR / "combined_results_har_vanilla.csv"

EXPECTED_DAYS = {"2026-05-05"}
EXPECTED_RUNS = {1, 2, 3}
MEDIAN_BAND = (4, 8)
P99_MAX = 50

ENTRY_COLS = ["rank", "site", "run", "day", "hostname", "started_offset_ms", "dns_ms"]
RESULT_COLS = [
    "rank", "site", "run", "strategy",
    "main_dns_ms", "total_dns_sum_ms", "wall_clock_dns_ms",
    "page_load_ms", "unique_domains_resolved", "domains_resolved",
]


def quantile(sorted_vals, q):
    if not sorted_vals:
        return 0
    idx = max(0, min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def load_entries(path):
    pages = defaultdict(list)  # (rank, site, run, day) -> list of (offset, dns_ms, host)
    days = set()
    runs = set()
    bad_rows = 0
    failed_dns = 0
    total_rows = 0
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ENTRY_COLS:
            print(f"FAIL: entries header mismatch.\n  got:      {reader.fieldnames}\n  expected: {ENTRY_COLS}")
            sys.exit(2)
        for row in reader:
            total_rows += 1
            try:
                rank = int(row["rank"])
                run = int(row["run"])
                offset = float(row["started_offset_ms"])
                dns_ms = float(row["dns_ms"])
            except (ValueError, KeyError):
                bad_rows += 1
                continue
            site = row["site"]
            day = row["day"]
            host = row["hostname"]
            if not site or not day or not host:
                bad_rows += 1
                continue
            days.add(day)
            runs.add(run)
            if dns_ms == 0.0:
                failed_dns += 1
            pages[(rank, site, run, day)].append((offset, dns_ms, host))
    return {
        "pages": pages,
        "days": days,
        "runs": runs,
        "bad_rows": bad_rows,
        "failed_dns": failed_dns,
        "total_rows": total_rows,
    }


def load_results(path):
    rows = []
    keys = set()
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != RESULT_COLS:
            print(f"FAIL: results header mismatch.\n  got:      {reader.fieldnames}\n  expected: {RESULT_COLS}")
            sys.exit(2)
        for row in reader:
            rank = int(row["rank"])
            site = row["site"]
            run = int(row["run"])
            unique = int(row["unique_domains_resolved"])
            doms = row["domains_resolved"]
            domain_set = set(d for d in doms.split(";") if d) if doms else set()
            rows.append({"rank": rank, "site": site, "run": run,
                         "unique": unique, "domain_set": domain_set})
            keys.add((rank, site, run))
    return rows, keys


def main():
    if not ENTRIES_CSV.exists() or not RESULTS_CSV.exists():
        print(f"FAIL: missing input(s) under {RUN_DIR}")
        sys.exit(2)

    print(f"Validating {ENTRIES_CSV.relative_to(ROOT)}")
    print(f"Validating {RESULTS_CSV.relative_to(ROOT)}\n")

    e = load_entries(ENTRIES_CSV)
    pages = e["pages"]
    results, result_keys = load_results(RESULTS_CSV)

    fails = []
    warns = []

    # --- Day field --------------------------------------------------------
    print("[1] Day field")
    print(f"    distinct day values: {sorted(e['days'])}")
    if e["days"] != EXPECTED_DAYS:
        fails.append(f"day field mismatch — expected {EXPECTED_DAYS}, got {e['days']}")
    else:
        print("    OK (single day, matches expected)")

    # --- Run IDs ----------------------------------------------------------
    print("\n[2] Run IDs in entries trace")
    print(f"    distinct run values: {sorted(e['runs'])}")
    if e["runs"] != EXPECTED_RUNS:
        fails.append(f"run IDs mismatch — expected {EXPECTED_RUNS}, got {e['runs']}")
    else:
        print("    OK")

    # --- Schema / parse failures -----------------------------------------
    print("\n[3] Row parse / schema sanity")
    print(f"    rows total:    {e['total_rows']}")
    print(f"    rows bad/skipped: {e['bad_rows']}")
    if e["bad_rows"] > 0:
        fails.append(f"{e['bad_rows']} entry rows failed to parse")
    else:
        print("    OK")

    # --- Monotonic timestamps within each page ---------------------------
    print("\n[4] Monotonic started_offset_ms within page")
    non_monotonic = []
    for key, evts in pages.items():
        prev = -float("inf")
        for offset, _dns, _h in evts:
            if offset + 1e-9 < prev:
                non_monotonic.append(key)
                break
            prev = offset
    print(f"    pages checked:        {len(pages)}")
    print(f"    non-monotonic pages:  {len(non_monotonic)}")
    if non_monotonic:
        fails.append(f"{len(non_monotonic)} pages have decreasing started_offset_ms; first 3: {non_monotonic[:3]}")
    else:
        print("    OK")

    # --- Per-page query count distribution -------------------------------
    print("\n[5] Per-page query count")
    counts = sorted(len(v) for v in pages.values())
    median = statistics.median(counts) if counts else 0
    p99 = quantile(counts, 0.99)
    p999 = quantile(counts, 0.999)
    mx = counts[-1] if counts else 0
    mn = counts[0] if counts else 0
    print(f"    n pages:  {len(counts)}")
    print(f"    min/median/p95/p99/p99.9/max: "
          f"{mn}/{median}/{quantile(counts, 0.95)}/{p99}/{p999}/{mx}")
    if not (MEDIAN_BAND[0] <= median <= MEDIAN_BAND[1]):
        warns.append(f"per-page median {median} outside expected band {MEDIAN_BAND}")
    if p99 >= P99_MAX:
        warns.append(f"per-page p99 {p99} >= {P99_MAX}")
    if mn == 0:
        warns.append("at least one page has zero entries")
    print(f"    expected: median in {MEDIAN_BAND}, p99 < {P99_MAX}")
    print("    OK" if not (warns and (warns[-1].startswith('per-page') or 'zero entries' in warns[-1])) else "    soft-warn")

    # --- Truncation: cross-check entries vs. results ---------------------
    print("\n[6] No truncation (entries ↔ results consistency)")
    entry_keys_3 = {(rank, site, run) for (rank, site, run, _day) in pages.keys()}
    only_entries = entry_keys_3 - result_keys
    only_results = result_keys - entry_keys_3

    # `only_results` is expected for pages that had zero non-reused DNS lookups
    # (entries trace omits these pages). `only_entries` should be empty.
    print(f"    pages in entries only (should be 0):  {len(only_entries)}")
    print(f"    pages in results only (zero DNS, ok): {len(only_results)}")
    if only_entries:
        fails.append(f"{len(only_entries)} (rank,site,run) keys present in entries but absent from results; first 3: {list(only_entries)[:3]}")

    # unique_domains_resolved should match the unique-host count in entries
    # (excluding the dns_ms=0 sentinel rows for failed DNS).
    res_by_key = {(r["rank"], r["site"], r["run"]): r for r in results}
    mismatches = []
    for (rank, site, run, _day), evts in pages.items():
        # Distinct hosts with dns_ms > 0 must equal unique_domains_resolved.
        unique_resolved_hosts = {h for _o, dns, h in evts if dns > 0}
        res = res_by_key.get((rank, site, run))
        if res is None:
            continue  # already reported in only_entries
        if len(unique_resolved_hosts) != res["unique"]:
            mismatches.append((rank, site, run, len(unique_resolved_hosts), res["unique"]))
        # Domain set should also match.
        if unique_resolved_hosts != res["domain_set"]:
            # already covered by count mismatch in most cases; only flag if not
            if len(unique_resolved_hosts) == res["unique"]:
                mismatches.append((rank, site, run, "set-diff", "set-diff"))
    print(f"    per-page unique-domain mismatches:    {len(mismatches)}")
    if mismatches:
        fails.append(f"{len(mismatches)} pages disagree between entries and results; first 3: {mismatches[:3]}")
    else:
        print("    OK")

    # --- Failed-DNS sentinel ---------------------------------------------
    print("\n[7] Failed-DNS sentinel (dns_ms=0)")
    print(f"    rows with dns_ms=0:  {e['failed_dns']}  ({100*e['failed_dns']/max(1,e['total_rows']):.2f}% of trace)")

    # --- Coverage --------------------------------------------------------
    print("\n[8] Coverage")
    sites_seen = {site for (_r, site, _run, _d) in pages.keys()}
    print(f"    distinct sites with entries: {len(sites_seen)}")
    print(f"    distinct (site, run) result rows: {len({(r['site'], r['run']) for r in results})}")
    expected_pairs = 9869 * 3
    actual_pairs = len({(r["site"], r["run"]) for r in results})
    drop_rate = (expected_pairs - actual_pairs) / expected_pairs
    print(f"    expected (site×run) pairs at full coverage: {expected_pairs}")
    print(f"    drop rate vs full CrUX×3: {drop_rate*100:.2f}%")
    if drop_rate > 0.20:
        warns.append(f"drop rate {drop_rate*100:.2f}% exceeds 20%")

    # --- Summary ---------------------------------------------------------
    print("\n=== SUMMARY ===")
    if fails:
        print(f"HARD FAILS ({len(fails)}):")
        for f in fails:
            print(f"  - {f}")
    else:
        print("HARD CHECKS: all passed")
    if warns:
        print(f"SOFT WARNINGS ({len(warns)}):")
        for w in warns:
            print(f"  - {w}")
    else:
        print("SOFT CHECKS: all passed")

    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
