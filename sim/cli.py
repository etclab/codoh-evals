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

from .analyze_c import (aggregate_cell as agg_cell_c,
                        analyze_user as analyze_user_c,
                        fit_p_obs)
from .cells import Cell, expand, parse_overrides
from .cover import (DEFAULT_COVER_PATH, DEFAULT_REFERENCE_PATH,
                    CoverUniverse)
from .lens_c import LensCParams, UserDayLog, UserResult, run_user
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
_PROGRESS_EVERY: int = 0
_LENS_C: dict = {}


def _worker_init(trace_path: str, cover_path: str, reference_path: str | None,
                 ntrials: int, base_seed: int, progress_every: int,
                 lens_c_cfg: dict | None = None) -> None:
    global _TRACE, _COVER, _REF, _NTRIALS, _BASE_SEED, _PROGRESS_EVERY, _LENS_C
    _TRACE = load_real(trace_path).filter_all_runs_intact()
    _COVER = CoverUniverse.from_file(cover_path)
    _REF = {s: hs for s, hs in _TRACE.Q_w.items()}
    _NTRIALS = ntrials
    _BASE_SEED = base_seed
    _PROGRESS_EVERY = progress_every
    _LENS_C = lens_c_cfg or {}


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

    # Estimate total trials in this chunk for progress reporting.
    n_total_est = 0
    for _bucket, _rank, site in victims_chunk:
        runs = {k[2] for k in _TRACE.pages if k[1] == site}
        n_total_est += len(runs) * _NTRIALS
    short = cell.hash[:6]
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
                    if (_PROGRESS_EVERY and
                            n_written % _PROGRESS_EVERY == 0):
                        elapsed = time.time() - t0
                        rate = n_written / max(elapsed, 1e-6)
                        eta = (n_total_est - n_written) / max(rate, 1e-6)
                        print(f"[hb] cell={short} chunk={chunk_id:04d} "
                              f"trial={n_written}/{n_total_est} "
                              f"elapsed={elapsed:.0f}s eta={eta:.0f}s",
                              flush=True)
    return {
        "cell_hash": cell.hash,
        "chunk_id": chunk_id,
        "n_trials": n_written,
        "elapsed_s": round(time.time() - t0, 2),
        "out": str(out_path),
    }


def _run_chunk_lens_c(args: tuple[Cell, str, int, list[int]]) -> dict:
    """Worker entry point for `--lens c`. One chunk = (cell, [user_idx,...]).

    Each user gets a per-user JSONL row containing per-day raw S' sets,
    repertoire, and underflow accounting. Aggregation (intersection,
    days-to-fingerprint, posterior) runs after all chunks complete in
    `_finalize_lens_c` so it can fit `p_obs` per cell from the cell's
    own data.
    """
    cell, out_dir, chunk_id, user_chunk = args
    assert _TRACE is not None and _COVER is not None
    cell_dir = Path(out_dir) / "raw" / f"cell_{cell.hash}"
    cell_dir.mkdir(parents=True, exist_ok=True)
    out_path = cell_dir / f"lens_c_{chunk_id:04d}.jsonl.gz"

    cfg = _LENS_C
    params = LensCParams(
        B=cell.B, T_max_s=cell.T_max_s, k=cell.k,
        lambda_bg=cell.lambda_bg, N=cell.N, alpha=cell.alpha,
        D=cell.D, init=cell.init,
        n_users=cfg.get("n_users", 30),
        max_days=cfg.get("max_days", 7),
        repertoire_size=cfg.get("repertoire_size", 10),
        repertoire_bucket_max_rank=cfg.get("repertoire_bucket_max_rank", 1000),
    )
    short = cell.hash[:6]
    n_total = len(user_chunk)
    n_written = 0
    n_failed = 0
    t0 = time.time()
    with gzip.open(out_path, "wt") as fh:
        for user_idx in user_chunk:
            try:
                user = run_user(
                    user_idx=user_idx, params=params, trace=_TRACE,
                    cover_universe=_COVER, base_seed=_BASE_SEED,
                    cell_tag=cell.hash,
                )
                row = {
                    "cell_hash": cell.hash,
                    "user_idx": user.user_idx,
                    "repertoire": sorted(user.repertoire),
                    "days": [
                        {"d": d.day_idx,
                         "S_prime": sorted(d.S_prime_total),
                         "S_prime_size": len(d.S_prime_total),
                         "n_commits": d.n_commits,
                         "n_victim_batches": d.n_victim_batches,
                         "underflow_count": d.underflow_count,
                         "odoh_fallback_count": d.odoh_fallback_count}
                        for d in user.days
                    ],
                }
            except Exception as e:
                # Per-user fault isolation: a single user failure must not
                # poison the rest of the chunk. Emit an error sentinel row
                # (the analyzer skips rows with `"error"` set) and keep
                # going. Print the traceback to stdout so the heartbeat log
                # captures it for postmortem.
                import traceback
                tb = traceback.format_exc()
                row = {
                    "cell_hash": cell.hash,
                    "user_idx": user_idx,
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": tb,
                }
                n_failed += 1
                print(f"[err] cell={short} chunk={chunk_id:04d} "
                      f"user={user_idx} {type(e).__name__}: {e}",
                      flush=True)
            fh.write(json.dumps(row) + "\n")
            fh.flush()  # survive a chunk-level kill: don't lose unflushed rows
            n_written += 1
            if (_PROGRESS_EVERY
                    and n_written % max(1, _PROGRESS_EVERY) == 0):
                elapsed = time.time() - t0
                rate = n_written / max(elapsed, 1e-6)
                eta = (n_total - n_written) / max(rate, 1e-6)
                print(f"[hb] cell={short} chunk={chunk_id:04d} "
                      f"user={n_written}/{n_total} "
                      f"elapsed={elapsed:.0f}s eta={eta:.0f}s "
                      f"failed={n_failed}",
                      flush=True)
    return {
        "cell_hash": cell.hash, "chunk_id": chunk_id,
        "n_users": n_written, "n_failed": n_failed,
        "elapsed_s": round(time.time() - t0, 2),
        "out": str(out_path),
    }


def _finalize_lens_c(cells: list[Cell], out_dir: Path,
                     posterior_threshold: float = 0.9) -> None:
    """Reduce step: glob raw lens_c JSONL per cell, fit `p_obs`, run the
    analyzer, and emit `out/agg/lens_c.csv` (one row per cell) plus
    `out/agg/lens_c_users.csv` (one row per user)."""
    # Re-load reference set once (same across cells; uses trace.Q_w).
    # The worker init already loaded the trace, but the parent process is
    # separate from the pool — pull from the manifest instead so we don't
    # depend on the worker globals here.
    cell_rows = []
    user_rows = []
    for cell in cells:
        cell_dir = out_dir / "raw" / f"cell_{cell.hash}"
        users: list[UserResult] = []
        n_failed_cell = 0
        for path in sorted(cell_dir.glob("lens_c_*.jsonl.gz")):
            with gzip.open(path, "rt") as fh:
                for line in fh:
                    row = json.loads(line)
                    if "error" in row:
                        n_failed_cell += 1
                        continue
                    users.append(UserResult(
                        user_idx=row["user_idx"],
                        repertoire=row["repertoire"],
                        days=[UserDayLog(
                            day_idx=d["d"],
                            S_prime_total=set(d["S_prime"]),
                            n_commits=d["n_commits"],
                            n_victim_batches=d["n_victim_batches"],
                            underflow_count=d["underflow_count"],
                            odoh_fallback_count=d["odoh_fallback_count"],
                        ) for d in row["days"]],
                    ))
        if n_failed_cell:
            print(f"[lens-c] cell={cell.hash[:6]} skipped "
                  f"{n_failed_cell} failed users")
        if not users:
            continue
        # Reference set: trace's Q_w. Re-load (cheap relative to a sweep).
        # Load only once across all cells (lazy).
        if not hasattr(_finalize_lens_c, "_ref_cache"):
            from .trace_loader import load_real
            trace_path = out_dir / "raw" / "manifest.json"
            mf = json.loads(trace_path.read_text())
            tr = load_real(mf["trace"]).filter_all_runs_intact()
            _finalize_lens_c._ref_cache = {s: hs for s, hs in tr.Q_w.items()}
        reference = _finalize_lens_c._ref_cache
        p_obs = fit_p_obs(users, reference)
        user_metrics = [
            analyze_user_c(u, reference_set=reference, alpha=cell.alpha,
                           p_obs=p_obs, R_size=10)
            for u in users
        ]
        cell_metrics = agg_cell_c(
            user_metrics, cell_tag=cell.hash, p_obs=p_obs,
            posterior_threshold=posterior_threshold,
        )
        cell_rows.append({
            "cell_hash": cell.hash, "label": cell.label,
            "B": cell.B, "T_max_s": cell.T_max_s, "k": cell.k,
            "lambda_bg": cell.lambda_bg, "N": cell.N, "alpha": cell.alpha,
            "D": cell.D, "init": cell.init,
            "n_users": cell_metrics.n_users,
            "n_failed": n_failed_cell,
            "days_to_fp_median": cell_metrics.days_to_fp_median,
            "days_to_fp_p10": cell_metrics.days_to_fp_p10,
            "days_to_fp_p90": cell_metrics.days_to_fp_p90,
            "n_censored": cell_metrics.n_censored,
            "posterior_fraction_users": cell_metrics.posterior_fraction_users,
            "repertoire_acc_median_final": cell_metrics.repertoire_acc_median_final,
            "underflow_day_rate_mean": cell_metrics.underflow_day_rate_mean,
            "p_obs_fitted": cell_metrics.p_obs_fitted,
        })
        for m in user_metrics:
            user_rows.append({
                "cell_hash": cell.hash,
                "user_idx": m.user_idx,
                "days_to_fp_alpha": m.days_to_fp_alpha,
                "days_to_fp_censored": m.days_to_fp_censored,
                "intersect_final": m.intersections[-1]["intersect_size"]
                                   if m.intersections else 0,
                "rep_acc_final": m.intersections[-1]["repertoire_acc"]
                                 if m.intersections else 0.0,
                "posterior_R_final": m.intersections[-1]["posterior_R"]
                                     if m.intersections else 0.0,
            })
    agg = out_dir / "agg"
    agg.mkdir(parents=True, exist_ok=True)
    if cell_rows:
        _write_csv(agg / "lens_c.csv", cell_rows)
    if user_rows:
        _write_csv(agg / "lens_c_users.csv", user_rows)
    print(f"[lens-c] wrote {len(cell_rows)} cells and "
          f"{len(user_rows)} users to {agg}/")


def _write_csv(path: Path, rows: list[dict]) -> None:
    import csv as _csv
    if not rows:
        return
    keys = list(rows[0].keys())
    with open(path, "w", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _build_work_items_lens_c(cells: list[Cell], n_users: int, workers: int,
                             out_dir: str, oversubscribe: int = 4) -> list[tuple]:
    """One chunk per (cell, user-shard).

    Heuristic mirrors `_build_work_items`: aim for `oversubscribe × workers`
    total chunks so the slowest cell's users split across multiple workers
    and wall time approaches `total_work / workers` rather than
    `max_chunk_time`. Worker init (trace + cover load) is amortized at pool
    startup, so finer chunking is essentially free.
    """
    target_chunks = max(workers, oversubscribe * workers)
    chunks_per_cell = max(1, target_chunks // max(1, len(cells)))
    chunks_per_cell = min(chunks_per_cell, n_users)  # no empty chunks
    chunk_size = max(1, (n_users + chunks_per_cell - 1) // chunks_per_cell)
    work = []
    chunk_id = 0
    for cell in cells:
        for i in range(0, n_users, chunk_size):
            shard = list(range(i, min(i + chunk_size, n_users)))
            work.append((cell, out_dir, chunk_id, shard))
            chunk_id += 1
    return work


def _build_work_items(
    cells: list[Cell], victims: dict[str, list[tuple[int, str]]],
    workers: int, out_dir: str, oversubscribe: int = 4,
) -> list[tuple]:
    """Flatten (cell × victim_shard) into chunks.

    Heuristic: aim for `oversubscribe × workers` total chunks across all
    cells, so the slowest cell's work is split across multiple workers and
    wall time approaches `total_work / workers` rather than
    `max_chunk_time`. Init cost (trace + cover load, ~30s) is paid once
    per worker at pool startup, not per chunk, so finer chunking is
    basically free.
    """
    flat: list[tuple[str, int, str]] = []
    for bucket, lst in victims.items():
        for rank, site in lst:
            flat.append((bucket, rank, site))
    target_chunks = max(workers, oversubscribe * workers)
    chunks_per_cell = max(1, target_chunks // max(1, len(cells)))
    # Cap shards-per-cell at the number of victims (no empty chunks).
    chunks_per_cell = min(chunks_per_cell, len(flat))
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
    pr.add_argument("--progress", type=int, default=25,
                    help="emit a [hb] heartbeat line every N trials per chunk "
                         "(0 disables; default 25)")
    pr.add_argument("--lens", choices=["b", "c"], default="b",
                    help="b (default) = lens-(a/b) per-victim trials; "
                         "c = lens-(c) long-term intersection (synthetic users)")
    pr.add_argument("--n-users", type=int, default=30,
                    help="(--lens c) synthetic users per cell (default 30, "
                         "matches lens-(a) §8.1 trial count)")
    pr.add_argument("--max-days", type=int, default=7,
                    help="(--lens c) synthetic days per user (default 7)")
    pr.add_argument("--repertoire-size", type=int, default=10,
                    help="(--lens c) pages per user (default 10, per §8.3)")
    pr.add_argument("--repertoire-bucket-max-rank", type=int, default=1000,
                    help="(--lens c) cap on repertoire rank — top-1k by "
                         "default (CrUX bands cap finer popularity)")
    pr.add_argument("--posterior-threshold", type=float, default=0.9,
                    help="(--lens c) posterior_R threshold for the "
                         "fraction-of-users robustness column (default 0.9)")
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
    sampling["ntrials"] = ntrials  # keep manifest + plan log in sync with CLI

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
    lens_c_cfg = {
        "n_users": args.n_users,
        "max_days": args.max_days,
        "repertoire_size": args.repertoire_size,
        "repertoire_bucket_max_rank": args.repertoire_bucket_max_rank,
    }
    init_args = (args.trace, args.cover, args.reference,
                 ntrials, args.seed, args.progress, lens_c_cfg)

    if args.lens == "c":
        work = _build_work_items_lens_c(
            cells, args.n_users, workers, str(out_dir),
        )
        chunk_fn = _run_chunk_lens_c
    else:
        # Build the work list using whatever sampling the worker would
        # compute, so the planner sees the same victims. Easiest: sample
        # here too.
        plan_rng = random.Random(args.seed)
        plan_trace = load_real(args.trace).filter_all_runs_intact()
        plan_victims = plan_trace.magnitude_band_sample(
            plan_rng,
            top1k_all=sampling["top1k_all"],
            mid_n=sampling["mid_n"], tail_n=sampling["tail_n"],
        )
        if not sampling["top1k_all"]:
            n = sampling["top1k_n"]
            if len(plan_victims["top-1k"]) > n:
                plan_victims["top-1k"] = plan_rng.sample(
                    plan_victims["top-1k"], n,
                )
        del plan_trace, plan_rng
        work = _build_work_items(cells, plan_victims, workers, str(out_dir))
        chunk_fn = _run_chunk

    workers = min(workers, len(work))
    print(f"[start] lens={args.lens} workers={workers} cells={len(cells)} "
          f"chunks={len(work)} -> {out_dir}/raw")

    t0 = time.time()
    if workers == 1:
        _worker_init(*init_args)
        results = [chunk_fn(w) for w in work]
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(workers, initializer=_worker_init,
                      initargs=init_args) as pool:
            results = []
            total = len(work)
            for r in pool.imap_unordered(chunk_fn, work):
                results.append(r)
                tag = ("trials" if "n_trials" in r else "users")
                count = r.get("n_trials", r.get("n_users"))
                print(f"  [{len(results):4d}/{total}] done "
                      f"cell={r['cell_hash']} chunk={r['chunk_id']:04d} "
                      f"{tag}={count} t={r['elapsed_s']}s",
                      flush=True)
    print(f"[done] {len(results)} chunks in {round(time.time() - t0, 1)}s")
    (out_dir / "raw" / "results.json").write_text(
        json.dumps(results, indent=2)
    )

    if args.lens == "c":
        _finalize_lens_c(cells, out_dir,
                         posterior_threshold=args.posterior_threshold)
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
