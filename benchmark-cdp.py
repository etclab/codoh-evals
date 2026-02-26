#!/usr/bin/env python3
"""Page load benchmark using Playwright + Chrome DevTools Protocol."""

import csv
import logging
import subprocess
import sys
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

VALID_STRATEGIES = {"vanilla", "odoh"}

SITES_FILE = "top-10.csv"
LOG_FILE = "benchmark.log"
RUNS_PER_SITE = 2

CHROMIUM_ARGS = [
    "--dns-prefetch-disable",
    "--disable-background-networking",
    "--disable-features=AsyncDns,DnsOverHttps",  # force system resolver
]

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


def measure_page(context, page, url):
    """Load a URL and return timing metrics via CDP."""
    dns_timings = []

    client = context.new_cdp_session(page)
    client.send("Network.enable")

    def on_response_received(event):
        resp = event.get("response", {})
        timing = resp.get("timing")
        if not timing:
            return
        dns_start = timing.get("dnsStart", -1)
        dns_end = timing.get("dnsEnd", -1)
        req_url = resp.get("url", "")
        try:
            host = urlparse(req_url).hostname or ""
        except Exception:
            host = ""
        request_time = timing.get("requestTime", 0)
        dns_timings.append({
            "host": host,
            "dnsStart": dns_start,
            "dnsEnd": dns_end,
            "requestTime": request_time,
            "url": req_url,
            "type": event.get("type", ""),
        })

    client.on("Network.responseReceived", on_response_received)

    try:
        page.goto(url, wait_until="load", timeout=30000)
        page.wait_for_timeout(2000)  # let lazy JS fire off subresource fetches
    except Exception as e:
        log.error("Failed to load %s: %s", url, e)
        print(f"  Error loading {url}: {e}")
        try:
            client.detach()
        except Exception:
            pass
        return None

    # Page load time from Performance API (unaffected by TAO)
    page_load = page.evaluate("""() => {
        const [nav] = performance.getEntriesByType('navigation');
        return nav ? nav.loadEventEnd - nav.startTime : 0;
    }""")

    try:
        client.detach()
    except Exception:
        pass

    # Compute metrics from CDP timings
    # dnsStart/dnsEnd are ms offsets from requestTime (which is in seconds).
    # To compare across requests, convert to absolute seconds.
    nav_host = urlparse(url).hostname
    main_dns = 0.0
    first_abs_dns_start = float("inf")
    last_abs_dns_end = 0.0
    domains_with_dns = set()

    for t in dns_timings:
        ds = t["dnsStart"]
        de = t["dnsEnd"]
        if ds >= 0 and de > ds:
            dns_ms = de - ds  # already in milliseconds
            abs_start = t["requestTime"] + ds / 1000  # absolute seconds
            abs_end = t["requestTime"] + de / 1000

            if abs_start < first_abs_dns_start:
                first_abs_dns_start = abs_start
            if abs_end > last_abs_dns_end:
                last_abs_dns_end = abs_end
            domains_with_dns.add(t["host"])

            log.info("  DNS lookup %s (%s): %.1fms [requestTime=%.6f dnsStart=%.3f dnsEnd=%.3f]",
                     t["host"], t["type"], dns_ms, t["requestTime"], ds, de)
            print(f"    DNS {t['host']} ({t['type']}): {dns_ms:.1f}ms "
                  f"[requestTime={t['requestTime']:.6f} dnsStart={ds:.3f} dnsEnd={de:.3f}]")

        # Main document DNS: match on the navigation host (handles redirects)
        if t["host"] == nav_host and ds >= 0 and de > ds:
            main_dns = de - ds
        # Fallback: first Document request with valid DNS
        if main_dns == 0.0 and t["type"] == "Document" and ds >= 0 and de > ds:
            main_dns = de - ds

    dns_span = (last_abs_dns_end - first_abs_dns_start) * 1000 if first_abs_dns_start < float("inf") else 0.0

    return {
        "mainDns": main_dns,
        "dnsSpan": dns_span,
        "pageLoad": page_load,
        "uniqueDomains": len(domains_with_dns),
    }


def run_benchmark():
    sites = load_sites(SITES_FILE)
    log.info("=" * 60)
    log.info("Benchmark started: strategy=%s, sites=%d, runs=%d", STRATEGY, len(sites), RUNS_PER_SITE)
    print(f"Loaded {len(sites)} sites")

    results = []

    with sync_playwright() as p:
        for site in sites:
            url = f"https://{site}"
            log.info("Starting site: %s", url)
            print(f"\nBenchmarking {url}")

            for run in range(1, RUNS_PER_SITE + 1):
                flush_dns_cache()
                time.sleep(0.5)

                # Restart browser each run to clear Chromium's internal DNS cache
                browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
                context = browser.new_context()
                page = context.new_page()

                timings = measure_page(context, page, url)

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
