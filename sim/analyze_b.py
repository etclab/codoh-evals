"""Lens-(b) aggregation: cross-batch-union page-identification accuracy.

Reads `out/raw/cell_<hash>/trials.jsonl.gz` (one row per trial, lens_b is a
single AttackResult dict) and emits `out/agg/lens_b.csv` with one row per
cell:

    cell_hash, B, T_max_s, k, lambda_bg, N, alpha, D, init,
    n_trials, mean_cand_size, p10_cand_size, p90_cand_size,
    frac_cand_le_k, top1_acc, top5_acc, mean_mrr, mean_victim_score

`frac_cand_le_k` is the fraction of trials where |candidates| ≤ cell.k —
operator-side threshold corresponding to "tight enough that the privacy set
collapses below the per-query cover ratio."
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import statistics
import sys
from pathlib import Path


CSV_HEADER = [
    "cell_hash", "B", "T_max_s", "k", "lambda_bg", "N", "alpha", "D", "init",
    "n_trials", "mean_cand_size", "p10_cand_size", "p90_cand_size",
    "frac_cand_le_k", "frac_cand_le_5", "frac_cand_le_10",
    "top1_acc", "top5_acc", "mean_mrr", "mean_victim_score",
]


def aggregate_cell(cell_meta: dict, trials_paths: list[Path]) -> dict:
    cands: list[int] = []
    top1 = 0
    top5 = 0
    rrs: list[float] = []
    scores: list[float] = []
    n = 0
    for trials_path in trials_paths:
        with gzip.open(trials_path, "rt") as fh:
            for line in fh:
                row = json.loads(line)
                b = row["lens_b"]
                cands.append(b["cand_size"])
                top1 += int(b["top1"])
                top5 += int(b["top5"])
                rrs.append(b["rr"])
                scores.append(b["victim_score"])
                n += 1
    if n == 0:
        return {**_meta_cols(cell_meta), "n_trials": 0}
    cands_sorted = sorted(cands)
    out = {
        **_meta_cols(cell_meta),
        "n_trials": n,
        "mean_cand_size": statistics.fmean(cands),
        "p10_cand_size": _q(cands_sorted, 0.10),
        "p90_cand_size": _q(cands_sorted, 0.90),
        "frac_cand_le_k": sum(1 for c in cands if c <= cell_meta["k"]) / n,
        "frac_cand_le_5": sum(1 for c in cands if c <= 5) / n,
        "frac_cand_le_10": sum(1 for c in cands if c <= 10) / n,
        "top1_acc": top1 / n,
        "top5_acc": top5 / n,
        "mean_mrr": statistics.fmean(rrs),
        "mean_victim_score": statistics.fmean(scores),
    }
    return out


def _meta_cols(m: dict) -> dict:
    keys = ("B", "T_max_s", "k", "lambda_bg", "N", "alpha", "D", "init")
    return {"cell_hash": m.get("hash", m.get("cell_hash")),
            **{k: m[k] for k in keys if k in m}}


def _q(sorted_vals, q: float):
    if not sorted_vals:
        return 0
    idx = max(0, min(len(sorted_vals) - 1,
                     int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sim.analyze_b")
    p.add_argument("--out-dir", default="out",
                   help="sweep output root (with raw/ + agg/)")
    args = p.parse_args(argv)
    root = Path(args.out_dir)
    manifest_path = root / "raw" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    rows = []
    for cell_meta in manifest["cells"]:
        h = cell_meta["hash"]
        cell_dir = root / "raw" / f"cell_{h}"
        trials_paths = sorted(cell_dir.glob("trials*.jsonl.gz"))
        if not trials_paths:
            print(f"  skip {h}: no chunks in {cell_dir}", file=sys.stderr)
            continue
        rows.append(aggregate_cell(cell_meta, trials_paths))
    out_csv = root / "agg" / "lens_b.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_HEADER})
    print(f"[lens-b] wrote {len(rows)} rows -> {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
