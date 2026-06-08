"""Lens-(c) headline figure: |C_7| (size of day-7 candidate-set intersection)
vs each swept axis.

Three panels:
  - |C_7| vs B at default (k=3, λ_bg=100, T_max=300)
  - |C_7| vs k at default (B=20, T_max=300, λ_bg=100)
  - |C_7| vs λ_bg at default (B=20, T_max=300, k=3)

Each panel shows median + p10/p90 band over n_users users per cell.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def load(users_csv: Path):
    """Group `intersect_final` per (B, T_max, k, λ_bg) cell."""
    by_cell = defaultdict(list)
    with open(users_csv) as f:
        for r in csv.DictReader(f):
            key = (int(r['B']), float(r['T_max_s']), int(r['k']),
                   int(r['lambda_bg']))
            by_cell[key].append(int(r['intersect_final']))
    return by_cell


def quants(values):
    a = np.asarray(values)
    return np.percentile(a, 10), np.median(a), np.percentile(a, 90)


def panel(ax, by_cell, axis, fixed, xlabel, *, log_x=False):
    """Plot median + p10/p90 band over `axis` while holding `fixed` axes."""
    points = []
    for (B, T, k, bg), vs in by_cell.items():
        if all(_match(v, x) for v, x in zip((B, T, k, bg), fixed)):
            x = {'B': B, 'T': T, 'k': k, 'bg': bg}[axis]
            points.append((x, *quants(vs), len(vs)))
    points.sort()
    xs = [p[0] for p in points]
    p10s = [p[1] for p in points]
    meds = [p[2] for p in points]
    p90s = [p[3] for p in points]
    ns = [p[4] for p in points]
    ax.fill_between(xs, p10s, p90s, alpha=0.2, label='p10–p90')
    ax.plot(xs, meds, marker='o', linewidth=2, label=f'median (n={ns[0]})')
    if log_x:
        ax.set_xscale('log')
    ax.set_xlabel(xlabel)
    ax.set_yscale('log')
    ax.set_xticks(xs)
    ax.set_xticklabels([str(x) for x in xs])
    ax.grid(True, which='both', alpha=0.3)
    return points


def _match(actual, target):
    if target == '*':
        return True
    return actual == target


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--users-csv', default='out_lensc_merged/agg/lens_c_users.csv')
    p.add_argument('--out', default='out_lensc_merged/lens_c_intersect_final.pdf')
    p.add_argument('--png', default='out_lensc_merged/lens_c_intersect_final.png')
    args = p.parse_args(argv)

    by_cell = load(Path(args.users_csv))
    fig, axes = plt.subplots(1, 3, figsize=(13, 4), sharey=True)

    # Panel A: B-axis at default (k=3, λ_bg=100, T=300)
    pts_a = panel(axes[0], by_cell, 'B',
                  fixed=('*', 300.0, 3, 100),
                  xlabel='B (batch size)', log_x=True)
    axes[0].set_ylabel(r'$|C_7|$ (day-7 intersection size)')
    axes[0].set_title('k=3, λ_bg=100, T_max=300')

    # Panel B: k-axis at default (B=20, T=300, λ_bg=100)
    pts_b = panel(axes[1], by_cell, 'k',
                  fixed=(20, 300.0, '*', 100),
                  xlabel='k (covers per query)')
    axes[1].set_title('B=20, λ_bg=100, T_max=300')

    # Panel C: λ_bg-axis at default (B=20, T=300, k=3)
    pts_c = panel(axes[2], by_cell, 'bg',
                  fixed=(20, 300.0, 3, '*'),
                  xlabel='λ_bg (concurrent users)', log_x=True)
    axes[2].set_title('B=20, k=3, T_max=300')

    # Annotate horizontal reference: |R| = 10 (the strict ≤R fingerprint
    # threshold) — every cell's median sits well above it; that's the
    # "all cells censor at days_to_fp=7" finding visualized.
    for ax in axes:
        ax.axhline(10, color='red', linestyle='--', linewidth=1, alpha=0.6,
                   label='|R| = 10 (fingerprint threshold)')
        ax.legend(loc='upper left', fontsize=8)

    fig.suptitle('Day-7 candidate-set intersection size — cross-day attack',
                 fontsize=12, y=1.02)
    fig.tight_layout()
    fig.savefig(args.out, bbox_inches='tight')
    fig.savefig(args.png, dpi=150, bbox_inches='tight')
    print(f'wrote {args.out} and {args.png}')

    # Print the data behind the figure for the writeup.
    print()
    for label, pts in [('B', pts_a), ('k', pts_b), ('λ_bg', pts_c)]:
        print(f'{label}-axis:')
        for x, p10, med, p90, n in pts:
            print(f'  {label}={x:>5}  p10={p10:>6.0f}  med={med:>6.0f}  '
                  f'p90={p90:>6.0f}  n={n}')


if __name__ == '__main__':
    main()
