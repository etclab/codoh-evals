#!/usr/bin/env python3
"""Page load benchmark using Playwright with configurable DNS strategy."""

import csv
import logging
import subprocess
import sys
import time
from playwright.sync_api import sync_playwright

VALID_STRATEGIES = {"vanilla", "doh"}

SITES_FILE = "top-10.csv"
LOG_FILE = "benchmark.log"
RUNS_PER_SITE = 2

# Parse strategy from command line
STRATEGY = sys.argv[1] if len(sys.argv) > 1 else "vanilla"
if STRATEGY not in VALID_STRATEGIES:
    print(f"Unknown strategy '{STRATEGY}'. Valid: {', '.join(sorted(VALID_STRATEGIES))}")
    sys.exit(1)
OUTPUT_FILE = f"results_{STRATEGY}.csv"

# Set up file logger
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def flush_dns_cache():
    """Flush the system DNS resolver cache."""
    try:
        result = subprocess.run(
            ["resolvectl", "flush-caches"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            log.info("DNS cache flushed successfully")
        else:
            log.warning("DNS cache flush failed: %s", result.stderr.strip())
    except Exception as e:
        log.warning("DNS cache flush error: %s", e)


def load_sites(path):
    """Load site list from CSV (rank, domain)."""
    sites = []
    with open(path) as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2:
                sites.append(row[1].strip())
    return sites


def measure_page(page, url):
    """Load a URL and return timing metrics."""
    try:
        page.goto(url, wait_until="load", timeout=30000)
    except Exception as e:
        log.error("Failed to load %s: %s", url, e)
        print(f"  Error loading {url}: {e}")
        return None

    timings = page.evaluate("""() => {
        const [nav] = performance.getEntriesByType('navigation');
        const mainDns = nav.domainLookupEnd - nav.domainLookupStart;
        const pageLoad = nav.loadEventEnd - nav.startTime;

        const resources = performance.getEntriesByType('resource');
        const domainDns = {};
        for (const r of resources) {
            try {
                const host = new URL(r.name).hostname;
                const dns = r.domainLookupEnd - r.domainLookupStart;
                if (!domainDns[host]) {
                    domainDns[host] = dns;
                }
            } catch(e) {}
        }

        const uniqueDomains = Object.keys(domainDns).length;
        const totalDns = Object.values(domainDns).reduce((a, b) => a + b, 0);

        return {
            mainDns: mainDns,
            pageLoad: pageLoad,
            uniqueDomains: uniqueDomains,
            totalDns: totalDns + mainDns,
            domainDetails: domainDns,
        };
    }""")

    # Log per-domain DNS details
    for host, dns in timings["domainDetails"].items():
        if dns > 0:
            log.info("  DNS lookup %s: %.1fms", host, dns)

    return timings


def run_benchmark():
    sites = load_sites(SITES_FILE)
    log.info("=" * 60)
    log.info("Benchmark started: strategy=%s, sites=%d, runs=%d", STRATEGY, len(sites), RUNS_PER_SITE)
    print(f"Loaded {len(sites)} sites")

    results = []

    with sync_playwright() as p:
        # Firefox reports accurate DNS timing via the Performance API.
        # Chromium's headless shell reports 0ms for DNS lookups.
        launch_kwargs = {"headless": True}
        if STRATEGY == "doh":
            launch_kwargs["firefox_user_prefs"] = {
                "network.trr.mode": 3,                       # TRR only (no fallback to system DNS)
                "network.trr.uri": "https://1.1.1.1/dns-query",  # Cloudflare DoH
                "network.trr.bootstrapAddr": "1.1.1.1",      # Avoid chicken-and-egg DNS lookup
            }
            log.info("DoH enabled via Firefox TRR (mode=3, resolver=1.1.1.1)")
        browser = p.firefox.launch(**launch_kwargs)

        for site in sites:
            url = f"https://{site}"
            log.info("Starting site: %s", url)
            print(f"\nBenchmarking {url}")

            for run in range(1, RUNS_PER_SITE + 1):
                # Fresh context per run = no caching
                context = browser.new_context()
                page = context.new_page()

                flush_dns_cache()
                time.sleep(0.5)  # brief pause after cache flush

                timings = measure_page(page, url)

                if timings:
                    row = {
                        "site": site,
                        "run": run,
                        "strategy": STRATEGY,
                        "main_dns_ms": timings["mainDns"],
                        "page_load_ms": timings["pageLoad"],
                        "unique_domains": timings["uniqueDomains"],
                        "total_dns_ms": timings["totalDns"],
                    }
                    results.append(row)
                    log.info("  Run %d: dns=%.1fms load=%.1fms domains=%d",
                             run, timings["mainDns"], timings["pageLoad"], timings["uniqueDomains"])
                    print(f"  Run {run}: dns={timings['mainDns']:.1f}ms load={timings['pageLoad']:.1f}ms domains={timings['uniqueDomains']}")
                else:
                    log.warning("  Run %d: FAILED", run)

                context.close()

        browser.close()

    # Write results
    fieldnames = ["site", "run", "strategy", "main_dns_ms", "page_load_ms", "unique_domains", "total_dns_ms"]
    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    log.info("Results written to %s (%d rows)", OUTPUT_FILE, len(results))
    print(f"\nResults written to {OUTPUT_FILE}")


if __name__ == "__main__":
    run_benchmark()
