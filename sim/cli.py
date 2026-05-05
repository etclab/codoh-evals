"""Sweep driver: parallelize cells, sequential trials within a cell.

Usage:
    python -m sim.cli run --cells smoke
    python -m sim.cli run --cells default --out-dir out/
    python -m sim.cli run --cells smoke --set B=5,10 T_max_s=60,300
    python -m sim.cli run --cells smoke --trace data/foo.csv --ntrials 30 \
        --top1k 50 --mid 50 --tail 50

Worker model:
    - one process per cell (Pool size = min(cpu_count(), 16))
    - each worker loads trace + cover universe ONCE, then runs all trials
    - per-trial JSONL emitted to out/raw/cell_<hash>/trials.jsonl.gz
    - cell metadata + sweep manifest emitted to out/raw/manifest.json

Smoke defaults are sized so the full grid runs in a few minutes on a laptop.
The default and full presets are meant for the cluster.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import multiprocessing as mp
import os
import random
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .cells import Cell, expand, parse_overrides
from .cover import (DEFAULT_COVER_PATH, DEFAULT_REFERENCE_PATH,
                    CoverUniverse)
from .trace_loader import Trace, load_real
from .trial import TrialLog, run_trial


REPO = Path(__file__).resolve().parents[1]
DEFAULT_TRACE = (REPO / "runs" / "crux-combined-r1-r3"
                 / "combined_entries_har_vanilla.csv")


# =====================================================================
# Smoke / default sizing knobs (overridable via CLI flags)
# =====================================================================

SMOKE_SAMPLING = {"top1k_all": False, "top1k_n": 50, "mid_n": 50, "tail_n": 50,
                  "ntrials": 10}
DEFAULT_SAMPLING = {"top1k_all": True, "top1k_n": 994, "mid_n": 200,
                    "tail_n": 200, "ntrials": 30}


# =====================================================================
# Per-worker globals (initialized in `_worker_init`)
# =====================================================================

_TRACE: Trace | None = None
_COVER: CoverUniverse | None = None
_REF: dict[str, set[str]] | None = None
_NTRIALS: int = 0
_BASE_SEED: int = 0


def _worker_init(trace_path: str, cover_path: str, reference_path: str | None,
                 ntrials: int, base_seed: int) -> None:
    global _TRACE, _COVER, _REF, _NTRIALS, _BASE_SEED
    _TRACE = load_real(trace_path).filter_all_runs_intact()
    _COVER = CoverUniverse.from_file(cover_path)
    _REF = {s: hs for s, hs in _TRACE.Q_w.items()}
    _NTRIALS = ntrials
    _BASE_SEED = base_seed


def _run_chunk(args: tuple[Cell, str, int, list[tuple[str, int, str]]]) -> dict:
    """Worker entry point. Runs all trials for one (cell, victim_chunk).

    `victims_chunk` is a list of (bucket, rank, site) tuples — the slice of
    the global victim sample assigned to this chunk. The worker writes
    `cell_<hash>/trials_<chunk_id>.jsonl.gz`; analyzers glob them.
    """
    cell, out_dir, chunk_id, victims_chunk = args
    assert _TRACE is not None and _COVER is not None
    params = cell.to_trial_params()
    cell_dir = Path(out_dir) / "raw" / f"cell_{cell.hash}"
    cell_dir.mkdir(parents=True, exist_ok=True)
    out_path = cell_dir / f"trials_{chunk_id:04d}.jsonl.gz"

    n_written = 0
    t0 = time.time()
    day = next(iter(_TRACE.days))
    with gzip.open(out_path, "wt") as fh:
        for bucket, rank, site in victims_chunk:
            runs = sorted({k[2] for k in _TRACE.pages if k[1] == site})
            if not runs:
                continue
            for run in runs:
                victim_key = (rank, site, run, day)
                if victim_key not in _TRACE.pages:
                    continue
                for ti in range(_NTRIALS):
                    seed = _seed_for(cell, victim_key, ti)
                    log = run_trial(
                        params, victim_key=victim_key,
                        trace=_TRACE, cover_universe=_COVER, seed=seed,
                    )
                    row = _summarize(log, bucket, ti, _REF)
                    fh.write(json.dumps(row) + "\n")
                    n_written += 1
    return {
        "cell_hash": cell.hash,
        "chunk_id": chunk_id,
        "n_trials": n_written,
        "elapsed_s": round(time.time() - t0, 2),
        "out": str(out_path),
    }


def _build_work_items(
    cells: list[Cell], victims: dict[str, list[tuple[int, str]]],
    workers: int, out_dir: str,
) -> list[tuple]:
    """Flatten (cell, victim) into chunks. Aim for ~workers items per cell
    when cells < workers; one item per cell otherwise."""
    flat: list[tuple[str, int, str]] = []
    for bucket, lst in victims.items():
        for rank, site in lst:
            flat.append((bucket, rank, site))
    chunks_per_cell = max(1, workers // max(1, len(cells)))
    chunk_size = max(1, (len(flat) + chunks_per_cell - 1) // chunks_per_cell)
    work = []
    chunk_id = 0
    for cell in cells:
        for i in range(0, len(flat), chunk_size):
            shard = flat[i:i + chunk_size]
            work.append((cell, out_dir, chunk_id, shard))
            chunk_id += 1
    return work


# =====================================================================
# Helpers
# =====================================================================

def _seed_for(cell: Cell, victim_key, trial_idx: int) -> int:
    """Stable per-trial seed: blake2b(cell, victim, trial). Use hashlib not
    `hash()` — Python's builtin hash is randomized per-process unless
    PYTHONHASHSEED is set, which would silently break figure regeneration
    across re-runs.
    """
    import hashlib
    payload = f"{cell.hash}|{victim_key[1]}|{victim_key[2]}|{trial_idx}".encode()
    return int.from_bytes(hashlib.blake2b(payload, digest_size=4).digest(),
                          "big") & 0x7FFFFFFF


def _summarize(log: TrialLog, bucket: str, trial_idx: int,
               reference_set: dict[str, set[str]]) -> dict:
    """Reduce a TrialLog to a single JSONL row (lens-a + lens-b results)."""
    lens_a = [_attack_dict(r) for r in log.lens_a(reference_set)]
    lens_b = _attack_dict(log.lens_b(reference_set))
    return {
        "cell_hash": _cell_hash_from_params(log.params),
        "victim_site": log.victim_site,
        "victim_rank": log.victim_rank,
        "bucket": bucket,
        "trial_idx": trial_idx,
        "n_commits": len(log.commits),
        "n_victim_batches": log.n_victim_batches,
        "underflow_count": log.underflow_count,
        "S_prime_total_size": len(log.S_prime_total),
        "Q_v_size": len(log.victim_Q_v),
        "lens_a": lens_a,
        "lens_b": lens_b,
    }


def _attack_dict(r) -> dict:
    return {
        "cand_size": r.candidate_size,
        "top1": r.top1_hit,
        "top5": r.top5_hit,
        "rr": r.rr,
        "victim_score": r.victim_score,
        "g": r.n_strictly_greater,
        "t": r.n_tied_with_victim,
    }


def _cell_hash_from_params(p) -> str:
    c = Cell(B=p.B, T_max_s=p.T_max_s, k=p.k, lambda_bg=p.lambda_bg,
             N=p.N, alpha=p.alpha, D=p.D, init=p.init)
    return c.hash


# =====================================================================
# Entry point
# =====================================================================

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sim.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="run a sweep")
    pr.add_argument("--cells", default="smoke",
                    help="preset name (smoke|default|full)")
    pr.add_argument("--set", dest="overrides", nargs="*", default=[],
                    help="axis overrides, e.g. --set B=5,10 T_max_s=30,300")
    pr.add_argument("--out-dir", default="out",
                    help="output root (raw + agg subdirs created)")
    pr.add_argument("--trace", default=str(DEFAULT_TRACE),
                    help="per-entry trace CSV")
    pr.add_argument("--cover", default=str(DEFAULT_COVER_PATH),
                    help="cover universe CSV (CrUX 1M)")
    pr.add_argument("--reference", default=None,
                    help="reference set CSV (default: trace.Q_w)")
    pr.add_argument("--ntrials", type=int, default=None,
                    help="trials per victim (default: preset-dependent)")
    pr.add_argument("--top1k", type=int, default=None,
                    help="top-1k bucket sample size (default: all if preset is default/full, 50 for smoke)")
    pr.add_argument("--mid", type=int, default=None,
                    help="1k-5k bucket sample size")
    pr.add_argument("--tail", type=int, default=None,
                    help="5k-10k bucket sample size")
    pr.add_argument("--seed", type=int, default=20260505,
                    help="base seed for victim sampling + trial seeding")
    pr.add_argument("--workers", type=int, default=None,
                    help="pool size (default: min(cpu_count(), 16))")
    pr.add_argument("--dry-run", action="store_true",
                    help="print plan, don't run")

    args = p.parse_args(argv)
    if args.cmd != "run":
        p.error(f"unknown command: {args.cmd}")
        return 2

    overrides = parse_overrides(args.overrides) if args.overrides else {}
    axes_dict = dict(expand_axes(args.cells))
    axes_dict.update(overrides)
    cells = expand(axes_dict)

    sampling = SMOKE_SAMPLING.copy() if args.cells == "smoke" \
        else DEFAULT_SAMPLING.copy()
    if args.top1k is not None:
        sampling["top1k_n"] = args.top1k
        sampling["top1k_all"] = False
    if args.mid is not None:
        sampling["mid_n"] = args.mid
    if args.tail is not None:
        sampling["tail_n"] = args.tail
    ntrials = args.ntrials if args.ntrials is not None else sampling["ntrials"]

    out_dir = Path(args.out_dir)
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    (out_dir / "agg").mkdir(parents=True, exist_ok=True)

    manifest = {
        "preset": args.cells,
        "overrides": overrides,
        "n_cells": len(cells),
        "sampling": sampling,
        "ntrials": ntrials,
        "trace": args.trace,
        "cover": args.cover,
        "reference": args.reference,
        "seed": args.seed,
        "cells": [
            {"hash": c.hash, "label": c.label, **c.to_dict()} for c in cells
        ],
    }
    (out_dir / "raw" / "manifest.json").write_text(
        json.dumps(manifest, indent=2)
    )
    print(f"[plan] preset={args.cells} cells={len(cells)} ntrials={ntrials} "
          f"sampling={sampling}")
    if args.dry_run:
        for c in cells:
            print(f"  {c.hash}  {c.label}")
        return 0

    workers = args.workers or mp.cpu_count()
    init_args = (args.trace, args.cover, args.reference,
                 ntrials, args.seed)

    # Build the work list using whatever sampling the worker would compute,
    # so the planner sees the same victims. Easiest: sample here too.
    plan_rng = random.Random(args.seed)
    # We need a Trace just to enumerate victims for chunking. Worst case we
    # load the trace twice (once here, once per worker), but it's < 5s.
    plan_trace = load_real(args.trace).filter_all_runs_intact()
    plan_victims = plan_trace.magnitude_band_sample(
        plan_rng,
        top1k_all=sampling["top1k_all"],
        mid_n=sampling["mid_n"], tail_n=sampling["tail_n"],
    )
    if not sampling["top1k_all"]:
        n = sampling["top1k_n"]
        if len(plan_victims["top-1k"]) > n:
            plan_victims["top-1k"] = plan_rng.sample(plan_victims["top-1k"], n)
    del plan_trace, plan_rng

    work = _build_work_items(cells, plan_victims, workers, str(out_dir))
    workers = min(workers, len(work))
    print(f"[start] workers={workers} cells={len(cells)} chunks={len(work)} "
          f"-> {out_dir}/raw")

    t0 = time.time()
    if workers == 1:
        _worker_init(*init_args)
        results = [_run_chunk(w) for w in work]
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(workers, initializer=_worker_init,
                      initargs=init_args) as pool:
            results = []
            for r in pool.imap_unordered(_run_chunk, work):
                results.append(r)
                print(f"  done cell={r['cell_hash']} chunk={r['chunk_id']} "
                      f"trials={r['n_trials']} t={r['elapsed_s']}s")
    print(f"[done] {len(results)} chunks in {round(time.time() - t0, 1)}s")
    (out_dir / "raw" / "results.json").write_text(
        json.dumps(results, indent=2)
    )
    return 0


def expand_axes(name: str) -> dict:
    """Return the axes dict for a preset (so we can merge overrides into it)."""
    from .cells import PRESETS
    if name not in PRESETS:
        raise SystemExit(f"unknown preset: {name!r}; "
                         f"choose from {sorted(PRESETS)}")
    return PRESETS[name]


if __name__ == "__main__":
    sys.exit(main())
