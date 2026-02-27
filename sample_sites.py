#!/usr/bin/env python3
"""Sample random sites from the top N in a Tranco-style CSV."""

import argparse
import csv
import random

SEED = 42
TOP_N = 2000
SAMPLE_SIZE = 200
INPUT_FILE = "top-1m.csv"
OUTPUT_FILE = "sampled-sites.csv"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sample random sites from a Tranco-style ranked CSV."
    )
    parser.add_argument(
        "-i", "--input", default=INPUT_FILE,
        help=f"Input CSV file (default: {INPUT_FILE})",
    )
    parser.add_argument(
        "-o", "--output", default=OUTPUT_FILE,
        help=f"Output CSV file (default: {OUTPUT_FILE})",
    )
    parser.add_argument(
        "-s", "--seed", type=int, default=SEED,
        help=f"Random seed (default: {SEED})",
    )
    parser.add_argument(
        "-n", "--top-n", type=int, default=TOP_N,
        help=f"Consider the top N sites from the input (default: {TOP_N})",
    )
    parser.add_argument(
        "-k", "--sample-size", type=int, default=SAMPLE_SIZE,
        help=f"Number of sites to sample (default: {SAMPLE_SIZE})",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    seed = args.seed

    # Read top N sites
    sites = []
    with open(args.input, newline="") as f:
        reader = csv.reader(f)
        for rank, domain in reader:
            if int(rank) > args.top_n:
                break
            sites.append((int(rank), domain))

    # Sample randomly with the given seed
    random.seed(seed)
    sampled = random.sample(sites, args.sample_size)
    sampled.sort(key=lambda x: x[0])  # keep sorted by original rank

    # Write output
    with open(args.output, "w", newline="") as f:
        writer = csv.writer(f)
        for rank, domain in sampled:
            writer.writerow([rank, domain])

    print(f"Sampled {len(sampled)} sites (seed={seed}) -> {args.output}")
    for rank, domain in sampled:
        print(f"  {rank}: {domain}")


if __name__ == "__main__":
    main()
