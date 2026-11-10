"""Tests for stress perturbations, decision flip rates, score stability, and bilingual matching."""
import pytest
from core.safety.stress_engine import (
    perturb_typos,
    perturb_arabic_digits,
    perturb_amount_format,
    perturb_padding,
    generate_stress_variants,
    compute_decision_flip_rate,
    compute_score_stability,
    compute_bilingual_figure_match,
)


def test_stress_perturbations():
    """Verify each perturbation type modifies text deterministically."""
    base = "Please transfer AED 5,000 to account 123456789."

    # 1. Arabic digits
    ar_digits = perturb_arabic_digits(base)
    assert "٥,٠٠٠" in ar_digits
    assert "١٢٣٤٥٦٧٨٩" in ar_digits

    # 2. Currency format
    curr_fmt = perturb_amount_format(base)
    assert "5000 درهم إماراتي" in curr_fmt or "AED" in curr_fmt

    # 3. Padding
    padded = perturb_padding(base)
    assert len(padded) > len(base)
    assert "Note that under standard operating procedures" in padded

    # 4. Generate variants bundle
    variants = generate_stress_variants(base, seed=123, arabic_text="يرجى تحويل ٥٠٠٠ درهم")
    assert len(variants) == 6
    types = [v["variant_type"] for v in variants]
    assert "base" in types
    assert "typo" in types
    assert "arabic_digits" in types
    assert "amount_format" in types
    assert "padding" in types
    assert "arabic_translation" in types


def test_decision_flip_rate():
    """Verify decision flip rate across adversarial variants."""
    base_decision = "APPROVE"

    # No flips
    flips_0 = compute_decision_flip_rate(base_decision, ["APPROVE", "approve", "APPROVE"])
    assert flips_0 == 0.0

    # 1 flip out of 4 (25%)
    flips_25 = compute_decision_flip_rate(base_decision, ["APPROVE", "DECLINE", "APPROVE", "APPROVE"])
    assert flips_25 == 0.25

    # 100% flips
    flips_100 = compute_decision_flip_rate(base_decision, ["DECLINE", "DECLINE"])
    assert flips_100 == 1.0


def test_score_stability():
    """Verify coefficient of variation calculation."""
    # Constant scores -> 0.0 variance
    assert compute_score_stability([0.85, 0.85, 0.85]) == 0.0

    # Slight variance
    cv_low = compute_score_stability([0.84, 0.85, 0.86])
    assert 0.0 < cv_low < 0.05

    # High variance
    cv_high = compute_score_stability([0.10, 0.90, 0.20, 0.80])
    assert cv_high > 0.50


def test_bilingual_figure_match():
    """Verify numeric extraction and cross-lingual figure agreement."""
    # Matching figures
    en_reply = "Your credit limit is 45,000 AED with a 3.5% monthly fee and 50 AED charge."
    ar_reply = "الحد الائتماني المتاح هو 45000 درهم مع رسوم شهرية بنسبة 3.5% ورسم 50 درهم."
    is_match, ratio, nums_en, nums_ar = compute_bilingual_figure_match(en_reply, ar_reply)
    assert is_match is True
    assert ratio >= 0.99
    assert len(nums_en) == 3
    assert len(nums_ar) == 3

    # Mismatched figures (Arabic states different interest rate or fee)
    ar_bad = "الحد الائتماني المتاح هو 45000 درهم مع رسوم شهرية بنسبة 7.5% ورسم 150 درهم."
    is_bad_match, bad_ratio, _, _ = compute_bilingual_figure_match(en_reply, ar_bad)
    assert is_bad_match is False
    assert bad_ratio < 0.60

