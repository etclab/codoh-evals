"""Lens-(a) aggregation: per-batch identification accuracy by victim bucket.

Reads `out/raw/cell_<hash>/trials.jsonl.gz` and emits
`out/agg/lens_a.csv` with one row per (cell, bucket) — each trial's
`lens_a` field is a list of per-victim-batch AttackResult dicts; we
flatten across batches and average within (cell, bucket).

Columns:
    cell_hash, B, T_max_s, k, lambda_bg, N, alpha, D, init,
    bucket, n_batches, mean_cand_size, p10_cand_size, p90_cand_size,
    frac_cand_le_k, top1_acc, top5_acc, mean_mrr
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


CSV_HEADER = [
    "cell_hash", "B", "T_max_s", "k", "lambda_bg", "N", "alpha", "D", "init",
    "bucket", "n_batches", "mean_cand_size", "p10_cand_size", "p90_cand_size",
    "frac_cand_le_k", "frac_cand_le_5", "frac_cand_le_10",
    "top1_acc", "top5_acc", "mean_mrr",
]


def aggregate_cell(cell_meta: dict, trials_paths: list[Path]) -> list[dict]:
    by_bucket: dict[str, dict] = defaultdict(
        lambda: {"cand": [], "top1": 0, "top5": 0, "rr": [], "n": 0}
    )
    for trials_path in trials_paths:
        with gzip.open(trials_path, "rt") as fh:
            for line in fh:
                row = json.loads(line)
                bucket = row["bucket"]
                for b in row["lens_a"]:
                    acc = by_bucket[bucket]
                    acc["cand"].append(b["cand_size"])
                    acc["top1"] += int(b["top1"])
                    acc["top5"] += int(b["top5"])
                    acc["rr"].append(b["rr"])
                    acc["n"] += 1
    out = []
    for bucket, acc in by_bucket.items():
        n = acc["n"]
        if n == 0:
            continue
        cands = sorted(acc["cand"])
        out.append({
            **_meta_cols(cell_meta),
            "bucket": bucket,
            "n_batches": n,
            "mean_cand_size": statistics.fmean(acc["cand"]),
            "p10_cand_size": _q(cands, 0.10),
            "p90_cand_size": _q(cands, 0.90),
            "frac_cand_le_k": sum(1 for c in acc["cand"]
                                  if c <= cell_meta["k"]) / n,
            "frac_cand_le_5": sum(1 for c in acc["cand"] if c <= 5) / n,
            "frac_cand_le_10": sum(1 for c in acc["cand"] if c <= 10) / n,
            "top1_acc": acc["top1"] / n,
            "top5_acc": acc["top5"] / n,
            "mean_mrr": statistics.fmean(acc["rr"]),
        })
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
    p = argparse.ArgumentParser(prog="sim.analyze_a")
    p.add_argument("--out-dir", default="out")
    args = p.parse_args(argv)
    root = Path(args.out_dir)
    manifest = json.loads((root / "raw" / "manifest.json").read_text())
    rows = []
    for cell_meta in manifest["cells"]:
        h = cell_meta["hash"]
        cell_dir = root / "raw" / f"cell_{h}"
        trials_paths = sorted(cell_dir.glob("trials*.jsonl.gz"))
        if not trials_paths:
            print(f"  skip {h}: no chunks in {cell_dir}", file=sys.stderr)
            continue
        rows.extend(aggregate_cell(cell_meta, trials_paths))
    out_csv = root / "agg" / "lens_a.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_HEADER})
    print(f"[lens-a] wrote {len(rows)} rows -> {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
