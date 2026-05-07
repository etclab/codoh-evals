"""Lens (c) aggregation: per-day candidate sets, intersection over D days,
days-to-fingerprint (with right-censoring), repertoire accuracy, and the
Bayesian-posterior robustness check.

Per `lens-c-plan.md` §Aggregation:

  - days_to_fingerprint = smallest D with `|⋂_{d=1..D} C_d| ≤ |R| = 10`.
    Right-censored at `max_D` with `days_to_fp_censored = True` when never
    reached.
  - repertoire_accuracy_D = |R ∩ intersection_D| / |R| — catches cells
    that fingerprint to the *wrong* 10 (intersection size ≤ 10 but missing
    the true repertoire).
  - Bayesian posterior: uniform prior over `reference_set`, per-day
    Bernoulli likelihood with cell-fitted `p_obs`. Reported metric per
    cell = fraction of users whose true-repertoire posterior mass crosses
    `0.9` within `D ≤ max_D`.

Cell-level: median + p10/p90 over users for `days_to_fingerprint`;
posterior fraction as a separate column.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .lens_c import UserResult


# =====================================================================
# Per-user output
# =====================================================================

@dataclass
class UserMetrics:
    user_idx: int
    repertoire: list[str]
    per_day: list[dict] = field(default_factory=list)
    intersections: list[dict] = field(default_factory=list)
    days_to_fp_alpha: int = 0
    days_to_fp_censored: bool = True


# =====================================================================
# Per-cell rollup
# =====================================================================

@dataclass
class CellMetrics:
    cell_tag: str
    n_users: int
    days_to_fp_median: float
    days_to_fp_p10: float
    days_to_fp_p90: float
    n_censored: int
    posterior_fraction_users: float
    repertoire_acc_median_final: float
    underflow_day_rate_mean: float
    p_obs_fitted: float


# =====================================================================
# Primitives
# =====================================================================

def candidate_set(
    S_prime: set[str],
    reference_set: dict[str, set[str]],
    alpha: float,
) -> set[str]:
    """{w : |Q_w ∩ S'| / |Q_w| ≥ α}. Empty Q_w pages are excluded — they
    would otherwise score 0/0 and silently drop or inflate the candidate
    set depending on Python's division behavior.
    """
    out: set[str] = set()
    for w, Q_w in reference_set.items():
        if not Q_w:
            continue
        if len(Q_w & S_prime) / len(Q_w) >= alpha:
            out.add(w)
    return out


def days_to_fingerprint(
    per_day_C: list[set[str]],
    R_size: int,
) -> tuple[int, bool]:
    """Smallest `D` with `|⋂_{d=1..D} C_d| ≤ R_size`. Returns
    `(max_D, True)` when never reached.
    """
    if not per_day_C:
        return 0, True
    intersect: set[str] | None = None
    for d, C in enumerate(per_day_C, start=1):
        intersect = C if intersect is None else intersect & C
        if len(intersect) <= R_size:
            return d, False
    return len(per_day_C), True


def fit_p_obs(
    users: list[UserResult],
    reference_set: dict[str, set[str]],
) -> float:
    """Cell-level `p_obs` = mean per-day victim_score on the user's true
    repertoire = `mean over (user, day, w∈R) of |Q_w ∩ S'_d| / |Q_w|`.

    This is the empirical observation rate of victim domains in S' — the
    Bernoulli rate that anchors the Bayesian posterior likelihood. Fit
    once per cell from the cell's own data.
    """
    samples: list[float] = []
    for u in users:
        for day in u.days:
            for w in u.repertoire:
                Q_w = reference_set.get(w)
                if not Q_w:
                    continue
                samples.append(len(Q_w & day.S_prime_total) / len(Q_w))
    if not samples:
        return 0.5
    return sum(samples) / len(samples)


def bayesian_posterior_R(
    per_day_S: list[set[str]],
    reference_set: dict[str, set[str]],
    repertoire: list[str],
    p_obs: float,
) -> list[float]:
    """Per-day cumulative posterior mass on the true repertoire R.

    log L(S'_d | w) = n_hit · log p_obs + (|Q_w| − n_hit) · log(1 − p_obs)
        where n_hit = |Q_w ∩ S'_d|.
    log_post[w] starts at log_prior = −log|reference_set|; cumulative.
    Per-day mass = Σ_{w ∈ R} exp(log_post[w] − logsumexp(log_post)).
    """
    if not reference_set:
        return [0.0] * len(per_day_S)
    p_obs = min(max(p_obs, 1e-6), 1.0 - 1e-6)
    log_p = math.log(p_obs)
    log_q = math.log(1.0 - p_obs)
    log_prior = -math.log(len(reference_set))
    log_post: dict[str, float] = {w: log_prior for w in reference_set}
    Q_size: dict[str, int] = {w: len(Q_w) for w, Q_w in reference_set.items()}
    R = set(repertoire)
    out: list[float] = []
    for S_d in per_day_S:
        for w, Q_w in reference_set.items():
            n_hit = len(Q_w & S_d)
            log_post[w] += n_hit * log_p + (Q_size[w] - n_hit) * log_q
        m = max(log_post.values())
        # Numerically-stable logsumexp; cancels the constant prior offset.
        denom = m + math.log(sum(math.exp(v - m) for v in log_post.values()))
        mass = sum(
            math.exp(log_post[w] - denom) for w in R if w in log_post
        )
        out.append(mass)
    return out


# =====================================================================
# Per-user driver
# =====================================================================

def analyze_user(
    user: UserResult,
    *,
    reference_set: dict[str, set[str]],
    alpha: float,
    p_obs: float,
    R_size: int = 10,
) -> UserMetrics:
    per_day_S = [d.S_prime_total for d in user.days]
    per_day_C = [candidate_set(S, reference_set, alpha) for S in per_day_S]
    R = set(user.repertoire)
    posteriors = bayesian_posterior_R(
        per_day_S, reference_set, user.repertoire, p_obs,
    )

    per_day_rows = []
    for day, C in zip(user.days, per_day_C):
        per_day_rows.append({
            "d": day.day_idx,
            "S_prime_size": len(day.S_prime_total),
            "cand_size": len(C),
            "underflow_count": day.underflow_count,
            "odoh_fallback_count": day.odoh_fallback_count,
        })

    intersections: list[dict] = []
    intersect: set[str] | None = None
    for d_idx, C in enumerate(per_day_C, start=1):
        intersect = C if intersect is None else intersect & C
        intersections.append({
            "D": d_idx,
            "intersect_size": len(intersect),
            "repertoire_acc": (
                len(R & intersect) / max(len(R), 1)
            ),
            "posterior_R": posteriors[d_idx - 1],
        })

    days_to_fp, censored = days_to_fingerprint(per_day_C, R_size)
    return UserMetrics(
        user_idx=user.user_idx,
        repertoire=list(user.repertoire),
        per_day=per_day_rows,
        intersections=intersections,
        days_to_fp_alpha=days_to_fp,
        days_to_fp_censored=censored,
    )


# =====================================================================
# Cell-level rollup
# =====================================================================

def aggregate_cell(
    user_metrics: list[UserMetrics],
    *,
    cell_tag: str,
    p_obs: float,
    posterior_threshold: float = 0.9,
) -> CellMetrics:
    n = len(user_metrics)
    if n == 0:
        return CellMetrics(
            cell_tag=cell_tag, n_users=0,
            days_to_fp_median=0.0, days_to_fp_p10=0.0, days_to_fp_p90=0.0,
            n_censored=0, posterior_fraction_users=0.0,
            repertoire_acc_median_final=0.0, underflow_day_rate_mean=0.0,
            p_obs_fitted=p_obs,
        )

    days = sorted(m.days_to_fp_alpha for m in user_metrics)
    n_censored = sum(1 for m in user_metrics if m.days_to_fp_censored)
    median = _quantile(days, 0.5)
    p10 = _quantile(days, 0.10)
    p90 = _quantile(days, 0.90)

    n_post = sum(
        1 for m in user_metrics
        if any(it["posterior_R"] >= posterior_threshold
               for it in m.intersections)
    )
    posterior_frac = n_post / n

    rep_accs = sorted(
        m.intersections[-1]["repertoire_acc"]
        for m in user_metrics if m.intersections
    )
    rep_acc_median = _quantile(rep_accs, 0.5) if rep_accs else 0.0

    uf_rates: list[float] = []
    for m in user_metrics:
        n_days = len(m.per_day)
        if n_days:
            n_uf = sum(1 for d in m.per_day if d["underflow_count"] > 0)
            uf_rates.append(n_uf / n_days)
    uf_mean = sum(uf_rates) / len(uf_rates) if uf_rates else 0.0

    return CellMetrics(
        cell_tag=cell_tag, n_users=n,
        days_to_fp_median=median,
        days_to_fp_p10=p10,
        days_to_fp_p90=p90,
        n_censored=n_censored,
        posterior_fraction_users=posterior_frac,
        repertoire_acc_median_final=rep_acc_median,
        underflow_day_rate_mean=uf_mean,
        p_obs_fitted=p_obs,
    )


# =====================================================================
# Helpers
# =====================================================================

def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1,
                     int(round(q * (len(sorted_vals) - 1)))))
    return float(sorted_vals[idx])
