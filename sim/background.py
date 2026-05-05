"""Background traffic model.

`λ_bg` = N concurrent users. Each user emits two streams:
  - **Bursts**: every ~30s (Poisson), pick a random page from the trace
    and replay its `Q_w` with the page's per-entry inter-arrivals.
  - **Idle Poisson heartbeat**: 0.01 qps from the Zipf-weighted cover
    universe (CrUX top-1M, see `cover.py`).

Both streams flow into the same enclave batch buffer.

Pre-generates events for the trial window [0, t_end_ms] and returns them
as a sorted list of (host, t_ms). Trial-scale event counts are small
enough for this to be cheap (e.g. 100 users × 30s window ≈ 800 events).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .cover import CoverUniverse
from .trace_loader import Trace


@dataclass(frozen=True)
class BgEvent:
    hostname: str
    t_ms: float


def generate_bg_events(
    trace: Trace,
    universe: CoverUniverse,
    *,
    n_users: int,
    t_end_ms: float,
    rng: random.Random,
    burst_period_s: float = 30.0,
    idle_qps: float = 0.01,
) -> list[BgEvent]:
    """Pre-sample all background events on [0, t_end_ms].

    Bursts: per-user, draw burst arrival times from a Poisson process with
    rate 1/burst_period_s, replay a random trace page's per-entry stream
    starting at each burst time.

    Idle: per-user, Poisson(0.01 qps) heartbeat of random Zipf-weighted
    universe domains, modeled as inter-arrival ~ Exp(rate).
    """
    events: list[BgEvent] = []
    if n_users <= 0 or t_end_ms <= 0:
        return events

    page_keys = list(trace.pages.keys())
    if not page_keys and n_users > 0:
        raise ValueError("trace has no pages — cannot generate bursts")

    universe_domains = universe.domains
    universe_weights = universe.weights
    burst_rate_per_ms = 1.0 / (burst_period_s * 1000.0)
    idle_rate_per_ms = idle_qps / 1000.0

    for _user in range(n_users):
        # bursts
        t = rng.expovariate(burst_rate_per_ms)
        while t < t_end_ms:
            page = trace.pages[rng.choice(page_keys)]
            for entry in page:
                tq = t + entry.offset_ms
                if tq < t_end_ms:
                    events.append(BgEvent(entry.hostname, tq))
            t += rng.expovariate(burst_rate_per_ms)

        # idle Poisson heartbeat
        t = rng.expovariate(idle_rate_per_ms)
        while t < t_end_ms:
            host = rng.choices(universe_domains, weights=universe_weights, k=1)[0]
            events.append(BgEvent(host, t))
            t += rng.expovariate(idle_rate_per_ms)

    events.sort(key=lambda e: e.t_ms)
    return events
