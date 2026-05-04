#!/usr/bin/env python3
"""Page load benchmark using Playwright HAR capture for accurate DNS timings.

Unlike the JS Performance API, HAR capture is not subject to the
Timing-Allow-Origin (TAO) restriction, so cross-origin DNS/TCP/TLS
timings are reported accurately.

Usage:
    python benchmark-har.py <strategy> [options]

Examples:
    python benchmark-har.py vanilla
    python benchmark-har.py odoh --sites top-100.csv --runs 10
    python benchmark-har.py odoh --runs 5 --randomize
"""

import argparse
import csv
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

VALID_STRATEGIES = {"vanilla", "odoh", "doh", "codoh"}

CHROMIUM_ARGS = [
    "--dns-prefetch-disable",
    "--disable-background-networking",
    "--disable-features=AsyncDns,DnsOverHttps",  # force system resolver
    "--disable-features=HttpCache",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Page-load benchmark with HAR-based DNS timing.",
    )
    parser.add_argument(
        "strategy",
        choices=sorted(VALID_STRATEGIES),
        help="DNS resolution strategy to benchmark",
    )
    parser.add_argument(
        "--sites", default="data/top-10k-resolvable.csv",
        help="CSV file with (rank, domain) rows (default: data/top-10k-resolvable.csv)",
    )
    parser.add_argument(
        "--log",
        help="Log file path (default: benchmark-har-<strategy>.log)",
    )
    parser.add_argument(
        "--runs", type=int, default=2,
        help="Number of runs per site (default: 2)",
    )
    parser.add_argument(
        "--randomize", action="store_true",
        help="Shuffle site order each cycle to avoid time-of-day bias. "
             "Without this flag, all runs for a site are done consecutively.",
    )
    parser.add_argument(
        "--output",
        help="Output CSV path (default: results_har_<strategy>.csv)",
    )
    return parser.parse_args()


args = parse_args()
OUTPUT_FILE = args.output or f"results_har_{args.strategy}.csv"
LOG_FILE = args.log or f"benchmark-har-{args.strategy}.log"

# Set up file logger
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def load_sites(path):
    """Load site list from CSV (rank, domain). Returns list of (rank, domain) tuples."""
    sites = []
    with open(path) as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2:
                sites.append((int(row[0].strip()), row[1].strip()))
    return sites


def _merge_intervals_duration(intervals):
    """Merge overlapping (start, end) intervals and return total duration."""
    if not intervals:
        return 0.0
    sorted_intervals = sorted(intervals)
    merged_duration = 0.0
    current_start, current_end = sorted_intervals[0]
    for start, end in sorted_intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            merged_duration += current_end - current_start
            current_start, current_end = start, end
    merged_duration += current_end - current_start
    return merged_duration


def parse_har(har_file, nav_host):
    """Parse a HAR file and extract DNS timing metrics.

    Returns a dict with:
      - main_dns: DNS time (ms) for the main document request
      - total_dns_sum: sum of all positive DNS lookup times (ms)
      - wall_clock_dns: wall-clock DNS time after merging parallel intervals (ms)
      - domain_dns: dict mapping hostname -> dns_ms (first lookup only)
    """
    domain_dns = {}
    total_dns_sum = 0.0
    main_dns = 0.0
    dns_intervals = []  # list of (start_ms, end_ms) for wall-clock calculation

    with open(har_file, "r", encoding="utf-8") as f:
        har_data = json.load(f)

        entries = har_data["log"]["entries"]

        # Use the first entry's startedDateTime as the baseline
        # https://w3c.github.io/web-performance/specs/HAR/Overview.html#sec-object-types-timings
        t0 = None
        if entries:
            t0 = datetime.fromisoformat(entries[0]["startedDateTime"])
        # print(f"  Parsed HAR with {len(entries)} entries, baseline time: {t0}")

        for i, entry in enumerate(entries):
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

                # Reconstruct wall-clock DNS interval
                if t0:
                    entry_start = datetime.fromisoformat(entry["startedDateTime"])
                    offset_ms = (entry_start - t0) / timedelta(milliseconds=1)
                    blocked_ms = max(timings.get("blocked", 0), 0)
                    dns_start = offset_ms + blocked_ms
                    dns_end = dns_start + dns_ms
                    # print(f"    Entry {i}: {host} dns={dns_ms:.1f}ms "
                    #       f"interval=({dns_start:.1f}ms, {dns_end:.1f}ms), start={entry_start}")
                    dns_intervals.append((dns_start, dns_end))

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

    # Compute wall-clock DNS time by merging overlapping intervals
    wall_clock_dns = _merge_intervals_duration(dns_intervals)

    return {
        "main_dns": main_dns,
        "total_dns_sum": total_dns_sum,
        "wall_clock_dns": wall_clock_dns,
        "domain_dns": domain_dns,
    }


def _force_close(user_data_dir):
    """Force-kill this run's browser process tree. Match on the unique
    user-data-dir so we don't touch sibling benchmarks running in parallel."""
    subprocess.run(["pkill", "-9", "-f", user_data_dir], capture_output=True)
    time.sleep(0.5)


SITE_TIMEOUT_SECONDS = 60  # hard cap per site (backstop for Playwright's 30s)


def benchmark_site(p, site, rank, run, strategy):
    """Benchmark a single site and return a result dict, or None on failure."""
    url = f"https://{site}"
    nav_host = urlparse(url).hostname
    safe_site = site.replace("/", "_")
    har_file = f"/tmp/har_{safe_site}_{run}_{os.getpid()}.har"
    user_data_dir = f"/tmp/chrome_bench_{os.getpid()}_{run}_{safe_site}"

    # Delay between browser instances to avoid resource exhaustion
    time.sleep(1)

    # Use a thread-based watchdog instead of signal.SIGALRM.
    # Playwright's sync API uses greenlets, so SIGALRM exceptions don't
    # propagate through the greenlet boundary. A watchdog thread that kills
    # the browser process causes a connection error that Playwright handles
    # normally.
    timed_out = threading.Event()

    def _watchdog():
        timed_out.set()
        log.error("HARD TIMEOUT after %ds for %s — killing browser",
                  SITE_TIMEOUT_SECONDS, site)
        # Scope the kill to this run's unique user-data-dir so parallel
        # benchmark processes' browsers aren't collateral damage.
        subprocess.run(["pkill", "-9", "-f", user_data_dir], capture_output=True)

    timer = threading.Timer(SITE_TIMEOUT_SECONDS, _watchdog)
    timer.daemon = True
    timer.start()

    context = None
    try:
        # Restart browser each run to clear internal DNS cache.
        # launch_persistent_context embeds user_data_dir in the Chrome
        # cmdline, giving us a unique signature to pkill against.
        print(f"  Launching browser...", flush=True)
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=True,
            args=CHROMIUM_ARGS,
            record_har_path=har_file,
        )
        page = context.new_page()

        print(f"  Navigating to {url}...", flush=True)
        page.goto(url, wait_until="load", timeout=30000)
        page.wait_for_timeout(2000)  # let lazy JS fire subresource fetches

        page_load = page.evaluate("""() => {
            const [nav] = performance.getEntriesByType('navigation');
            return nav ? nav.loadEventEnd - nav.startTime : 0;
        }""")

        # Close inside try so the watchdog protects against context.close()
        # hanging on a stuck WebSocket.
        context.close()
    except Exception as e:
        timer.cancel()
        msg = (f"HARD TIMEOUT after {SITE_TIMEOUT_SECONDS}s — killing browser"
               if timed_out.is_set() else f"FAILED ({e})")
        log.error("%s run %d: %s", site, run, msg)
        print(f"  Run {run}: {msg}", flush=True)
        _force_close(user_data_dir)
        if os.path.exists(har_file):
            os.remove(har_file)
        shutil.rmtree(user_data_dir, ignore_errors=True)
        return None

    timer.cancel()
    shutil.rmtree(user_data_dir, ignore_errors=True)

    # Parse HAR for DNS timings
    har_metrics = parse_har(har_file, nav_host)
    main_dns = har_metrics["main_dns"]
    total_dns = har_metrics["total_dns_sum"]
    wall_clock_dns = har_metrics["wall_clock_dns"]
    domain_dns = har_metrics["domain_dns"]

    # Log per-domain breakdown
    # for host, dns_ms in sorted(domain_dns.items()):
    #     print(f"    DNS {host}: {dns_ms:.1f}ms")

    # Clean up temp file
    if os.path.exists(har_file):
        os.remove(har_file)

    row = {
        "rank": rank,
        "site": site,
        "run": run,
        "strategy": strategy,
        "main_dns_ms": main_dns,
        "total_dns_sum_ms": total_dns,
        "wall_clock_dns_ms": wall_clock_dns,
        "page_load_ms": page_load,
        "unique_domains_resolved": len(domain_dns),
        "domains_resolved": ";".join(sorted(domain_dns.keys())),
    }

    log.info("  Run %d: main_dns=%.1fms total_dns=%.1fms wall_clock_dns=%.1fms load=%.1fms domains=%d",
             run, main_dns, total_dns, wall_clock_dns, page_load, len(domain_dns))
    print(f"  Run {run}: main_dns={main_dns:.1f}ms "
          f"total_dns={total_dns:.1f}ms "
          f"wall_clock_dns={wall_clock_dns:.1f}ms "
          f"load={page_load:.1f}ms "
          f"domains_resolved={len(domain_dns)}",
          flush=True)

    return row


def run_benchmark():
    sites = load_sites(args.sites)
    strategy = args.strategy
    runs = args.runs
    randomize = args.randomize

    log.info("=" * 60)
    log.info("HAR Benchmark started: strategy=%s, sites=%d, runs=%d, randomize=%s",
             strategy, len(sites), runs, randomize)
    print(f"Loaded {len(sites)} sites  [strategy={strategy}, runs={runs}, "
          f"randomize={randomize}]")

    fieldnames = [
        "rank", "site", "run", "strategy",
        "main_dns_ms", "total_dns_sum_ms", "wall_clock_dns_ms",
        "page_load_ms", "unique_domains_resolved", "domains_resolved",
    ]

    row_count = 0
    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        f.flush()

        def _write(row):
            nonlocal row_count
            writer.writerow(row)
            f.flush()
            row_count += 1

        with sync_playwright() as p:
            if randomize:
                # Cycle-based: each cycle shuffles the site list independently
                # to eliminate time-of-day bias across sites.
                for cycle in range(1, runs + 1):
                    order = list(range(len(sites)))
                    random.shuffle(order)
                    ordered_sites = [sites[i] for i in order]
                    log.info("Cycle %d: order=%s", cycle, [s for _, s in ordered_sites])
                    print(f"\n--- Cycle {cycle}/{runs} "
                          f"(order: {', '.join(s for _, s in ordered_sites)}) ---")

                    for idx, (rank, site) in enumerate(ordered_sites, 1):
                        print(f"\n[{idx}/{len(ordered_sites)}] Benchmarking https://{site}", flush=True)
                        row = benchmark_site(p, site, rank, cycle, strategy)
                        if row:
                            _write(row)
            else:
                # Default: all runs for a site consecutively, then next site.
                for idx, (rank, site) in enumerate(sites, 1):
                    log.info("Starting site: https://%s", site)
                    print(f"\n[{idx}/{len(sites)}] Benchmarking https://{site}", flush=True)

                    for run in range(1, runs + 1):
                        row = benchmark_site(p, site, rank, run, strategy)
                        if row:
                            _write(row)

    log.info("Results written to %s (%d rows)", OUTPUT_FILE, row_count)
    print(f"\nResults written to {OUTPUT_FILE}")


if __name__ == "__main__":
    run_benchmark()
