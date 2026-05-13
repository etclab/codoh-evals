"""Plot cover-distribution sensitivity.

Reads `out_dsens_lensb/agg/{lens_a,lens_b}.csv` and
`out_dsens_lensc/agg/lens_c_users.csv`; emits a 3-panel figure showing
attacker accuracy / candidate-set size vs D ∈ {matched, uniform, stale}
at the Moderate operating point.

Usage:
    python -m sim.plot_dsens \\
        --lensb out_dsens_lensb/agg \\
        --lensc out_dsens_lensc/agg \\
        --out figs/dsens.pdf
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt

D_ORDER = ["matched", "uniform", "stale"]
D_COLORS = {"matched": "#2b8cbe", "uniform": "#fdae61", "stale": "#d7301f"}


def _read_csv(path: Path) -> list[dict]:
    with open(path) as f:
        return list(csv.DictReader(f))


def _by_D(rows: list[dict], key: str) -> dict[str, float]:
    """Return {D: float(row[key])} for the cells that match by D."""
    out: dict[str, float] = {}
    for r in rows:
        out[r["D"]] = float(r[key])
    return out


def _lens_a_by_bucket(rows: list[dict]) -> dict[str, dict[str, float]]:
    """{bucket: {D: top5_acc}}."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        out[r["bucket"]][r["D"]] = float(r["top5_acc"])
    return out


def _lens_c_intersect(users_rows: list[dict],
                      cells_rows: list[dict]) -> dict[str, list[int]]:
    """{D: [intersect_final per user]} keyed by joining cell_hash → D."""
    hash_to_D = {r["cell_hash"]: r["D"] for r in cells_rows}
    out: dict[str, list[int]] = defaultdict(list)
    for r in users_rows:
        D = hash_to_D.get(r["cell_hash"])
        if D is None:
            continue
        out[D].append(int(r["intersect_final"]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sim.plot_dsens")
    ap.add_argument("--lensb", required=True,
                    help="agg dir from --lens b run (lens_a.csv + lens_b.csv)")
    ap.add_argument("--lensc", required=True,
                    help="agg dir from --lens c run (lens_c.csv + lens_c_users.csv)")
    ap.add_argument("--out", required=True, help="output figure path")
    args = ap.parse_args(argv)

    lensb_dir = Path(args.lensb)
    lensc_dir = Path(args.lensc)

    lens_a = _read_csv(lensb_dir / "lens_a.csv")
    lens_b = _read_csv(lensb_dir / "lens_b.csv")
    lens_c_users = _read_csv(lensc_dir / "lens_c_users.csv")
    lens_c_cells = _read_csv(lensc_dir / "lens_c.csv")

    a_by_bucket = _lens_a_by_bucket(lens_a)
    b_top5 = _by_D(lens_b, "top5_acc")
    c_intersect = _lens_c_intersect(lens_c_users, lens_c_cells)

    fig, axes = plt.subplots(1, 3, figsize=(13, 4))

    # Panel 1: lens-a top-5 acc by bucket × D (grouped bars).
    ax = axes[0]
    buckets = sorted(a_by_bucket.keys())
    x = list(range(len(buckets)))
    width = 0.25
    for i, D in enumerate(D_ORDER):
        vals = [a_by_bucket[b].get(D, 0.0) for b in buckets]
        ax.bar([xi + (i - 1) * width for xi in x], vals, width,
               label=D, color=D_COLORS[D], edgecolor="black", linewidth=0.4)
    ax.set_xticks(x)
    ax.set_xticklabels(buckets, rotation=30, ha="right")
    ax.set_ylabel("Lens-(a) top-5 accuracy")
    ax.set_title("(a) Per-batch identification")
    ax.legend(frameon=False, fontsize=9)
    ax.grid(axis="y", alpha=0.3)

    # Panel 2: lens-b top-5 acc per D.
    ax = axes[1]
    vals = [b_top5.get(D, 0.0) for D in D_ORDER]
    ax.bar(D_ORDER, vals, color=[D_COLORS[D] for D in D_ORDER],
           edgecolor="black", linewidth=0.4)
    ax.set_ylabel("Lens-(b) top-5 accuracy")
    ax.set_title("(b) Page-load union")
    for i, v in enumerate(vals):
        ax.text(i, v, f"{v:.3f}", ha="center", va="bottom", fontsize=9)
    ax.grid(axis="y", alpha=0.3)

    # Panel 3: lens-c |C_7| boxplot per D.
    ax = axes[2]
    data = [c_intersect.get(D, []) for D in D_ORDER]
    bp = ax.boxplot(data, labels=D_ORDER, patch_artist=True,
                    showfliers=True)
    for patch, D in zip(bp["boxes"], D_ORDER):
        patch.set_facecolor(D_COLORS[D])
        patch.set_alpha(0.7)
    ax.set_ylabel("Lens-(c) |C_7| (day-7 candidate set)")
    ax.set_title("(c) Long-term intersection (7 d)")
    ax.set_yscale("log")
    ax.grid(axis="y", alpha=0.3, which="both")

    fig.suptitle(
        "Cover-distribution sensitivity at Moderate op point "
        "(B=100, T_max=300s, k=3, λ_bg=100, N=1024)",
        fontsize=11,
    )
    fig.tight_layout()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    print(f"[plot] wrote {out_path}")

    # Also dump the underlying numbers for the §5.2 prose.
    summary_path = out_path.with_suffix(".txt")
    with open(summary_path, "w") as f:
        f.write("D-sensitivity at Moderate op point\n")
        f.write("=" * 40 + "\n\n")
        f.write("Lens-(a) top-5 accuracy by bucket × D:\n")
        for bkt in buckets:
            f.write(f"  {bkt}:\n")
            for D in D_ORDER:
                f.write(f"    {D:8s}  {a_by_bucket[bkt].get(D, 0.0):.4f}\n")
        f.write("\nLens-(b) top-5 accuracy:\n")
        for D in D_ORDER:
            f.write(f"  {D:8s}  {b_top5.get(D, 0.0):.4f}\n")
        f.write("\nLens-(c) |C_7| (median, p10, p90):\n")
        for D in D_ORDER:
            xs = sorted(c_intersect.get(D, []))
            if not xs:
                continue
            n = len(xs)
            med = xs[n // 2]
            p10 = xs[max(0, int(0.1 * n))]
            p90 = xs[min(n - 1, int(0.9 * n))]
            f.write(f"  {D:8s}  median={med}  p10={p10}  p90={p90}  "
                    f"(n={n})\n")
    print(f"[plot] wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
