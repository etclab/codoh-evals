"""Parameter-cell expansion and stable hashing.

A *cell* is one point in the simulator's parameter grid:
    (B, T_max_s, k, lambda_bg, N, alpha, D, init)

Sweep presets:
  - smoke   : 3x3 corners on (B, T_max) at default other-axes
  - default : 5x5 default-axis sweep
  - full    : extended grid covering the supplementary axes

`Cell.hash` is a stable blake2b digest over the canonical JSON form, so the
on-disk layout `out/raw/cell_<hash>/trials.jsonl.gz` is reproducible across
processes / machines. Hash length is 12 hex chars (48 bits) — collision odds
are < 1e-10 at the grid sizes we run.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import asdict, dataclass
from typing import Iterable

from .trial import TrialParams

# ---------------------------------------------------------------------
# Default axes (single-value lists; presets override one or two).
# ---------------------------------------------------------------------

DEFAULTS = {
    "B": [20],
    "T_max_s": [300.0],
    "k": [3],
    "lambda_bg": [100],
    "N": [1024],
    "alpha": [0.5],
    "D": ["matched"],
    "init": ["cold"],
}

# ---------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------

SMOKE_AXES = {
    **DEFAULTS,
    "B": [5, 20, 100],
    "T_max_s": [30.0, 300.0, 1800.0],
}

DEFAULT_AXES = {
    **DEFAULTS,
    "B": [5, 20, 50, 100, 256],
    "T_max_s": [30.0, 100.0, 300.0, 600.0, 1800.0],
}

FULL_AXES = {
    "B": [5, 10, 20, 50, 100, 256, 512],
    "T_max_s": [10.0, 30.0, 100.0, 300.0, 600.0, 1800.0, 3600.0],
    "k": [1, 3, 5],
    "lambda_bg": [0, 50, 100, 500],
    "N": [1024],
    "alpha": [0.3, 0.5, 0.7],
    "D": ["matched", "uniform", "stale"],
    "init": ["cold", "warm"],
}

PRESETS: dict[str, dict] = {
    "smoke": SMOKE_AXES,
    "default": DEFAULT_AXES,
    "full": FULL_AXES,
}


# ---------------------------------------------------------------------
# Cell
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class Cell:
    B: int
    T_max_s: float
    k: int
    lambda_bg: int
    N: int
    alpha: float
    D: str
    init: str

    @property
    def hash(self) -> str:
        canonical = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.blake2b(canonical, digest_size=6).hexdigest()

    @property
    def label(self) -> str:
        """Short human-readable label for plot axes / log lines."""
        return (f"B{self.B}_T{int(self.T_max_s)}_k{self.k}_"
                f"bg{self.lambda_bg}_N{self.N}_a{self.alpha}_"
                f"{self.D}_{self.init}")

    def to_trial_params(self) -> TrialParams:
        return TrialParams(
            B=self.B, T_max_s=self.T_max_s, k=self.k,
            lambda_bg=self.lambda_bg, N=self.N, alpha=self.alpha,
            D=self.D, init=self.init,
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------

_AXIS_ORDER = ("B", "T_max_s", "k", "lambda_bg", "N", "alpha", "D", "init")


def expand(axes: dict | str) -> list[Cell]:
    """Cartesian product over the axis dict (or named preset)."""
    if isinstance(axes, str):
        if axes not in PRESETS:
            raise ValueError(f"unknown preset: {axes!r}; "
                             f"choose from {sorted(PRESETS)}")
        axes = PRESETS[axes]
    missing = [k for k in _AXIS_ORDER if k not in axes]
    if missing:
        raise ValueError(f"axes missing keys: {missing}")
    pools = [axes[k] for k in _AXIS_ORDER]
    out = []
    for combo in itertools.product(*pools):
        out.append(Cell(**dict(zip(_AXIS_ORDER, combo))))
    return out


def parse_overrides(overrides: Iterable[str]) -> dict:
    """Parse `--set B=5,20 T_max_s=30,300` style overrides.

    Each token is `key=v1,v2,...`. Numeric / float / bool literals are
    auto-coerced; anything else passes as a string. Used by the CLI to
    poke single axes without authoring a new preset.
    """
    out: dict = {}
    for tok in overrides:
        if "=" not in tok:
            raise ValueError(f"override must be key=values: {tok!r}")
        key, raw = tok.split("=", 1)
        if key not in _AXIS_ORDER:
            raise ValueError(f"unknown axis: {key!r}")
        out[key] = [_coerce(v) for v in raw.split(",")]
    return out


def _coerce(s: str):
    s = s.strip()
    for fn in (int, float):
        try:
            return fn(s)
        except ValueError:
            continue
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    return s
