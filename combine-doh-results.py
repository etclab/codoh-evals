#!/usr/bin/env python3
"""Combine per-chunk results_har_doh.csv files into a single CSV sorted by rank.

Usage:
    ./combine-doh-results.py <batch-folder>

Example:
    ./combine-doh-results.py runs/doh-parallel-20260415-154952

Output columns: rank,site,unique_domains_resolved,domains_resolved
Output file:    <batch-folder>/combined_results_har_doh.csv
"""

import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

COLUMNS = ["rank", "site", "unique_domains_resolved", "domains_resolved"]
REVERSE_COLUMNS = ["domain", "site_count", "sites"]
COMBO_COLUMNS = ["domains", "domain_count", "site_count", "sites"]

MAX_COMBO_DEPTH = 3
MIN_COMBO_SUPPORT = 1


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <batch-folder>", file=sys.stderr)
        return 1

    batch_dir = Path(sys.argv[1])
    if not batch_dir.is_dir():
        print(f"ERROR: '{batch_dir}' is not a directory.", file=sys.stderr)
        return 1

    rows: list[dict[str, str]] = []
    sub_dirs = sorted(p for p in batch_dir.glob("top-*") if p.is_dir())

    for sub in sub_dirs:
        csv_path = sub / "results_har_doh.csv"
        if not csv_path.is_file():
            print(f"  skip (no csv): {sub}", file=sys.stderr)
            continue

        print(f"  reading: {csv_path}", file=sys.stderr)
        with csv_path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append({col: row[col] for col in COLUMNS})

    if not rows:
        print(f"ERROR: no rows found under '{batch_dir}'.", file=sys.stderr)
        return 1

    rows.sort(key=lambda r: int(r["rank"]))

    def write_csv(path: Path, data: list[dict[str, str]]) -> None:
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(data)
        print(f"Wrote {len(data)} rows to {path}")

    def write_stats(path: Path, data: list[dict[str, str]]) -> None:
        values = sorted(int(r["unique_domains_resolved"]) for r in data)
        if values:
            mean = statistics.fmean(values)
            # Nearest-rank percentile: smallest value at or above the target rank.
            def pct(p: float) -> int:
                k = max(1, -(-len(values) * p // 100))  # ceil
                return values[k - 1]
            p50, p90, p99 = pct(50), pct(90), pct(99)
        else:
            mean = p50 = p90 = p99 = 0

        with path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["metric", "value"])
            writer.writerow(["count", len(values)])
            writer.writerow(["mean", f"{mean:.4f}"])
            writer.writerow(["p50", p50])
            writer.writerow(["p90", p90])
            writer.writerow(["p99", p99])
        print(f"Wrote stats (n={len(values)} mean={mean:.4f} p50={p50} p90={p90} p99={p99}) to {path}")

    def write_reverse(path: Path, data: list[dict[str, str]]) -> None:
        domain_to_sites: dict[str, list[str]] = defaultdict(list)
        for row in data:
            domains_str = row["domains_resolved"].strip()
            if not domains_str:
                continue
            site = row["site"]
            for domain in domains_str.split(";"):
                domain = domain.strip()
                if domain:
                    domain_to_sites[domain].append(site)

        reverse_rows = sorted(
            domain_to_sites.items(), key=lambda kv: (-len(kv[1]), kv[0])
        )
        with path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(REVERSE_COLUMNS)
            for domain, sites in reverse_rows:
                writer.writerow([domain, len(sites), ";".join(sites)])
        print(f"Wrote {len(reverse_rows)} domains to {path}")

    def build_domain_to_sites(data: list[dict[str, str]]) -> dict[str, set[str]]:
        d2s: dict[str, set[str]] = defaultdict(set)
        for row in data:
            domains_str = row["domains_resolved"].strip()
            if not domains_str:
                continue
            site = row["site"]
            for domain in domains_str.split(";"):
                domain = domain.strip()
                if domain:
                    d2s[domain].add(site)
        return d2s

    def eclat(
        domain_to_sites: dict[str, set[str]],
        max_depth: int = MAX_COMBO_DEPTH,
        min_support: int = MIN_COMBO_SUPPORT,
    ) -> dict[tuple[str, ...], set[str]]:
        # Sort domains by frequency ascending (rarer first -> prune earlier)
        sorted_domains = sorted(domain_to_sites, key=lambda d: len(domain_to_sites[d]))
        domain_index = {d: i for i, d in enumerate(sorted_domains)}

        results: dict[tuple[str, ...], set[str]] = {}

        def _recurse(
            prefix: tuple[str, ...],
            prefix_sites: set[str],
            candidates: list[tuple[str, set[str]]],
            depth: int,
        ) -> None:
            for i, (domain, d_sites) in enumerate(candidates):
                combo = prefix + (domain,)
                combo_sites = prefix_sites & d_sites
                if len(combo_sites) < min_support:
                    continue
                results[combo] = combo_sites
                if depth + 1 < max_depth:
                    # Only extend with domains that come after this one
                    # in the sorted order (avoids duplicate combos)
                    next_candidates = [
                        (d2, s2)
                        for d2, s2 in candidates[i + 1 :]
                        if len(prefix_sites & d_sites & s2) >= min_support
                    ]
                    if next_candidates:
                        _recurse(combo, combo_sites, next_candidates, depth + 1)

        initial = [(d, domain_to_sites[d]) for d in sorted_domains]
        _recurse((), set().union(*domain_to_sites.values()), initial, 0)
        return results

    def write_combos(path: Path, data: list[dict[str, str]]) -> None:
        domain_to_sites = build_domain_to_sites(data)
        combos = eclat(domain_to_sites)

        # Filter to size >= 2 (single domains are already in the reverse map)
        multi_combos = {k: v for k, v in combos.items() if len(k) >= 2}

        combo_rows = sorted(
            multi_combos.items(), key=lambda kv: (-len(kv[1]), -len(kv[0]), kv[0])
        )
        with path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(COMBO_COLUMNS)
            for domains, sites in combo_rows:
                writer.writerow([
                    ";".join(domains),
                    len(domains),
                    len(sites),
                    ";".join(sorted(sites)),
                ])
        print(f"Wrote {len(combo_rows)} domain combos (depth<={MAX_COMBO_DEPTH}, support>={MIN_COMBO_SUPPORT}) to {path}")

    output = batch_dir / "combined_results_har_doh.csv"
    write_csv(output, rows)
    write_stats(output.with_suffix(output.suffix + ".stats"), rows)
    write_reverse(output.with_name("combined_results_har_doh_reverse.csv"), rows)
    # write_combos(output.with_name("combined_results_har_doh_combos.csv"), rows)

    multi_rows = [r for r in rows if int(r["unique_domains_resolved"]) > 1]
    multi_output = batch_dir / "combined_results_har_doh_multi.csv"
    write_csv(multi_output, multi_rows)
    write_stats(multi_output.with_suffix(multi_output.suffix + ".stats"), multi_rows)
    write_reverse(multi_output.with_name("combined_results_har_doh_multi_reverse.csv"), multi_rows)
    # write_combos(multi_output.with_name("combined_results_har_doh_multi_combos.csv"), multi_rows)

    return 0


if __name__ == "__main__":
    sys.exit(main())
