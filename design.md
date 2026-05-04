# Page load benchmark

## Background
- We measure page load time and DNS lookup time across the Cisco Umbrella top-10k
- We compare: vanilla DNS, DoH, ODoH, and CoDoH (our cached ODoH)
- Visualization: CDF of page load times across all sites, one curve per strategy

## Approach
- Use Playwright (headless Chromium with persistent context) to load full pages including all subresources
- Capture DNS timings via HAR (record_har_path) rather than the JS Performance API, since the Performance API zeroes out cross-origin DNS times under Timing-Allow-Origin restrictions
- Page load time is read from `performance.getEntriesByType('navigation')` (loadEventEnd - startTime)
- Use a local DNS proxy to swap between DNS strategies without changing anything else

## Sites
- Cisco Umbrella top-10k, filtered to resolvable domains (see top-10k-resolvable.csv)
- 10 repetitions per site per strategy

## Chunks

### Chunk 1: Playwright baseline with vanilla DNS
- Use Playwright (headless Chromium) to load each site in top-10k-resolvable.csv
- Run: `python benchmark-har.py vanilla`
- Capture via HAR:
  - `timings.dns` per entry: DNS time per request (first lookup per host = per-domain DNS)
  - Main document DNS: matched by navigation host
  - Wall-clock DNS: merged overlapping intervals across all entries
  - Page load: `performance.getEntriesByType('navigation')` loadEventEnd - startTime
- Fresh persistent context (unique user_data_dir) per run to clear in-browser DNS/HTTP cache
- Output: CSV of (rank, site, run, strategy, main_dns_ms, total_dns_sum_ms, wall_clock_dns_ms, page_load_ms, unique_domains_resolved, domains_resolved)

### Chunk 2: Playwright with DoH via dnscrypt-proxy
- Run dnscrypt-proxy with `dnscrypt-proxy-doh.toml` (DoH upstream, no TRR)
- Set system DNS to 127.0.0.1:53 via `set-dns.sh`
- Same measurements as chunk 1
- Run: `python benchmark-har.py doh`

### Chunk 3: Playwright with ODoH via dnscrypt-proxy
- Stop the default dnscrypt-proxy service and restart with `dnscrypt-proxy-odoh.toml`
- Config uses `odoh-cloudflare` server with ODoH relays (anonymized routing)
- dnscrypt-proxy listens on `127.0.2.1:53`; Firefox uses system DNS (no TRR prefs needed)
- Internal cache disabled (`cache = false`) to measure real upstream ODoH latency
- Run: `python benchmark.py odoh`

### Chunk 4: Playwright with CoDoH via local DNS proxy
- Swap the proxy upstream to CoDoH
- Same measurements

### Chunk 5: analysis and visualization
- Combine all CSVs
- Plot CDF of page load times (one curve per strategy)
- Plot CDF of DNS lookup times (one curve per strategy)

## Things to consider
- Disable browser caching: use fresh browser context per run
- Flush local DNS resolver cache between runs (`sudo resolvectl flush-caches` or equivalent)
- Repeat the experiment locally and then later run in a cloud VM
- Randomize site visit order to avoid time-of-day bias
