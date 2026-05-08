"""α-sensitivity post-hoc analyzer.

Re-aggregates lens-(c) raw JSONL under multiple α thresholds without
re-simulating. α only affects the analyzer (`candidate_set` threshold
on `|Q_w ∩ S'_d| / |Q_w|`); the per-day `S'_d` sets are α-invariant.

Output: `out_lensc_merged/agg/lens_c_alpha_sens.csv` with one row per
(cell_hash, α) ∈ cells × {0.3, 0.5, 0.7}.
"""

from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

from .analyze_c import (aggregate_cell, analyze_user, fit_p_obs)
from .lens_c import UserDayLog, UserResult
from .trace_loader import load_real


def main():
    merged = Path('out_lensc_merged')
    trace = load_real(
        'runs/crux-combined-r1-r3/combined_entries_har_vanilla.csv'
    ).filter_all_runs_intact()
    ref = {s: hs for s, hs in trace.Q_w.items()}

    # Pull cells from any source manifest.
    cells_meta = {}
    for src in ['out_lensc_shs1_headline', 'out_lensc_shs1_k',
                'out_lensc_shs2_headline', 'out_lensc_shs2_lambda']:
        mf = json.loads(Path(src, 'raw', 'manifest.json').read_text())
        for c in mf['cells']:
            cells_meta[c['hash']] = c

    out_rows = []
    for cell_hash, cd in cells_meta.items():
        cell_dir = merged / 'raw' / f'cell_{cell_hash}'
        if not cell_dir.exists():
            continue
        users = []
        seen = set()
        for path in sorted(cell_dir.glob('lens_c_*.jsonl.gz')):
            with gzip.open(path, 'rt') as fh:
                for line in fh:
                    row = json.loads(line)
                    if 'error' in row:
                        continue
                    key = (cell_hash, row['user_idx'])
                    if key in seen:
                        continue
                    seen.add(key)
                    users.append(UserResult(
                        user_idx=row['user_idx'],
                        repertoire=row['repertoire'],
                        days=[UserDayLog(
                            d['d'], set(d['S_prime']), d['n_commits'],
                            d['n_victim_batches'], d['underflow_count'],
                            d['odoh_fallback_count'],
                        ) for d in row['days']],
                    ))
        if not users:
            continue
        for alpha in (0.3, 0.5, 0.7):
            p_obs = fit_p_obs(users, ref)
            um = [analyze_user(u, reference_set=ref, alpha=alpha,
                               p_obs=p_obs, R_size=10) for u in users]
            cm = aggregate_cell(um, cell_tag=cell_hash, p_obs=p_obs,
                                posterior_threshold=0.9)
            # |C_7| stats from per-user intersections.
            ints = sorted(m.intersections[-1]['intersect_size']
                          if m.intersections else 0
                          for m in um)
            med = ints[len(ints) // 2]
            p10 = ints[max(0, int(0.1 * len(ints)))]
            p90 = ints[min(len(ints) - 1, int(0.9 * len(ints)))]
            out_rows.append({
                'cell_hash': cell_hash, 'label': cd['label'],
                'B': cd['B'], 'T_max_s': cd['T_max_s'], 'k': cd['k'],
                'lambda_bg': cd['lambda_bg'], 'alpha': alpha,
                'days_to_fp_median': cm.days_to_fp_median,
                'days_to_fp_p10': cm.days_to_fp_p10,
                'days_to_fp_p90': cm.days_to_fp_p90,
                'n_censored': cm.n_censored,
                'C7_median': med, 'C7_p10': p10, 'C7_p90': p90,
                'rep_acc_median_final': cm.repertoire_acc_median_final,
            })

    out_path = merged / 'agg' / 'lens_c_alpha_sens.csv'
    with open(out_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f'wrote {out_path} ({len(out_rows)} rows)')


if __name__ == '__main__':
    main()
