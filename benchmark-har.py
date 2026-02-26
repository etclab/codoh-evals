#!/usr/bin/env python3
"""Page load benchmark using Playwright HAR capture for accurate DNS timings.

Unlike the JS Performance API, HAR capture is not subject to the
Timing-Allow-Origin (TAO) restriction, so cross-origin DNS/TCP/TLS
timings are reported accurately.
"""

import csv
import json
import logging
import os
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

VALID_STRATEGIES = {"vanilla", "odoh"}

SITES_FILE = "top-10.csv"
LOG_FILE = "benchmark-har.log"
RUNS_PER_SITE = 2

CHROMIUM_ARGS = [
    "--dns-prefetch-disable",
    "--disable-background-networking",
    "--disable-features=AsyncDns,DnsOverHttps",  # force system resolver
    "--disable-features=HttpCache",
]

# Parse strategy from command line
STRATEGY = sys.argv[1] if len(sys.argv) > 1 else "vanilla"
if STRATEGY not in VALID_STRATEGIES:
    print(f"Unknown strategy '{STRATEGY}'. Valid: {', '.join(sorted(VALID_STRATEGIES))}")
    sys.exit(1)
OUTPUT_FILE = f"results_har_{STRATEGY}.csv"

# Set up file logger
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def load_sites(path):
    """Load site list from CSV (rank, domain)."""
    sites = []
    with open(path) as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2:
                sites.append(row[1].strip())
    return sites


def parse_har(har_file, nav_host):
    """Parse a HAR file and extract DNS timing metrics.

    Returns a dict with:
      - main_dns: DNS time (ms) for the main document request
      - total_dns_sum: sum of all positive DNS lookup times (ms)
      - domain_dns: dict mapping hostname -> dns_ms (first lookup only)
    """
    domain_dns = {}
    total_dns_sum = 0.0
    main_dns = 0.0

    with open(har_file, "r", encoding="utf-8") as f:
        har_data = json.load(f)

        for i, entry in enumerate(har_data["log"]["entries"]):
            req_url = entry["request"]["url"]
            timings = entry.get("timings", {})

            # HAR spec: dns is ms, -1 means connection/DNS was reused
            dns_ms = timings.get("dns", -1)

            try:
                host = urlparse(req_url).hostname or ""
            except Exception:
                host = ""

            if dns_ms > 0:
                total_dns_sum += dns_ms

                # Track first DNS lookup per domain
                if host and host not in domain_dns:
                    domain_dns[host] = dns_ms

                log.info("  HAR entry %d: %s dns=%.1fms", i, host, dns_ms)

            # Main document DNS: match on navigation host
            if host == nav_host and dns_ms > 0 and main_dns == 0.0:
                main_dns = dns_ms

            # Fallback: first entry with a valid DNS lookup
            if main_dns == 0.0 and i == 0 and dns_ms > 0:
                main_dns = dns_ms

    return {
        "main_dns": main_dns,
        "total_dns_sum": total_dns_sum,
        "domain_dns": domain_dns,
    }


def run_benchmark():
    sites = load_sites(SITES_FILE)
    log.info("=" * 60)
    log.info("HAR Benchmark started: strategy=%s, sites=%d, runs=%d",
             STRATEGY, len(sites), RUNS_PER_SITE)
    print(f"Loaded {len(sites)} sites  [strategy={STRATEGY}]")

    results = []

    with sync_playwright() as p:
        for site in sites:
            url = f"https://{site}"
            nav_host = urlparse(url).hostname
            log.info("Starting site: %s", url)
            print(f"\nBenchmarking {url}")

            for run in range(1, RUNS_PER_SITE + 1):
                har_file = f"/tmp/har_{site}_{run}_{os.getpid()}.har"

                # Restart browser each run to clear internal DNS cache
                browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
                context = browser.new_context(record_har_path=har_file)
                page = context.new_page()

                try:
                    page.goto(url, wait_until="load", timeout=30000)
                    page.wait_for_timeout(2000)  # let lazy JS fire subresource fetches

                    page_load = page.evaluate("""() => {
                        const [nav] = performance.getEntriesByType('navigation');
                        return nav ? nav.loadEventEnd - nav.startTime : 0;
                    }""")
                except Exception as e:
                    log.error("Failed to load %s: %s", url, e)
                    print(f"  Run {run}: FAILED ({e})")
                    context.close()
                    browser.close()
                    if os.path.exists(har_file):
                        os.remove(har_file)
                    continue

                # Closing the context flushes network data to the HAR file
                context.close()
                browser.close()

                # Parse HAR for DNS timings
                har_metrics = parse_har(har_file, nav_host)
                main_dns = har_metrics["main_dns"]
                total_dns = har_metrics["total_dns_sum"]
                domain_dns = har_metrics["domain_dns"]

                # Log per-domain breakdown
                for host, dns_ms in sorted(domain_dns.items()):
                    print(f"    DNS {host}: {dns_ms:.1f}ms")

                # Clean up temp file
                if os.path.exists(har_file):
                    os.remove(har_file)

                row = {
                    "site": site,
                    "run": run,
                    "strategy": STRATEGY,
                    "main_dns_ms": main_dns,
                    "total_dns_sum_ms": total_dns,
                    "page_load_ms": page_load,
                    "unique_domains_resolved": len(domain_dns),
                }
                results.append(row)

                log.info("  Run %d: main_dns=%.1fms total_dns=%.1fms load=%.1fms domains=%d",
                         run, main_dns, total_dns, page_load, len(domain_dns))
                print(f"  Run {run}: main_dns={main_dns:.1f}ms "
                      f"total_dns={total_dns:.1f}ms "
                      f"load={page_load:.1f}ms "
                      f"domains_resolved={len(domain_dns)}")

    # Write results
    fieldnames = [
        "site", "run", "strategy",
        "main_dns_ms", "total_dns_sum_ms",
        "page_load_ms", "unique_domains_resolved",
    ]
    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    log.info("Results written to %s (%d rows)", OUTPUT_FILE, len(results))
    print(f"\nResults written to {OUTPUT_FILE}")


if __name__ == "__main__":
    run_benchmark()
