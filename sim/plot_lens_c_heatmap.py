"""Lens-(c) master figure: (B, T_max) heatmap of |C_7| median.

Two panels: median and p90 of the day-7 candidate-set intersection size,
with cells colored on log-scale and annotated with the median value.
Operator-tier overlay shaded as nested green regions when thresholds are
configured.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib
# Embed TrueType fonts (42) instead of the default Type 3.
matplotlib.rcParams["pdf.fonttype"] = 42
matplotlib.rcParams["ps.fonttype"] = 42
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np


def load_grid(users_csv: Path, *, k: int = 3, lambda_bg: int = 100):
    """Aggregate per-user `intersect_final` into (B, T_max) grid medians/p90s
    at the default holding axes."""
    by_cell = defaultdict(list)
    with open(users_csv) as f:
        for r in csv.DictReader(f):
            if int(r['k']) != k or int(r['lambda_bg']) != lambda_bg:
                continue
            key = (int(r['B']), float(r['T_max_s']))
            by_cell[key].append(int(r['intersect_final']))
    return by_cell


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--users-csv',
                   default='out_lensc_merged/agg/lens_c_users.csv')
    p.add_argument('--out',
                   default='out_lensc_merged/lens_c_heatmap_BTmax.pdf')
    p.add_argument('--png',
                   default='out_lensc_merged/lens_c_heatmap_BTmax.png')
    p.add_argument('--k', type=int, default=3)
    p.add_argument('--lambda-bg', type=int, default=100)
    args = p.parse_args(argv)

    by_cell = load_grid(Path(args.users_csv),
                        k=args.k, lambda_bg=args.lambda_bg)
    Bs = sorted({k_[0] for k_ in by_cell})
    Ts = sorted({k_[1] for k_ in by_cell})
    if not Bs or not Ts:
        raise SystemExit('no cells matched holding axes')

    med = np.zeros((len(Bs), len(Ts)))
    p90 = np.zeros((len(Bs), len(Ts)))
    n = np.zeros((len(Bs), len(Ts)), dtype=int)
    for i, B in enumerate(Bs):
        for j, T in enumerate(Ts):
            vs = by_cell.get((B, T), [])
            if not vs:
                med[i, j] = np.nan; p90[i, j] = np.nan
                continue
            med[i, j] = float(np.median(vs))
            p90[i, j] = float(np.percentile(vs, 90))
            n[i, j] = len(vs)

    fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), sharey=True)
    norm = mcolors.LogNorm(vmin=10, vmax=max(np.nanmax(med), np.nanmax(p90)))

    # pcolormesh on integer cell-corner coords gives clean PDF polygons
    # (imshow renders an interpolated raster that streaks under PDF viewers).
    x_edges = np.arange(len(Ts) + 1) - 0.5
    y_edges = np.arange(len(Bs) + 1) - 0.5
    for ax, mat, title in [(axes[0], med, f'median |C_7|'),
                           (axes[1], p90, f'p90 |C_7|')]:
        im = ax.pcolormesh(x_edges, y_edges, mat, cmap='viridis_r',
                           norm=norm, shading='flat',
                           edgecolors='none', linewidth=0)
        ax.set_xticks(range(len(Ts)))
        ax.set_xticklabels([f'{int(t)}' for t in Ts])
        ax.set_yticks(range(len(Bs)))
        ax.set_yticklabels([str(b) for b in Bs])
        ax.set_xlabel('T_max (s)')
        ax.set_title(title)
        ax.set_aspect('auto')
        for i in range(len(Bs)):
            for j in range(len(Ts)):
                v = mat[i, j]
                if np.isnan(v):
                    continue
                tcolor = 'white' if v < norm.vmin * 5 else 'black'
                ax.text(j, i, f'{int(v)}', ha='center', va='center',
                        color=tcolor, fontsize=10)
        fig.colorbar(im, ax=ax, fraction=0.045, pad=0.04, label='|C_7|')

    axes[0].set_ylabel('B (batch size)')
    fig.suptitle(f'Day-7 intersection-set size — k={args.k}, λ_bg={args.lambda_bg}',
                 fontsize=12)
    fig.tight_layout()
    fig.savefig(args.out, bbox_inches='tight')
    fig.savefig(args.png, dpi=150, bbox_inches='tight')
    print(f'wrote {args.out}, {args.png}')

    # Operator-tier mapping based on |C_7| since strict ≤R=10 saturates.
    print()
    print('Operator-tier mapping (|C_7| median ≥ threshold):')
    tiers = [('Strict',     200),
             ('Moderate',   100),
             ('Permissive',  50)]
    for tier, threshold in tiers:
        ok = []
        for i, B in enumerate(Bs):
            for j, T in enumerate(Ts):
                if not np.isnan(med[i, j]) and med[i, j] >= threshold:
                    ok.append((B, int(Ts[j])))
        print(f'  {tier:>10} (|C_7| >= {threshold}): {ok}')


if __name__ == '__main__':
    main()
