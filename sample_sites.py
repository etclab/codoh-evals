#!/usr/bin/env python3
"""Sample 200 random sites from the top 2000 in Tranco top-1m.csv."""

import csv
import random
import sys

SEED = 42
TOP_N = 2000
SAMPLE_SIZE = 200
INPUT_FILE = "top-1m.csv"
OUTPUT_FILE = "sampled-sites.csv"


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else SEED

    # Read top 2000 sites
    sites = []
    with open(INPUT_FILE, newline="") as f:
        reader = csv.reader(f)
        for rank, domain in reader:
            if int(rank) > TOP_N:
                break
            sites.append((int(rank), domain))

    # Sample 200 randomly with the given seed
    random.seed(seed)
    sampled = random.sample(sites, SAMPLE_SIZE)
    sampled.sort(key=lambda x: x[0])  # keep sorted by original rank

    # Write output
    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        for rank, domain in sampled:
            writer.writerow([rank, domain])

    print(f"Sampled {len(sampled)} sites (seed={seed}) -> {OUTPUT_FILE}")
    for rank, domain in sampled:
        print(f"  {rank}: {domain}")


if __name__ == "__main__":
    main()
