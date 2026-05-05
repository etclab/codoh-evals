## Layout
- `data/` — input domain CSVs:
    - `crux-top10k-resolvable.csv` — Chrome User Experience Report top-10k origins (3 magnitude bands: top-1k, top-5k, top-10k), filtered to those that resolve. Each row is `rank,hostname` where `rank` is the pre-filter CrUX position (1..9968, with gaps where the hostname didn't resolve). Stratify by `rank ≤ 1000` (top-1k bucket), `1000 < rank ≤ 5000` (1k–5k), `5000 < rank ≤ 10000` (5k–10k). 9,869 rows; **primary input for the leakage simulator.**
    - `umbrella-top-10k-resolvable.csv` — Cisco Umbrella top-10k filtered to resolvable. Retained for the legacy DoH/ODoH page-load benchmarks; not used by the simulator.
- `plots/` — plotting scripts (`prepare_plot_data.py`, `*.gnuplot`) and the legacy `.dat`/`.pdf`/`results_har_*.csv` snapshots
- `runs/` — timestamped per-strategy benchmark output:
    - `results_har_<strategy>.csv` — aggregated per (site, run): page-load and DNS summaries (drives CDF plots)
    - `entries_har_<strategy>.csv` — per-entry DNS trace `(rank, site, run, day, hostname, started_offset_ms, dns_ms)`, one row per resolved lookup in HAR emission order; consumed by the leakage simulator

## Benchmarks
- Ensure [`dnscrypt-proxy`](https://github.com/etclab/dnscrypt-proxy/) is installed alongside this repo
- Run DoH with: `SITES=data/name.csv RUNS=2 ./run-doh.sh`
    - Outputs: `runs/doh-<ts>/results_har_doh.csv`
- Run ODoH with: `SITES=data/name.csv RUNS=2 ./run-odoh.sh`
    - Outputs: `runs/odoh-<ts>/results_har_odoh.csv`
- Default values for `SITES=data/top-10k-resolvable.csv` and `RUNS=1`.
- Once you have the results, generate plots from `plots/` (inside venv):
    - For DoH: `cd plots && python3 prepare_plot_data.py ../runs/doh-<ts>/results_har_doh.csv -o cdf_doh.dat`
        - Outputs: `cdf_doh.dat` and `cdf_doh_dns_ratio.dat`
    - For ODoH: `cd plots && python3 prepare_plot_data.py ../runs/odoh-<ts>/results_har_odoh.csv -o cdf_odoh.dat`
        - Outputs: `cdf_odoh.dat` and `cdf_odoh_dns_ratio.dat`
- Plot using gnuplot (from `plots/`):
    - DNS vs page load time, DoH vs ODoH: `gnuplot plot_cdf.gnuplot`
        - Outputs: `cdf_odoh_doh.pdf`
    - DNS / page load time ratio, DoH vs ODoH: `gnuplot plot_dns_ratio.gnuplot`
        - Outputs: `cdf_dns_ratio.pdf`
- Ensure you run `make` inside of `coredns` while cloning the repo as go deps won't be resolved if `coredns` is used for the first time.
- Ensure you run `cert-maker.sh` to generate/load certificates.
    

## For Simulation
The leakage simulator consumes the per-entry trace produced
by `benchmark-har.py vanilla` — `entries_har_vanilla.csv`, schema
`(rank, site, run, day, hostname, started_offset_ms, dns_ms)`, one row per
resolved DNS lookup in HAR emission order. Vanilla is sufficient because `Q_w`
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

## Steps
- download [`dnscrypt-proxy`](https://github.com/etclab/dnscrypt-proxy/) and start using: `./run.sh`
- use `set-dns.sh` to setup system dns to use `dnscrypt-proxy`
- setup benchmark with: `./setup-benchmark.sh`
- run benchmark with: `source venv/bin/activate && python3 benchmark-har.py odoh`

## Scratch
- Change DNS for `enp113s0f0np0` with: `sudo resolvectl dns enp113s0f0np0 127.0.0.1:5300`
- Original DNS is at: 127.0.0.1:53 (list using: `sudo ss -ntlup | grep :53`)

- To disable systemd-resolverd cache
    - Edit file: `sudo vi /etc/systemd/resolved.conf.d/no-cache.conf`
    - Add `[Resolve]\nCache=no` to it
    - Restart systemd-resolverd: `sudo systemctl restart systemd-resolved`

- "Send absolutely every DNS query to whatever DNS servers are configured on `enp113s0f0np0` interface"
    - `sudo resolvectl domain enp113s0f0np0 ~.`
    - Revert the above change with: `sudo resolvectl revert enp113s0f0np0`

- `systemd-resolved` queries(?) all configured servers in parallel and uses whichever responds first.
    - Command: `sudo resolvectl status`
    - Output: The Global section has `8.8.8.8` as the current DNS server alongside `127.0.0.1:5300`. systemd-resolved is querying both in parallel and using whichever responds first — Google's 8.8.8.8 is almost certainly faster than ODoH, so it wins the race (possibly).
