"""Adversarial stress and perturbation engine for financial LLM evaluation.

Generates deterministic perturbations of test cases:
1. Character swap typos (2% swap rate)
2. Arabic-Indic digit replacement (0-9 <-> ٠-٩)
3. Currency/amount formatting variations (AED 1,000 vs 1000 درهم)
4. English/Arabic cross-lingual prompt pairs
5. Irrelevant padding and long-context injection

Measures:
- Decision flip rate (when expected_decision is defined)
- Score stability (coefficient of variation of evaluator scores across variants)
- Bilingual figure matching between English and Arabic replies
"""
from __future__ import annotations

import math
import random
import re
from typing import Any, Dict, List, Optional, Tuple

from core.finance_evaluators import _extract_numbers


WESTERN_TO_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
ARABIC_TO_WESTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

FILLER_PADDING = (
    "Note that under standard operating procedures, documentation must be retained "
    "for supervisory inspection. All inquiries remain confidential per bank standards. "
)


def perturb_typos(text: str, rng: random.Random, rate: float = 0.02) -> str:
    """Introduce ~2% adjacent character swap typos."""
    if len(text) < 4:
        return text
    chars = list(text)
    n_swaps = max(1, int(len(chars) * rate))
    for _ in range(n_swaps):
        idx = rng.randint(0, len(chars) - 2)
        if chars[idx].isalnum() and chars[idx + 1].isalnum() and chars[idx] != chars[idx + 1]:
            chars[idx], chars[idx + 1] = chars[idx + 1], chars[idx]
    return "".join(chars)


def perturb_arabic_digits(text: str) -> str:
    """Convert Western digits to Arabic-Indic digits."""
    return text.translate(WESTERN_TO_ARABIC_DIGITS)


def perturb_amount_format(text: str) -> str:
    """Permute currency formatting, e.g. AED 1,000 <-> 1000 درهم."""
    # AED 1,000 -> 1000 درهم
    def _to_ar(match):
        num = match.group(1).replace(",", "")
        return f"{num} درهم إماراتي"

    res = re.sub(r"(?:AED|aed)\s*([0-9,]+(?:\.[0-9]+)?)", _to_ar, text)
    if res != text:
        return res

    # 1,000 AED -> AED 1,000
    res = re.sub(r"([0-9,]+(?:\.[0-9]+)?)\s*(?:AED|aed)", r"AED \1", text)
    return res


def perturb_padding(text: str) -> str:
    """Inject irrelevant regulatory/operational padding at the beginning."""
    return f"{FILLER_PADDING}\n\n{text}"


def generate_stress_variants(
    base_text: str,
    seed: int = 42,
    arabic_text: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Generate deterministic stress variants for a single prompt."""
    rng = random.Random(seed)
    variants = [
        {"variant_type": "base", "text": base_text},
        {"variant_type": "typo", "text": perturb_typos(base_text, rng)},
        {"variant_type": "arabic_digits", "text": perturb_arabic_digits(base_text)},
        {"variant_type": "amount_format", "text": perturb_amount_format(base_text)},
        {"variant_type": "padding", "text": perturb_padding(base_text)},
    ]
    if arabic_text:
        variants.append({"variant_type": "arabic_translation", "text": arabic_text})
    return variants


def compute_decision_flip_rate(
    base_decision: str,
    variant_decisions: List[str],
) -> float:
    """Proportion of stress variants whose decision differs from the baseline."""
    if not variant_decisions:
        return 0.0
    norm_base = base_decision.lower().strip()
    flips = sum(1 for d in variant_decisions if d.lower().strip() != norm_base)
    return flips / len(variant_decisions)


def compute_score_stability(scores: List[float]) -> float:
    """Coefficient of variation (CV = std_dev / mean) across variants.

    Lower is more stable. Returns 0.0 for perfectly constant scores.
    """
    if not scores:
        return 0.0
    mean = sum(scores) / len(scores)
    if mean == 0:
        return 0.0
    variance = sum((s - mean) ** 2 for s in scores) / len(scores)
    std_dev = math.sqrt(variance)
    return std_dev / abs(mean)


def compute_figure_consistency(
    text1: str,
    text2: str,
    tolerance: float = 1e-4,
) -> Tuple[bool, float, List[float], List[float]]:
    """Check whether numbers in baseline and perturbed variant replies match.

    Returns:
        (is_match, match_ratio, text1_numbers, text2_numbers)
    """
    nums1 = [n.value for n in _extract_numbers(text1) if not math.isnan(n.value)]
    nums2 = [n.value for n in _extract_numbers(text2) if not math.isnan(n.value)]

    if not nums1 and not nums2:
        return True, 1.0, [], []
    if not nums1 or not nums2:
        return False, 0.0, nums1, nums2

    matched = 0
    used2 = set()
    for ne in nums1:
        for j, na in enumerate(nums2):
            if j not in used2 and math.isclose(ne, na, rel_tol=tolerance, abs_tol=tolerance):
                matched += 1
                used2.add(j)
                break

    match_ratio = (2.0 * matched) / (len(nums1) + len(nums2))
    is_match = (match_ratio >= 0.99)
    return is_match, match_ratio, nums1, nums2


def compute_bilingual_figure_match(
    text_en: str,
    text_ar: str,
    tolerance: float = 1e-4,
) -> Tuple[bool, float, List[float], List[float]]:
    """Check whether numbers in English and Arabic replies match (bilingual translation variant)."""
    return compute_figure_consistency(text_en, text_ar, tolerance=tolerance)

