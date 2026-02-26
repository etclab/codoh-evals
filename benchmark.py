#!/usr/bin/env python3
"""Page load benchmark using Playwright with configurable DNS strategy."""

import csv
import logging
import subprocess
import sys
import time
from playwright.sync_api import sync_playwright

VALID_STRATEGIES = {"vanilla", "doh", "odoh"}

SITES_FILE = "top-10.csv"
LOG_FILE = "benchmark.log"
RUNS_PER_SITE = 1

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
        const pageLoad = nav.loadEventEnd - nav.startTime;

        // Collect all entries (navigation + resources) to find the full
        // DNS time span: first lookup start → last lookup end.
        const allEntries = [nav, ...performance.getEntriesByType('resource')];

        let firstDnsStart = Infinity;
        let lastDnsEnd = 0;
        const domainDns = {};

        for (const entry of allEntries) {
            const start = entry.domainLookupStart;
            const end   = entry.domainLookupEnd;
            if (start > 0 && end > 0 && end > start) {
                if (start < firstDnsStart) firstDnsStart = start;
                if (end   > lastDnsEnd)    lastDnsEnd = end;
            }
            // Per-domain breakdown (resources only)
            if (entry !== nav) {
                try {
                    const host = new URL(entry.name).hostname;
                    const dns = end - start;
                    if (!domainDns[host]) domainDns[host] = dns;
                } catch(e) {}
            }
        }

        const mainDns = nav.domainLookupEnd - nav.domainLookupStart;
        const dnsSpan = (firstDnsStart < Infinity) ? lastDnsEnd - firstDnsStart : 0;
        const uniqueDomains = Object.keys(domainDns).length;

        return {
            mainDns: mainDns,
            dnsSpan: dnsSpan,
            pageLoad: pageLoad,
            uniqueDomains: uniqueDomains,
            firstDnsStart: firstDnsStart < Infinity ? firstDnsStart : 0,
            lastDnsEnd: lastDnsEnd,
            domainDetails: domainDns,
            nav_array: nav.toJSON(),
            resource_array: performance.getEntriesByType('resource').map(e => e.toJSON()),
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
        # Disable Firefox's internal DNS cache so every navigation does a
        # fresh lookup.  This is critical for ODoH/vanilla where DNS goes
        # through the system resolver — without it, Firefox serves cached
        # IPs and the Performance API reports ~0 ms.
        dns_no_cache_prefs = {
            "network.dnsCacheEntries": 0,
            "network.dnsCacheExpiration": 0,
        }

        launch_kwargs = {"headless": True}
        if STRATEGY == "doh":
            launch_kwargs["firefox_user_prefs"] = {
                **dns_no_cache_prefs,
                "network.trr.mode": 3,                       # TRR only (no fallback to system DNS)
                "network.trr.uri": "https://1.1.1.1/dns-query",  # Cloudflare DoH
                "network.trr.bootstrapAddr": "1.1.1.1",      # Avoid chicken-and-egg DNS lookup
            }
            log.info("DoH enabled via Firefox TRR (mode=3, resolver=1.1.1.1)")
        elif STRATEGY == "odoh":
            launch_kwargs["firefox_user_prefs"] = {
                **dns_no_cache_prefs,
            }
            log.info("ODoH enabled via system DNS -> dnscrypt-proxy on 127.0.0.1:5300")
        else:
            launch_kwargs["firefox_user_prefs"] = {
                **dns_no_cache_prefs,
            }
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
                        "dns_span_ms": timings["dnsSpan"],
                        "page_load_ms": timings["pageLoad"],
                        "unique_domains": timings["uniqueDomains"],
                    }
                    results.append(row)
                    log.info("  Run %d: main_dns=%.1fms dns_span=%.1fms load=%.1fms domains=%d",
                             run, timings["mainDns"], timings["dnsSpan"], timings["pageLoad"], timings["uniqueDomains"])
                    print(f"  Run {run}: main_dns={timings['mainDns']:.1f}ms dns_span={timings['dnsSpan']:.1f}ms load={timings['pageLoad']:.1f}ms domains={timings['uniqueDomains']}")
                    print(f"  nav_array: {timings['nav_array']}")
                    print(f"  resource_array: {timings['resource_array']}")
                else:
                    log.warning("  Run %d: FAILED", run)

                context.close()

        browser.close()

    # Write results
    fieldnames = ["site", "run", "strategy", "main_dns_ms", "dns_span_ms", "page_load_ms", "unique_domains"]
    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    log.info("Results written to %s (%d rows)", OUTPUT_FILE, len(results))
    print(f"\nResults written to {OUTPUT_FILE}")


if __name__ == "__main__":
    run_benchmark()
