"""(B, T_max) heatmap from a sweep's lens-b aggregation.

Headline metric: lens-(b) top-5 accuracy averaged across victim buckets.
Expected smoke gradient: high B / short T_max is less leaky than low B /
long T_max (longer T_max → larger cross-batch union → tighter candidate
set).

Smoke-run acceptance check: top-1 accuracy varies by > 0.1 across the
3×3 corners. Prints `OK gradient=...` / `FAIL` to stdout.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path


def load(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            r["B"] = int(r["B"])
            r["T_max_s"] = float(r["T_max_s"])
            r["top1_acc"] = float(r["top1_acc"]) if r["top1_acc"] else 0.0
            r["top5_acc"] = float(r["top5_acc"]) if r["top5_acc"] else 0.0
            r["mean_cand_size"] = float(r["mean_cand_size"]) \
                if r["mean_cand_size"] else 0.0
            rows.append(r)
    return rows


def grid(rows: list[dict], metric: str) -> tuple[list[int], list[float], list[list[float]]]:
    Bs = sorted({r["B"] for r in rows})
    Ts = sorted({r["T_max_s"] for r in rows})
    by_BT = {(r["B"], r["T_max_s"]): r[metric] for r in rows}
    M = [[by_BT.get((b, t), float("nan")) for t in Ts] for b in Bs]
    return Bs, Ts, M


def render_text(rows: list[dict], metric: str) -> str:
    Bs, Ts, M = grid(rows, metric)
    out = [f"\n=== {metric} ==="]
    out.append("B \\ T_max  " + "  ".join(f"{t:>8.0f}" for t in Ts))
    for b, row in zip(Bs, M):
        out.append(f"B={b:<6d}  " + "  ".join(f"{v:>8.3f}" for v in row))
    return "\n".join(out)


def render_png(rows: list[dict], metric: str, out_path: Path) -> None:
    try:
        import matplotlib.pyplot as plt  # noqa
    except ImportError:
        print("matplotlib not installed; skipping PNG", file=sys.stderr)
        return
    import numpy as np
    Bs, Ts, M = grid(rows, metric)
    A = np.array(M)
    fig, ax = plt.subplots(figsize=(5, 4))
    im = ax.imshow(A, aspect="auto", origin="lower", cmap="viridis",
                   vmin=0.0, vmax=max(1.0, A.max()))
    ax.set_xticks(range(len(Ts)))
    ax.set_xticklabels([f"{t:.0f}" for t in Ts])
    ax.set_yticks(range(len(Bs)))
    ax.set_yticklabels([str(b) for b in Bs])
    ax.set_xlabel("T_max (s)")
    ax.set_ylabel("B")
    ax.set_title(f"lens-(b) {metric}")
    for i in range(A.shape[0]):
        for j in range(A.shape[1]):
            ax.text(j, i, f"{A[i,j]:.2f}", ha="center", va="center",
                    color="white" if A[i, j] < 0.5 else "black", fontsize=8)
    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"[plot] wrote {out_path}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sim.plot_heatmap")
    p.add_argument("--out-dir", default="out")
    p.add_argument("--metric", default="top5_acc",
                   choices=["top1_acc", "top5_acc", "mean_cand_size"])
    p.add_argument("--gradient-min", type=float, default=0.1,
                   help="acceptance gate: max-min top1_acc must exceed this")
    args = p.parse_args(argv)
    root = Path(args.out_dir)
    rows = load(root / "agg" / "lens_b.csv")
    print(render_text(rows, args.metric))
    if args.metric != "top1_acc":
        print(render_text(rows, "top1_acc"))
    render_png(rows, args.metric, root / "agg" / f"heatmap_{args.metric}.png")

    top1s = [float(r["top1_acc"]) for r in rows if r.get("top1_acc")]
    if top1s:
        gradient = max(top1s) - min(top1s)
        ok = gradient > args.gradient_min
        print(f"\n[gate] top1 gradient = {gradient:.3f} "
              f"({'OK' if ok else 'FAIL'}; threshold {args.gradient_min})")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
