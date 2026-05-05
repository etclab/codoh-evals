# codoh-evals

DNS page-load benchmarks for the CoDoH PETS'26 paper, plus the leakage simulator at `sim/`.

## Prerequisites
- Python ≥ 3.10 (`sim/` and `validate-trace.py` are stdlib-only; the page-load benchmarks need a venv).
- One-time benchmark setup: `./setup-benchmark.sh` (creates the venv, installs Playwright + headless Chromium).
- Sibling repos under `../`:
    - [`coredns`](https://github.com/etclab/coredns) — run `make` once before first use.
    - [`dnscrypt-proxy`](https://github.com/etclab/dnscrypt-proxy/) — for DoH/ODoH benchmarks.
- TLS certs for the local DoH/ODoH endpoints: `./cert-maker.sh`.

## Layout
- `data/` — input domain CSVs:
    - `crux-top10k-resolvable.csv` — Chrome User Experience Report top-10k origins (3 magnitude bands: top-1k, top-5k, top-10k), filtered to those that resolve. Each row is `rank,hostname` where `rank` is the pre-filter CrUX position (1..9968, with gaps where the hostname didn't resolve). Stratify by `rank ≤ 1000` (top-1k bucket), `1000 < rank ≤ 5000` (1k–5k), `5000 < rank ≤ 10000` (5k–10k). 9,869 rows; **primary input for the leakage simulator.**
    - `umbrella-top-10k-resolvable.csv` — Cisco Umbrella top-10k filtered to resolvable. Default input for the legacy DoH/ODoH page-load benchmarks; not used by the simulator.
- `sim/` — leakage simulator package (per-module overview below).
- `plots/` — plotting scripts (`prepare_plot_data.py`, `*.gnuplot`) and the legacy `.dat`/`.pdf`/`results_har_*.csv` snapshots.
- `runs/` — timestamped per-strategy benchmark output:
    - `results_har_<strategy>.csv` — aggregated per (site, run): page-load and DNS summaries (drives CDF plots).
    - `entries_har_<strategy>.csv` — per-entry DNS trace `(rank, site, run, day, hostname, started_offset_ms, dns_ms)`, one row per resolved lookup in HAR emission order; consumed by the leakage simulator.

## Pipeline
The leakage analysis runs in three stages:

1. **Crawl** — `RUNS=3 ./run-vanilla-parallel.sh` (needs venv) → `runs/vanilla-parallel-<ts>/combined_entries_har_vanilla.csv`.
2. **Validate** — `python3 validate-trace.py` (stdlib). The script's input path is currently hardcoded to `runs/crux-combined-r1-r3/`; edit it to point at a fresh batch.
3. **Simulate** — `python3 -m sim.tests.test_<module>` (stdlib) for sanity passes; the parameter-sweep CLI is not yet implemented.

## Leakage simulator

The simulator consumes the per-entry trace produced by `benchmark-har.py vanilla` —
`entries_har_vanilla.csv`, schema `(rank, site, run, day, hostname, started_offset_ms, dns_ms)`,
one row per resolved DNS lookup in HAR emission order. Vanilla is sufficient because `Q_w`
and per-entry timings are properties of the page, not the resolver.

### One-shot full crawl
```sh
./setup-benchmark.sh                       # one-time: venv + Playwright + Chromium
RUNS=3 ./run-vanilla-parallel.sh           # ~30 min on a 56-CPU box at --jobs=40
```
Output: `runs/vanilla-parallel-<ts>/combined_entries_har_vanilla.csv` — the
simulator's input.

### Tunables
- `RUNS=N` — page-load runs per site (default 3).
- `--chunks N` — split `SOURCE_CSV` into N parallel chunks (default 20).
- `--jobs N` — cap concurrent workers (default: matches `--chunks`).
- `SOURCE_CSV=path` — override the input CSV (default `data/crux-top10k-resolvable.csv`).
- `SITE_FILES="a.csv b.csv ..."` — explicit chunk list (skips on-the-fly split).

### Checkpointed / top-up collection
Each invocation numbers runs starting at 1, so concatenating two batches naively
collides on `(site, run, day)`. Use `RUN_OFFSET` to shift the second batch's
`run` IDs:
```sh
RUNS=2 ./run-vanilla-parallel.sh                 # batch A: emits run = 1, 2
RUNS=1 RUN_OFFSET=2 ./run-vanilla-parallel.sh    # batch B: emits run = 3
```
Concatenating both `combined_entries_har_vanilla.csv` files gives a dataset
operationally equivalent to a single `RUNS=3` run, with no key collisions.
Caveat: if the two invocations straddle UTC midnight, the `day` column will
differ — the simulator treats those as separate days (lens c semantics).

### Sanity-check the simulator
Stdlib-only; no installs required. From the repo root:
```sh
for t in trace_loader enclave trial hand_checked closed_form; do
    python3 -m sim.tests.test_$t
done
```
`sim/` modules:
- `trace_loader` — loads the per-entry CSV (or generates synthetic) and exposes `Trace.pages`, `Trace.Q_w`, `filter_all_runs_intact`, magnitude-band sampler.
- `cache` — LRU with pre-cache suppression on insert.
- `enclave` — `BatchBuffer` (size + time triggers, B_min underflow) producing `Commit(B_eff, S_prime, victim_in_batch, …)`.
- `cover` — `Matched` / `Uniform` / `Stale` cover distributions over a `CoverUniverse`.
- `background` — pre-generates `BgEvent`s (bursts + idle Poisson) for the trial window.
- `attacker` — `score`, `candidate_set`, `rank`, `evaluate` (strict tie semantics).
- `trial` — `run_trial(params, victim_key, trace, cover_universe, seed) → TrialLog` per-batch + cross-batch results.

## Latency benchmarks (DoH / ODoH)
- `SITES=data/name.csv RUNS=2 ./run-doh.sh` (needs venv). Outputs: `runs/doh-<ts>/results_har_doh.csv`.
- `SITES=data/name.csv RUNS=2 ./run-odoh.sh` (needs venv). Outputs: `runs/odoh-<ts>/results_har_odoh.csv`.
- Defaults: `SITES=data/umbrella-top-10k-resolvable.csv` and `RUNS=1`.
- Generate plots from `plots/` (inside venv):
    - DoH: `cd plots && python3 prepare_plot_data.py ../runs/doh-<ts>/results_har_doh.csv -o cdf_doh.dat` → `cdf_doh.dat`, `cdf_doh_dns_ratio.dat`.
    - ODoH: `cd plots && python3 prepare_plot_data.py ../runs/odoh-<ts>/results_har_odoh.csv -o cdf_odoh.dat` → `cdf_odoh.dat`, `cdf_odoh_dns_ratio.dat`.
- Plot using gnuplot (from `plots/`):
    - DNS vs page load time, DoH vs ODoH: `gnuplot plot_cdf.gnuplot` → `cdf_odoh_doh.pdf`.
    - DNS / page load time ratio, DoH vs ODoH: `gnuplot plot_dns_ratio.gnuplot` → `cdf_dns_ratio.pdf`.

## Operator notes (resolver/DNS tweaks)
- Change DNS for `enp113s0f0np0`: `sudo resolvectl dns enp113s0f0np0 127.0.0.1:5300`. Original DNS is at `127.0.0.1:53` (list with `sudo ss -ntlup | grep :53`).
- Disable `systemd-resolved` cache: edit `sudo vi /etc/systemd/resolved.conf.d/no-cache.conf`, add `[Resolve]\nCache=no`, then `sudo systemctl restart systemd-resolved`.
- "Send absolutely every DNS query to whatever DNS servers are configured on `enp113s0f0np0`": `sudo resolvectl domain enp113s0f0np0 ~.`. Revert with `sudo resolvectl revert enp113s0f0np0`.
- `systemd-resolved` queries all configured servers in parallel and uses whichever responds first (`sudo resolvectl status`). If the Global section lists `8.8.8.8` alongside `127.0.0.1:5300`, Google's resolver almost certainly wins the race against ODoH.
