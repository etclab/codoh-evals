"""Cross-tier D-sensitivity figure.

Reads the three per-tier sweeps in `results/dsens_B{20,50,100}/` and emits
a 3-panel figure showing how matched (D≈Q) vs uniform/stale degrade as a
function of B.

Usage:
    python -m sim.plot_dsens_tiers --out figs/dsens_tiers.pdf
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import NullFormatter, ScalarFormatter

D_ORDER = ["matched", "uniform", "stale"]
D_COLORS = {"matched": "#2b8cbe", "uniform": "#fdae61", "stale": "#d7301f"}
D_MARKERS = {"matched": "o", "uniform": "s", "stale": "^"}
B_VALUES = [20, 50, 100]


def _read_csv(path: Path) -> list[dict]:
    with open(path) as f:
        return list(csv.DictReader(f))


def _percentile(xs: list[int], p: float) -> float:
    if not xs:
        return float("nan")
    return float(np.percentile(xs, 100 * p))


def _gather(results_dir: Path) -> dict:
    data = {"a_top1k": defaultdict(dict), "b_top5": defaultdict(dict),
            "c_intersect": defaultdict(dict)}
    for B in B_VALUES:
        d = results_dir / f"dsens_B{B}"
        a = _read_csv(d / "lens_a.csv")
        b = _read_csv(d / "lens_b.csv")
        c_users = _read_csv(d / "lens_c_users.csv")
        c_cells = _read_csv(d / "lens_c.csv")
        hash_to_D = {r["cell_hash"]: r["D"] for r in c_cells}
        for r in a:
            if r["bucket"] != "top-1k":
                continue
            data["a_top1k"][r["D"]][B] = float(r["top5_acc"])
        for r in b:
            data["b_top5"][r["D"]][B] = float(r["top5_acc"])
        c_by_D: dict[str, list[int]] = defaultdict(list)
        for r in c_users:
            D = hash_to_D.get(r["cell_hash"])
            if D is None:
                continue
            c_by_D[D].append(int(r["intersect_final"]))
        for D in D_ORDER:
            data["c_intersect"][D][B] = c_by_D.get(D, [])
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sim.plot_dsens_tiers")
    ap.add_argument("--results", default="results",
                    help="root containing dsens_B{20,50,100}/")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    data = _gather(Path(args.results))

    fig, axes = plt.subplots(1, 3, figsize=(13, 4))

    def _plain_log_x(ax):
        ax.set_xticks(B_VALUES)
        ax.xaxis.set_major_formatter(ScalarFormatter())
        ax.xaxis.set_minor_formatter(NullFormatter())

    # Panel 1: lens-a top-5 (top-1k bucket) vs B, one line per D.
    ax = axes[0]
    for D in D_ORDER:
        ys = [data["a_top1k"][D].get(B, float("nan")) for B in B_VALUES]
        ax.plot(B_VALUES, ys, marker=D_MARKERS[D], color=D_COLORS[D],
                label=D, linewidth=2, markersize=9)
    ax.set_xlabel("B (commit threshold)")
    ax.set_ylabel("Top-5 accuracy, top-1k victims")
    ax.set_title("(a) Single batch")
    ax.set_xscale("log")
    _plain_log_x(ax)
    ax.legend(frameon=False, fontsize=10)
    ax.grid(True, alpha=0.3, which="both")

    # Panel 2: lens-b top-5 vs B.
    ax = axes[1]
    for D in D_ORDER:
        ys = [data["b_top5"][D].get(B, float("nan")) for B in B_VALUES]
        ax.plot(B_VALUES, ys, marker=D_MARKERS[D], color=D_COLORS[D],
                label=D, linewidth=2, markersize=9)
    ax.set_xlabel("B (commit threshold)")
    ax.set_ylabel("Top-5 accuracy")
    ax.set_title("(b) Within a page load")
    ax.set_xscale("log")
    _plain_log_x(ax)
    ax.legend(frameon=False, fontsize=10)
    ax.grid(True, alpha=0.3, which="both")

    # Panel 3: lens-c median |C_7| vs B with p10/p90 band.
    ax = axes[2]
    for D in D_ORDER:
        meds, lo, hi = [], [], []
        for B in B_VALUES:
            xs = data["c_intersect"][D].get(B, [])
            meds.append(_percentile(xs, 0.5))
            lo.append(_percentile(xs, 0.1))
            hi.append(_percentile(xs, 0.9))
        ax.plot(B_VALUES, meds, marker=D_MARKERS[D], color=D_COLORS[D],
                label=D, linewidth=2, markersize=9)
        ax.fill_between(B_VALUES, lo, hi, color=D_COLORS[D], alpha=0.15)
    ax.set_xlabel("B (commit threshold)")
    ax.set_ylabel("|C_7| (median, p10–p90 band)")
    ax.set_title("(c) Returning user, day-7 intersection")
    ax.set_xscale("log")
    _plain_log_x(ax)
    ax.set_yscale("log")
    ax.legend(frameon=False, fontsize=10)
    ax.grid(True, alpha=0.3, which="both")

    fig.suptitle(
        "Cover-distribution sensitivity vs commit threshold B "
        "(T_max=300s, k=3, λ_bg=100, N=1024)",
        fontsize=11,
    )
    fig.tight_layout()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    print(f"[plot] wrote {out_path}")

    # Numeric dump.
    txt = out_path.with_suffix(".txt")
    with open(txt, "w") as f:
        f.write("Cross-tier D-sensitivity\n" + "=" * 40 + "\n\n")
        f.write("Lens-(a) top-5 acc, top-1k bucket\n")
        f.write(f"  B    " + "  ".join(f"{D:>9s}" for D in D_ORDER) + "\n")
        for B in B_VALUES:
            row = [f"{data['a_top1k'][D].get(B, 0):.4f}" for D in D_ORDER]
            f.write(f"  {B:<4d} " + "  ".join(f"{v:>9s}" for v in row) + "\n")
        f.write("\nLens-(b) top-5 acc\n")
        f.write(f"  B    " + "  ".join(f"{D:>9s}" for D in D_ORDER) + "\n")
        for B in B_VALUES:
            row = [f"{data['b_top5'][D].get(B, 0):.4f}" for D in D_ORDER]
            f.write(f"  {B:<4d} " + "  ".join(f"{v:>9s}" for v in row) + "\n")
        f.write("\nLens-(c) median |C_7| (p10 / p90)\n")
        f.write(f"  B    " + "  ".join(f"{D:>14s}" for D in D_ORDER) + "\n")
        for B in B_VALUES:
            cells = []
            for D in D_ORDER:
                xs = data["c_intersect"][D].get(B, [])
                med = int(_percentile(xs, 0.5))
                p10 = int(_percentile(xs, 0.1))
                p90 = int(_percentile(xs, 0.9))
                cells.append(f"{med}({p10}-{p90})")
            f.write(f"  {B:<4d} " + "  ".join(f"{c:>14s}" for c in cells) + "\n")
    print(f"[plot] wrote {txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
