#!/usr/bin/env python3
"""Parse results_har_odoh.csv and output CDF data suitable for gnuplot."""

import argparse
import csv

def compute_cdf(values):
    """Return sorted values and their cumulative fractions."""
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    cdf = [(i + 1) / n for i in range(n)]
    return sorted_vals, cdf

def main():
    parser = argparse.ArgumentParser(
        description="Parse a HAR benchmark CSV and output CDF data files suitable for gnuplot."
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        default="results_har_odoh.csv",
        help="Input CSV file with benchmark results (default: results_har_odoh.csv)",
    )
    parser.add_argument(
        "-o", "--output",
        default="cdf_odoh.dat",
        help="Output .dat file for the main CDF data (default: cdf_odoh.dat)",
    )
    args = parser.parse_args()

    input_file = args.input_file
    output_file = args.output

    total_dns = []
    wall_clock_dns = []
    page_load = []

    with open(input_file, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = float(row["total_dns_sum_ms"])
            w = float(row["wall_clock_dns_ms"])
            p = float(row["page_load_ms"])
            if t == 0 or w == 0 or p == 0:
                continue
            total_dns.append(t)
            wall_clock_dns.append(w)
            page_load.append(p)

    datasets = [
        ("total_dns_sum_ms", total_dns),
        ("wall_clock_dns_ms", wall_clock_dns),
        ("page_load_ms", page_load),
    ]

    with open(output_file, "w") as out:
        for i, (label, values) in enumerate(datasets):
            sorted_vals, cdf = compute_cdf(values)
            out.write(f"# Dataset {i}: {label}\n")
            for v, c in zip(sorted_vals, cdf):
                out.write(f"{v:.4f}\t{c:.6f}\n")
            out.write("\n\n")  # two blank lines separate gnuplot datasets

    print(f"Wrote {output_file} with {len(datasets)} datasets "
          f"({len(total_dns)} points each)")

    # DNS ratio: wall_clock_dns_ms / page_load_ms (fraction of page load spent on DNS)
    dns_ratio = [w / p for w, p in zip(wall_clock_dns, page_load)]
    ratio_file = output_file.replace(".dat", "_dns_ratio.dat")
    sorted_vals, cdf = compute_cdf(dns_ratio)
    with open(ratio_file, "w") as out:
        out.write("# CDF of wall_clock_dns_ms / page_load_ms\n")
        for v, c in zip(sorted_vals, cdf):
            out.write(f"{v:.6f}\t{c:.6f}\n")

    print(f"Wrote {ratio_file} with {len(dns_ratio)} points")
    
if __name__ == "__main__":
    main()
