"""Statistical analysis endpoints.

Turn paired A/B comparison scores into the defensible statistics a model
validator needs: a win rate with a Wilson confidence interval, a paired t-test
p-value (the samples are matched by evaluation case) with Benjamini-Hochberg
control across a batch, a paired effect size (Cohen's d_z), and the power /
sample-size context that shows whether the result can be relied upon (supports
the outcomes-analysis expectations of SR 11-7 and PRA SS1/23).
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from backend.deps import ui_read_auth
from core.statistical_analyzer import (
    StatisticalResult,
    analyze,
    analyze_batch,
    power_analysis,
)

router = APIRouter(prefix="/api", tags=["statistics"])


# ── Schemas ─────────────────────────────────────────────────────────────────


class ComparisonPair(BaseModel):
    scores_a: List[float] = Field(..., min_length=2, description="Branch A per-sample scores")
    scores_b: List[float] = Field(..., min_length=2, description="Branch B per-sample scores")


class AnalyzeRequest(BaseModel):
    """Provide either a single pair (``scores_a``/``scores_b``) or a batch of
    ``comparisons``. A batch triggers Benjamini-Hochberg FDR control."""

    scores_a: Optional[List[float]] = Field(None, description="Branch A scores")
    scores_b: Optional[List[float]] = Field(None, description="Branch B scores")
    comparisons: Optional[List[ComparisonPair]] = Field(
        None, description="Multiple comparisons; FDR-controlled as a family."
    )
    confidence_level: float = Field(0.95, gt=0.5, lt=1.0)
    alpha: float = Field(0.05, gt=0.0, lt=1.0)

    @model_validator(mode="after")
    def _check_inputs(self) -> "AnalyzeRequest":
        has_single = self.scores_a is not None and self.scores_b is not None
        has_batch = bool(self.comparisons)
        if not has_single and not has_batch:
            raise ValueError("Provide scores_a+scores_b or a non-empty comparisons list.")
        return self


class StatisticalResultResponse(BaseModel):
    win_rate: float
    ci_lower: float
    ci_upper: float
    p_value: float
    adjusted_p_value: float
    effect_size: float
    is_significant: bool
    sample_size: int
    minimum_detectable_effect: float
    method: str = Field(
        "paired_t_test",
        description="Significance/effect-size method: paired_t_test (default) or welch_t_test.",
    )
    plain_english: str


class AnalyzeResponse(BaseModel):
    results: List[StatisticalResultResponse]
    multiple_comparison_correction: bool = Field(
        ..., description="True when Benjamini-Hochberg FDR control was applied."
    )


class PowerAnalysisRequest(BaseModel):
    effect_size: float = Field(..., description="Target Cohen's d to detect (non-zero)")
    power: float = Field(0.8, gt=0.0, lt=1.0)
    alpha: float = Field(0.05, gt=0.0, lt=1.0)


class PowerAnalysisResponse(BaseModel):
    effect_size: float
    power: float
    alpha: float
    minimum_sample_size_per_branch: int
    note: str


def _to_response(result: StatisticalResult) -> StatisticalResultResponse:
    return StatisticalResultResponse(
        win_rate=result.win_rate,
        ci_lower=result.ci_lower,
        ci_upper=result.ci_upper,
        p_value=result.p_value,
        adjusted_p_value=result.adjusted_p_value,
        effect_size=result.effect_size,
        is_significant=result.is_significant,
        sample_size=result.sample_size,
        minimum_detectable_effect=result.minimum_detectable_effect,
        method=result.method,
        plain_english=result.as_plain_english(),
    )


# ── Routes ──────────────────────────────────────────────────────────────────


@router.post(
    "/statistics/analyze",
    response_model=AnalyzeResponse,
    summary="Analyze A/B comparison scores",
)
def analyze_scores(body: AnalyzeRequest, _auth: object = Depends(ui_read_auth)) -> AnalyzeResponse:
    """Compute win rate (with 95% Wilson CI), Welch's t-test significance, Cohen's
    d, and the minimum detectable effect. Supplying multiple ``comparisons``
    applies Benjamini-Hochberg FDR control across the family."""
    try:
        if body.comparisons:
            pairs = [(c.scores_a, c.scores_b) for c in body.comparisons]
            results = analyze_batch(
                pairs, confidence_level=body.confidence_level, alpha=body.alpha
            )
            corrected = True
        else:
            assert body.scores_a is not None and body.scores_b is not None
            results = [
                analyze(
                    body.scores_a,
                    body.scores_b,
                    confidence_level=body.confidence_level,
                    alpha=body.alpha,
                )
            ]
            corrected = False
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return AnalyzeResponse(
        results=[_to_response(r) for r in results],
        multiple_comparison_correction=corrected,
    )


@router.post(
    "/statistics/power-analysis",
    response_model=PowerAnalysisResponse,
    summary="Minimum sample size for a target effect",
)
def compute_power(
    body: PowerAnalysisRequest, _auth: object = Depends(ui_read_auth)
) -> PowerAnalysisResponse:
    """Return the minimum samples per branch needed to detect ``effect_size`` at
    the requested power and significance level (Cohen, 1988; normal
    approximation)."""
    try:
        n = power_analysis(body.effect_size, power=body.power, alpha=body.alpha)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PowerAnalysisResponse(
        effect_size=body.effect_size,
        power=body.power,
        alpha=body.alpha,
        minimum_sample_size_per_branch=n,
        note=(
            f"At least {n} samples per branch are required to detect a Cohen's d of "
            f"{body.effect_size} with power {body.power:.0%} at alpha {body.alpha}."
        ),
    )
