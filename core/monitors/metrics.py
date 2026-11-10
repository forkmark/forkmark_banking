"""Metrics, divergence, and Population Stability Index (PSI) calculations for drift monitoring."""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.comparator import divergence_score
from core.finance_evaluators import NumericalFidelityEvaluator

ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def calculate_arabic_share(text: str) -> float:
    """Proportion of Arabic Unicode characters among non-whitespace characters."""
    if not text:
        return 0.0
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    arabic_count = sum(1 for c in chars if ARABIC_CHAR_RE.match(c))
    return round(arabic_count / len(chars), 4)


def is_arabic_text(text: str, threshold: float = 0.20) -> bool:
    """Return True if text has at least `threshold` Arabic characters."""
    return calculate_arabic_share(text) >= threshold


def compute_psi(
    baseline: Sequence[float],
    current: Sequence[float],
    num_bins: int = 10,
    epsilon: float = 1e-4,
) -> float:
    """Compute the Population Stability Index (PSI) between baseline and current distributions.

    Uses quantile binning derived from the baseline distribution.
    Credit-risk interpretation bands:
        PSI < 0.10: Stable (OK)
        0.10 <= PSI < 0.25: Moderate shift (Watch)
        PSI >= 0.25: Significant shift (Alert)
    """
    if not baseline or not current:
        return 0.0

    b_vals = sorted(float(x) for x in baseline)
    c_vals = [float(x) for x in current]

    n_b = len(b_vals)
    n_c = len(c_vals)

    # Identical values or degenerate baseline
    if b_vals[0] == b_vals[-1]:
        # If both distributions are identical constants
        if all(c == b_vals[0] for c in c_vals):
            return 0.0
        # If current has varied values, compare match proportion
        match_c = sum(1 for c in c_vals if c == b_vals[0]) / n_c
        non_match_c = 1.0 - match_c
        e1, a1 = 1.0 - epsilon, max(match_c, epsilon)
        e2, a2 = epsilon, max(non_match_c, epsilon)
        return round((a1 - e1) * math.log(a1 / e1) + (a2 - e2) * math.log(a2 / e2), 4)

    # Compute quantile breakpoints from baseline (0, 10th, 20th, ..., 90th percentile)
    quantiles = [i / num_bins for i in range(1, num_bins)]
    raw_cuts = []
    for q in quantiles:
        idx = int(round(q * (n_b - 1)))
        raw_cuts.append(b_vals[idx])

    # Ensure strictly non-decreasing cuts; remove duplicate adjacent thresholds
    cuts: List[float] = []
    for cut in raw_cuts:
        if not cuts or cut > cuts[-1]:
            cuts.append(cut)

    # Place values in bins: (-inf, cuts[0]], (cuts[0], cuts[1]], ..., (cuts[-1], +inf)
    num_effective_bins = len(cuts) + 1
    b_counts = [0] * num_effective_bins
    c_counts = [0] * num_effective_bins

    def _bin_idx(v: float) -> int:
        for i, cut in enumerate(cuts):
            if v <= cut:
                return i
        return len(cuts)

    for v in b_vals:
        b_counts[_bin_idx(v)] += 1
    for v in c_vals:
        c_counts[_bin_idx(v)] += 1

    psi = 0.0
    for b_cnt, c_cnt in zip(b_counts, c_counts):
        b_pct = max(b_cnt / n_b, epsilon)
        c_pct = max(c_cnt / n_c, epsilon)
        psi += (c_pct - b_pct) * math.log(c_pct / b_pct)

    return round(float(psi), 4)


def classify_psi(psi: float) -> str:
    """Classify PSI into standard credit-risk monitoring bands: 'OK', 'Watch', 'Alert'."""
    if psi >= 0.25:
        return "Alert"
    elif psi >= 0.10:
        return "Watch"
    return "OK"


_fidelity_evaluator = NumericalFidelityEvaluator()


def evaluate_grounding(source_doc: str, reply: str) -> bool:
    """Evaluate whether numerical figures in reply reconcile to source document."""
    if not source_doc:
        return True
    res = _fidelity_evaluator.evaluate(source_doc, reply)
    return res.is_faithful


def evaluate_divergence(baseline_reply: str, current_reply: str) -> float:
    """Compute local lexical or semantic divergence score in [0.0, 1.0]."""
    return divergence_score(baseline_reply, current_reply)

