## Benchmarks
- Ensure [`dnscrypt-proxy`](https://github.com/etclab/dnscrypt-proxy/) is installed alongside this repo
- Run DoH with: `SITES=name.csv RUNS=2 ./run-doh.sh`
    - Outputs: `results_har_doh.csv`
- Run ODoH with: `SITES=name.csv RUNS=2 ./run-doh.sh`
    - Outputs: `results_har_odoh.csv`
- Default values for `SITES=sampled-100-of-2000-resolvable.csv` and `RUNS=1`
- Once you have the results run (inside venv): 
    - For DoH: `python3 prepare_plot_data.py results_har_doh.csv -o cdf_doh.dat`
        - Outputs: `cdf_doh.dat` and `cdf_doh_dns_ratio.dat`
    - For ODoH: `python3 prepare_plot_data.py results_har_odoh.csv -o cdf_odoh.dat`
        - Outputs: `cdf_odoh.dat` and `cdf_odoh_dns_ratio.dat`
- Plot using gnuplot:
    - Compares dns vs page load time betn DoH & ODoH: `gnuplot prepare_plot_data.gnuplot`
        - Outputs: `cdf_odoh_doh.pdf`
    - Compare (dns / page load) time ratio betn DoH & ODoH: `gnuplot plot_dns_ratio.gnuplot`
        - Outputs: `cdf_dns_ratio.pdf`
    

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
