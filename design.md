# Page load benchmark

## Background
- The ODoH paper measures page load time and DNS lookup time across 500 websites from the Tranco top 2000
- We compare: vanilla DNS, DoH, ODoH, and CoDoH (our cached ODoH)
- Visualization: CDF of page load times across all sites, one curve per strategy

## Approach
- Use Playwright (headless Firefox) to load full pages including all subresources
- Firefox is used because Chromium's headless shell reports 0ms for DNS timing
- Capture DNS and page load timing via the Performance API (window.performance)
- Use a local DNS proxy to swap between DNS strategies without changing anything else

## Sites
- 10 sites from Tranco top 2000 (see top-10.csv)
- 10 repetitions per site per strategy

## Chunks

### Chunk 1: Playwright baseline with vanilla DNS
- Use Playwright (headless Firefox) to load each site in top-10.csv
- Run: `python benchmark.py vanilla`
- Capture via Performance API:
  - `performance.timing`: domainLookupEnd - domainLookupStart (main document DNS)
  - `performance.timing`: loadEventEnd - navigationStart (full page load time)
  - `performance.getEntriesByType('resource')`: DNS time per subresource domain
- Disable browser caching
- Flush local DNS cache between runs
- Output: CSV of (site, run#, strategy, main_dns_ms, page_load_ms, unique_domains, total_dns_ms)

### Chunk 2: Playwright with DoH via Firefox TRR
- Use Firefox's built-in Trusted Recursive Resolver (TRR) instead of an external proxy
- Set `network.trr.mode=3` (TRR only), `network.trr.uri=https://1.1.1.1/dns-query`
- No system-level DNS changes needed; the browser handles DoH natively
- Same measurements as chunk 1
- Run: `python benchmark.py doh`

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
